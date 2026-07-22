"""断点续写：启动扫描本地 CSV 的 pending 行 + 磁带根目录识别。"""
from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .catalog import LocalCatalog
from .constants import STATUS_DONE

LOG = logging.getLogger("cryotape")


@dataclass(frozen=True)
class TapeInventory:
    """扫描挂载点根目录得到的盘内资产清单。"""
    has_catalog_files: tuple[Path, ...]   # Catalog_Part*.csv
    has_manifest_files: tuple[Path, ...]  # Manifest_Part*.txt
    has_tar_files: tuple[Path, ...]       # *_Part*.tar


class ResumeHandler:
    """
    启动时：
        1. 扫描本地 CSV 的 pending 行，打印提醒
        2. 扫描挂载点根目录的 Catalog_Part*.csv，识别未完成的盘
        3. 提示用户选择处理方式
    """

    def __init__(self, catalog: LocalCatalog, mount: Path) -> None:
        self.catalog = catalog
        self.mount = mount

    def on_start(self, *, non_interactive: bool, assume_yes: bool) -> None:
        """启动钩子：扫描本地 CSV 并提醒。"""
        pending = self.catalog.scan_pending()
        if not pending:
            LOG.info("本地 CSV 中没有 pending 记录，无需恢复。")
            return
        LOG.warning("本地 CSV 中有 %d 条 pending 记录:", len(pending))
        for r in pending:
            print(f"  - {r.get('项目名称')} / {r.get('分卷编号')} / "
                  f"{r.get('归档文件名')} @ {r.get('磁带挂载点')}")
        print("\n请插入对应磁带，工具将自动识别未完成的盘并提示处理方式。\n")

    def inspect_mount(self) -> TapeInventory:
        """扫描挂载点根目录。"""
        if not self.mount.exists():
            return TapeInventory((), (), ())
        catalogs: list[Path] = []
        manifests: list[Path] = []
        tars: list[Path] = []
        for p in sorted(self.mount.iterdir()):
            if not p.is_file():
                continue
            n = p.name
            if re.match(r"^Catalog_Part\d+\.csv$", n):
                catalogs.append(p)
            elif re.match(r"^Manifest_Part\d+\.txt$", n):
                manifests.append(p)
            elif re.match(r"^.+_Part\d+\.tar$", n):
                tars.append(p)
        return TapeInventory(tuple(catalogs), tuple(manifests), tuple(tars))

    def find_unfinished_part_on_tape(self) -> Optional[Path]:
        """
        检查磁带上是否存在 Catalog_PartNN.csv 且 status != done。
        返回该 catalog 路径；否则 None。
        """
        inv = self.inspect_mount()
        for cat_path in inv.has_catalog_files:
            try:
                with cat_path.open("r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get("状态") and row["状态"] != STATUS_DONE:
                            return cat_path
            except OSError:
                continue
        return None