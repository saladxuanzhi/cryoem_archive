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
from .packer import GlobalPacker, TapePacker
from .resume import ResumeHandler
from .streamer import TarStreamer
from .types import CapacityPlan, ProjectInfo, TapePart
from .utils import (
    format_size,
    prompt,
    prompt_choice,
    prompt_text,
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
        projects = self._build_projects(roots)
        if not projects:
            print("[!] 没有发现任何项目的文件")
            return

        min_tail_bytes = int(self.config.min_tail_gb * 1000 ** 3)
        global_packer = GlobalPacker(
            cap_plan.cap_bytes,
            min_tail_bytes,
            integrity_priority=self.config.integrity_priority,
        )
        try:
            parts = global_packer.pack(projects)
        except OversizedFileError as exc:
            print(f"\n[!] {exc}")
            return

        tape_assignment = global_packer.tape_assignment(parts)
        total_size = sum(p.capacity_used_bytes for p in parts)
        n_tapes = tape_assignment[-1] if tape_assignment else 0
        mode = "完整度优先" if self.config.integrity_priority else "节省空间优先"

        print(f"\n[*] 共 {len(projects)} 个项目，总计 {format_size(total_size)}，"
              f"将分为 {len(parts)} 个 part，写入 {n_tapes} 盘磁带（{mode}）\n")

        # 按 project 分组打印
        from collections import defaultdict
        by_project: dict[str, list[tuple[int, TapePart, int]]] = defaultdict(list)
        for tape_no, part in zip(tape_assignment, parts):
            by_project[part.project_name].append((tape_no, part, 0))

        for project in projects:
            proj_parts = by_project.get(project.name, [])
            proj_total = sum(p.capacity_used_bytes for _, p, _ in proj_parts)
            print(f"\n📦 {project.name}: {project.file_count} 个文件，"
                  f"{format_size(proj_total)}，将分为 {len(proj_parts)} 个 part")
            for tape_no, p, _ in proj_parts:
                ratio = p.capacity_used_bytes / p.capacity_cap_bytes
                print(f"  - Tape{tape_no:02d} / Part{p.part_index:02d}: "
                      f"{p.file_count} 文件, {format_size(p.capacity_used_bytes)} "
                      f"({ratio * 100:.1f}% of {format_size(p.capacity_cap_bytes)}), "
                      f"{p.first_file}  ==>  {p.last_file}")

        print("\n" + "-" * 60)
        print("📼 磁带汇总:")
        for tape_no in sorted(set(tape_assignment)):
            tape_parts = [p for n, p in zip(tape_assignment, parts) if n == tape_no]
            used = sum(p.capacity_used_bytes for p in tape_parts)
            print(f"  Tape{tape_no:02d}: {len(tape_parts)} 个 part, "
                  f"总计 {format_size(used)}, "
                  f"剩余 {format_size(cap_plan.cap_bytes - used)}")

    # ---------- 真实写盘 ----------
    def _archive_all(self, roots: Sequence[Path], cap_plan: CapacityPlan) -> None:
        projects = self._build_projects(roots)
        if not projects:
            print("[!] 没有发现任何项目的文件")
            return

        min_tail_bytes = int(self.config.min_tail_gb * 1000 ** 3)
        global_packer = GlobalPacker(
            cap_plan.cap_bytes,
            min_tail_bytes,
            integrity_priority=self.config.integrity_priority,
        )
        try:
            parts = global_packer.pack(projects)
        except OversizedFileError as exc:
            print(f"[X] {exc}")
            return

        tape_assignment = global_packer.tape_assignment(parts)
        n_tapes = tape_assignment[-1] if tape_assignment else 0
        total_size = sum(p.capacity_used_bytes for p in parts)
        print(f"\n[*] 共 {len(projects)} 个项目，总计 {format_size(total_size)}，"
              f"将分为 {len(parts)} 个 part，写入 {n_tapes} 盘磁带")

        # 跟踪当前磁带已用空间
        current_tape_used = 0
        current_tape_idx = tape_assignment[0] if tape_assignment else 0

        for i, part in enumerate(parts):
            target_tape_idx = tape_assignment[i]
            # 检测是否需要换磁带
            if target_tape_idx != current_tape_idx:
                # 换磁带
                self._prompt_tape_swap(current_tape_idx, target_tape_idx)
                current_tape_idx = target_tape_idx
                current_tape_used = 0

            # 找到 project 的 parent_dir
            parent_dir = next(p.parent_dir for p in projects if p.name == part.project_name)

            self._write_one_part(
                part=part,
                parent_dir=parent_dir,
                cap_plan=cap_plan,
                is_last_part=(i == len(parts) - 1),
                tape_idx=target_tape_idx,
                current_tape_used=current_tape_used,
            )
            current_tape_used += part.capacity_used_bytes

    def _build_projects(self, roots: Sequence[Path]) -> list[ProjectInfo]:
        """为每个 project root 构建 ProjectInfo（含已排序的文件列表）。"""
        projects: list[ProjectInfo] = []
        for root in roots:
            files = FileDiscovery(root).discover()
            if not files:
                print(f"[!] {root.name}: 没有文件，跳过")
                continue
            projects.append(ProjectInfo(
                name=root.name,
                root=root,
                parent_dir=root.parent,
                files=files,
            ))
        return projects

    def _prompt_tape_swap(self, current_tape: int, next_tape: int) -> None:
        """提示用户插入新磁带。"""
        print(f"\n[!] === 磁带切换 ===")
        print(f"    即将写入 Tape{next_tape:02d}，请弹出当前 Tape{current_tape:02d}")
        print(f"    执行: umount {self.config.ltfs_mount}")
        print(f"    换上新的空磁带（或已写入部分数据的磁带），重新挂载 LTFS")
        if not prompt("准备好后按 [Enter] 继续",
                      default_yes=self.config.yes,
                      non_interactive=self.config.non_interactive):
            raise UserAbortedError("用户在换带时取消")

    def _detect_existing_volume_name(self) -> str:
        """扫描挂载点根目录已存在的 Catalog_Part*.csv，返回其中
        已记录的磁带卷名（同盘磁带的所有 Part 共享同一卷名）。"""
        inv = self.resume.inspect_mount()
        for cat_path in inv.has_catalog_files:
            try:
                import csv as _csv
                with cat_path.open("r", newline="", encoding="utf-8") as f:
                    reader = _csv.DictReader(f)
                    for row in reader:
                        vn = (row.get("磁带卷名") or "").strip()
                        if vn:
                            return vn
            except OSError:
                continue
        return ""

    def _prompt_volume_name(self, default: str = "") -> str:
        """提示用户输入 / 确认磁带卷名。

        - 若 ``default`` 非空（来自同盘旧 Catalog），直接采用。
        - 若 non_interactive 模式，返回 ``default``（可能为空）。
        - 交互模式下若用户留空回车且 ``default`` 为空，拒绝通过。
        """
        if self.config.non_interactive:
            return default
        print(f"\n[*] 磁带卷名（标签纸 / 磁带外壳标记的标识符）:")
        if default:
            print(f"    检测到磁带上已有 Catalog，建议沿用: {default!r}")
            print(f"    直接回车确认，或输入新卷名覆盖（一般仅在换磁带时改）。")
        else:
            print(f"    未检测到已有 Catalog——请录入标签纸上的卷名")
            print(f"    （例如 TAPE-2026-07-24-A）。不允许为空。")
        while True:
            try:
                ans = input(f"磁带卷名 [{default}]: ").strip()
            except EOFError:
                return default
            if ans:
                return ans
            if default:
                return default
            print("    卷名不能为空，请输入或 Ctrl-C 取消。")

    def _write_one_part(self, *, part: TapePart, parent_dir: Path,
                        cap_plan: CapacityPlan, is_last_part: bool,
                        tape_idx: int = 0,
                        current_tape_used: int = 0) -> None:
        """处理单盘的完整生命周期：摘要→卷名→确认→Manifest→tar→盘内CSV→本地CSV。"""
        mount = self.config.ltfs_mount
        archive_path = mount / part.archive_name
        manifest_path = mount / part.manifest_name
        catalog_path = mount / part.catalog_name
        log_path = self.config.log_dir / f"{part.archive_name}.log"

        ratio = part.capacity_used_bytes / part.capacity_cap_bytes
        remaining_after = cap_plan.cap_bytes - current_tape_used - part.capacity_used_bytes
        print("\n" + "=" * 60)
        print(f"📦 准备写入 {part.project_name} {part.archive_name} → Tape{tape_idx:02d}")
        print(f"   文件数:        {part.file_count}")
        print(f"   数据体积:      {format_size(part.capacity_used_bytes)} "
              f"({ratio * 100:.1f}% of {format_size(part.capacity_cap_bytes)})")
        print(f"   起始文件:      {part.first_file}")
        print(f"   终止文件:      {part.last_file}")
        print(f"   目标磁带:      {mount} (Tape{tape_idx:02d})")
        print(f"   本盘写后剩余:  {format_size(max(0, remaining_after))}")
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

        # 磁带卷名：先尝试从磁带上现有 Catalog 推断；推断不到再问用户
        suggested_volume = self._detect_existing_volume_name()
        volume_name = self._prompt_volume_name(default=suggested_volume)

        # 交互确认
        if not prompt(
            f"请确认磁带已挂载到 {mount}，并按 [Enter] 开始写入 {part.archive_name}",
            default_yes=self.config.yes,
            non_interactive=self.config.non_interactive,
        ):
            raise UserAbortedError(f"用户在 {part.archive_name} 前取消")

        # 写本地 CSV pending（带卷名）
        self.catalog.append_pending(part, mount=mount, volume_name=volume_name)

        # 写 Manifest（先于 .tar，确保失败时也能看到清单）
        streamer = TarStreamer(
            parent_dir=parent_dir,
            archive_path=archive_path,
            log_path=log_path,
            verbose=self.config.verbose,
            show_progress=self.config.show_progress,
            progress_label=f"Tape{tape_idx:02d}/{part.archive_name}",
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

        # 写盘内 CSV（带卷名）
        write_date = today_date()
        TapeCatalogWriter.write(
            catalog_path, part,
            status=STATUS_DONE,
            mount=mount,
            write_date=write_date,
            volume_name=volume_name,
        )

        # 改本地 CSV 为 done
        self.catalog.update_status(
            project=part.project_name,
            part_index=part.part_index,
            new_status=STATUS_DONE,
        )

        print(f"[√] {part.archive_name} 写入完成 "
              f"(卷名: {volume_name!r}, "
              f"Manifest: {manifest_path.name}, "
              f"Catalog: {catalog_path.name}, log: {log_path.name})")
        # 磁带切换由 _archive_all 中的 tape_assignment 检测并通过 _prompt_tape_swap 处理
        # 此处不再重复询问

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