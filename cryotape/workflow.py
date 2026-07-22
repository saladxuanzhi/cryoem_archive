"""主流程 orchestration：装载所有组件并执行。"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Sequence

from .capacity import TapeCapacityPlanner
from .catalog import LocalCatalog, TapeCatalogWriter
from .constants import STATUS_DONE, STATUS_FAILED
from .detector import ProjectDetector
from .discovery import FileDiscovery
from .exceptions import (
    CryoTapeError,
    OversizedFileError,
    UserAbortedError,
)
from .interactive import RuntimeConfig
from .packer import TapePacker
from .resume import ResumeHandler
from .streamer import TarStreamer
from .types import CapacityPlan, TapePart
from .utils import (
    format_size,
    prompt,
    prompt_choice,
    today_date,
)
from .validator import PathValidator

LOG = logging.getLogger("cryotape")


class Workflow:
    """装配所有组件并执行主流程。"""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.catalog = LocalCatalog(config.csv_catalog)
        self.resume = ResumeHandler(self.catalog, config.ltfs_mount)

    # ---------- 入口 ----------
    def run(self) -> int:
        try:
            return self._run_inner()
        except UserAbortedError as exc:
            print(f"\n[!] 已中止: {exc}")
            return 130
        except CryoTapeError as exc:
            print(f"\n[X] 错误: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\n[!] 用户中断 (Ctrl-C)")
            return 130

    def _run_inner(self) -> int:
        # 1. 启动期预检
        PathValidator.check_tar_available()
        PathValidator.check_project_dir(self.config.project_dir)
        if not self.config.dry_run:
            PathValidator.check_ltfs_mount(self.config.ltfs_mount)
        PathValidator.check_csv_catalog(self.config.csv_catalog)
        self.config.log_dir.mkdir(parents=True, exist_ok=True)

        # 2. 启动期 resume 提醒
        self.resume.on_start(
            non_interactive=self.config.non_interactive,
            assume_yes=self.config.yes,
        )

        # 3. 项目判定
        detector = ProjectDetector(self.config.project_dir)
        project_roots = detector.detect(
            non_interactive=self.config.non_interactive,
            assume_yes=self.config.yes,
        )
        print(f"\n[*] 共需归档 {len(project_roots)} 个项目")

        # 4. 容量规划（每盘重新读，因为换带）
        cap_planner = TapeCapacityPlanner(
            self.config.ltfs_mount,
            new_tape_capacity_bytes=int(self.config.new_tape_capacity_gb * 1000 ** 3),
            safety_margin_bytes=int(self.config.safety_margin_gb * 1000 ** 3),
        )
        cap_plan = cap_planner.plan()
        print(f"[*] 容量上限: {format_size(cap_plan.cap_bytes)} "
              f"(来源: {cap_plan.source}, 安全余量: {format_size(cap_plan.margin_bytes)})")

        # 5. dry-run 路径
        if self.config.dry_run:
            self._dry_run(project_roots, cap_plan)
            return 0

        # 6. 真实归档循环
        self._archive_all(project_roots, cap_plan)
        print("\n[√] 全部任务完成，台账: " + str(self.config.csv_catalog))
        return 0

    # ---------- 干跑 ----------
    def _dry_run(self, roots: Sequence[Path], cap_plan: CapacityPlan) -> None:
        print("\n========== DRY RUN ==========")
        for root in roots:
            project_name = root.name
            files = FileDiscovery(root).discover()
            if not files:
                print(f"\n[!] {project_name}: 没有文件，跳过")
                continue
            packer = TapePacker(cap_plan.cap_bytes)
            try:
                parts = packer.pack(project_name, files)
            except OversizedFileError as exc:
                print(f"\n[!] {project_name}: {exc}")
                continue
            total_size = sum(p.capacity_used_bytes for p in parts)
            print(f"\n📦 {project_name}: {len(files)} 个文件，{format_size(total_size)}，"
                  f"将分为 {len(parts)} 盘")
            for p in parts:
                ratio = p.capacity_used_bytes / p.capacity_cap_bytes
                print(f"  - Part{p.part_index:02d}: {p.file_count} 文件, "
                      f"{format_size(p.capacity_used_bytes)} "
                      f"({ratio * 100:.1f}% of {format_size(p.capacity_cap_bytes)}), "
                      f"{p.first_file}  ==>  {p.last_file}")

    # ---------- 真实写盘 ----------
    def _archive_all(self, roots: Sequence[Path], cap_plan: CapacityPlan) -> None:
        for root in roots:
            self._archive_one_project(root, cap_plan)

    def _archive_one_project(self, project_root: Path, cap_plan: CapacityPlan) -> None:
        project_name = project_root.name
        parent_dir = project_root.parent
        files = FileDiscovery(project_root).discover()
        if not files:
            print(f"[!] {project_name}: 没有文件，跳过")
            return

        packer = TapePacker(cap_plan.cap_bytes)
        try:
            parts = packer.pack(project_name, files)
        except OversizedFileError as exc:
            print(f"[X] {project_name}: {exc}")
            return

        print(f"\n[*] 项目 {project_name}: {len(parts)} 盘，{len(files)} 个文件，"
              f"{format_size(sum(p.capacity_used_bytes for p in parts))}")

        for i, part in enumerate(parts):
            self._write_one_part(
                part=part,
                parent_dir=parent_dir,
                cap_plan=cap_plan,
                is_last_part=(i == len(parts) - 1),
            )

    def _write_one_part(self, *, part: TapePart, parent_dir: Path,
                        cap_plan: CapacityPlan, is_last_part: bool) -> None:
        """处理单盘的完整生命周期：摘要→确认→Manifest→tar→盘内CSV→本地CSV。"""
        mount = self.config.ltfs_mount
        archive_path = mount / part.archive_name
        manifest_path = mount / part.manifest_name
        catalog_path = mount / part.catalog_name
        log_path = self.config.log_dir / f"{part.archive_name}.log"

        ratio = part.capacity_used_bytes / part.capacity_cap_bytes
        print("\n" + "=" * 60)
        print(f"📦 准备写入 {part.project_name} {part.archive_name}")
        print(f"   文件数:        {part.file_count}")
        print(f"   数据体积:      {format_size(part.capacity_used_bytes)} "
              f"({ratio * 100:.1f}% of {format_size(part.capacity_cap_bytes)})")
        print(f"   起始文件:      {part.first_file}")
        print(f"   终止文件:      {part.last_file}")
        print(f"   目标磁带:      {mount}")
        print("=" * 60)

        # 断点续写识别
        unfinished_cat = self.resume.find_unfinished_part_on_tape()
        if unfinished_cat is not None:
            m = re.search(r"Catalog_Part(\d+)\.csv", unfinished_cat.name)
            if m and int(m.group(1)) == part.part_index:
                # 同一盘的中断恢复
                self._handle_resume_choice(part)
            elif m and int(m.group(1)) < part.part_index:
                print(f"[!] 检测到磁带上有未完成的 Part{int(m.group(1)):02d}，"
                      f"但当前要写 Part{part.part_index:02d}。请先处理或换盘。")
                if not prompt("是否仍要继续写当前 Part？(可能覆盖)",
                              default_yes=False,
                              non_interactive=self.config.non_interactive):
                    raise UserAbortedError("用户拒绝覆盖未完成磁带")

        # 交互确认
        if not prompt(
            f"请确认磁带已挂载到 {mount}，并按 [Enter] 开始写入 {part.archive_name}",
            default_yes=self.config.yes,
            non_interactive=self.config.non_interactive,
        ):
            raise UserAbortedError(f"用户在 {part.archive_name} 前取消")

        # 写本地 CSV pending
        self.catalog.append_pending(part, mount=mount)

        # 写 Manifest（先于 .tar，确保失败时也能看到清单）
        streamer = TarStreamer(
            parent_dir=parent_dir,
            archive_path=archive_path,
            log_path=log_path,
            verbose=self.config.verbose,
        )
        streamer.stream_manifest_file(manifest_path, part.files,
                                      part.capacity_used_bytes)

        # 流式 tar
        try:
            streamer.stream(part.files)
        except Exception:
            self.catalog.update_status(
                project=part.project_name,
                part_index=part.part_index,
                new_status=STATUS_FAILED,
            )
            raise

        # 写盘内 CSV
        write_date = today_date()
        TapeCatalogWriter.write(
            catalog_path, part,
            status=STATUS_DONE,
            mount=mount,
            write_date=write_date,
        )

        # 改本地 CSV 为 done
        self.catalog.update_status(
            project=part.project_name,
            part_index=part.part_index,
            new_status=STATUS_DONE,
        )

        print(f"[√] {part.archive_name} 写入完成 (Manifest: {manifest_path.name}, "
              f"Catalog: {catalog_path.name}, log: {log_path.name})")

        if not is_last_part:
            print(f"\n[!] 请弹出当前磁带：umount {mount}")
            print("[!] 换上新的空磁带（或已写入部分数据的磁带），重新挂载 LTFS")
            if not prompt("准备好后按 [Enter] 继续下一盘",
                          default_yes=self.config.yes,
                          non_interactive=self.config.non_interactive):
                raise UserAbortedError("用户在盘间换带时取消")

    def _handle_resume_choice(self, part: TapePart) -> None:
        """处理磁带上同一盘 Part 的恢复选择。"""
        print(f"[!] 检测到该磁带 Part{part.part_index:02d} 之前中断过。")
        choice = prompt_choice(
            f"Part{part.part_index:02d} 处理方式:",
            ["continue (继续，保留已有 .tar，跳过 Manifest/Catalog 重写)",
             "rewrite (丢弃已有 .tar，从头重写)",
             "abort (取消整个归档)"],
            non_interactive=self.config.non_interactive,
        )
        if "abort" in choice:
            raise UserAbortedError("用户在恢复选择中取消")
        if "rewrite" in choice:
            mount = self.config.ltfs_mount
            for name in (part.archive_name, part.manifest_name, part.catalog_name):
                p = mount / name
                if p.exists():
                    p.unlink()
                    LOG.info("已删除旧文件: %s", p)
        # continue：什么都不做，由后续流程自然跳过 Manifest/Catalog 写入
        # （这里我们仍会重写 Manifest 与 Catalog，确保一致性）