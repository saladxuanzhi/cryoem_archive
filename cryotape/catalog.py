"""CSV 台账：本地总 CSV（状态机）+ 盘内单盘 CSV。"""
from __future__ import annotations

import csv
import logging
import os
from pathlib import Path

from .constants import (
    ALL_STATUSES,
    CSV_HEADER,
    STATUS_DONE,
    STATUS_PENDING,
)
from .types import TapePart
from .utils import format_size, today_date

LOG = logging.getLogger("cryotape")


def catalog_row_for(part: TapePart, *, status: str, mount: Path,
                    write_date: str, volume_name: str = "") -> dict[str, str]:
    """生成单盘台账的字典行。

    Args:
        volume_name: 标签纸上标记的磁带卷名（用于跨会话识别同一盘物理磁带）。
                     缺省为空字符串（旧台账 / 未填写场景）。
    """
    return {
        "写入日期": write_date,
        "项目名称": part.project_name,
        "分卷编号": f"Part{part.part_index:02d}",
        "归档文件名": part.archive_name,
        "文件数量": str(part.file_count),
        "总体积": format_size(part.capacity_used_bytes),
        "起始文件路径": part.first_file,
        "终止文件路径": part.last_file,
        "磁带挂载点": str(mount),
        "状态": status,
        "磁带卷名": volume_name or "",
    }


class TapeCatalogWriter:
    """写盘内 Catalog_Part{nn}.csv（包含表头 + 一行）。"""

    @staticmethod
    def write(catalog_path: Path, part: TapePart, *, status: str,
              mount: Path, write_date: str,
              volume_name: str = "") -> None:
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        row = catalog_row_for(part, status=status, mount=mount,
                              write_date=write_date,
                              volume_name=volume_name)
        with catalog_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(CSV_HEADER))
            writer.writeheader()
            writer.writerow(row)
        LOG.info("盘内台账已写入: %s", catalog_path)


class LocalCatalog:
    """
    维护本地总 CSV 台账。每行代表一个分卷。
    状态机: pending → done / failed / aborted
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def read_all(self) -> list[dict[str, str]]:
        """读取所有行。若文件不存在返回空列表。"""
        if not self.path.exists():
            return []
        with self.path.open("r", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def append_pending(self, part: TapePart, *, mount: Path,
                       volume_name: str = "") -> None:
        """追加一行 status=pending。

        Args:
            volume_name: 磁带卷名（标签纸上的标识符），可后续通过
                ``update_volume_name`` 补全。
        """
        self._append_row(catalog_row_for(
            part, status=STATUS_PENDING, mount=mount, write_date=today_date(),
            volume_name=volume_name,
        ))

    def update_status(self, *, project: str, part_index: int,
                      new_status: str) -> None:
        """
        把 (project, Part{nn:02d}) 对应行的状态改为 new_status。
        通过重写整文件实现（本地 CSV 通常 < 10k 行，性能足够）。
        """
        if new_status not in ALL_STATUSES:
            raise ValueError(f"非法状态: {new_status}")
        if not self.path.exists():
            return
        target_key = f"Part{part_index:02d}"
        rows = self.read_all()
        changed = False
        for r in rows:
            if r.get("项目名称") == project and r.get("分卷编号") == target_key:
                if r.get("状态") != new_status:
                    r["状态"] = new_status
                    changed = True
        if changed:
            self._rewrite_all(rows)

    def scan_pending(self) -> list[dict[str, str]]:
        """返回所有 status=pending 的行。"""
        return [r for r in self.read_all() if r.get("状态") == STATUS_PENDING]

    def update_volume_name(self, *, project: str, part_index: int,
                           volume_name: str) -> bool:
        """补全 (project, Part{nn:02d}) 对应行的磁带卷名。

        用于在写盘前未能及时获得卷名（例如非交互脚本中由外部标签系统
        异步写入），但事后需要补全的场景。返回是否真的修改了行。
        """
        if not self.path.exists():
            return False
        target_key = f"Part{part_index:02d}"
        rows = self.read_all()
        changed = False
        for r in rows:
            if (r.get("项目名称") == project
                    and r.get("分卷编号") == target_key
                    and r.get("磁带卷名") != volume_name):
                r["磁带卷名"] = volume_name
                changed = True
        if changed:
            self._rewrite_all(rows)
        return changed

    def _append_row(self, row: dict[str, str]) -> None:
        new = not self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(CSV_HEADER))
            if new:
                writer.writeheader()
            writer.writerow(row)

    def _rewrite_all(self, rows) -> None:
        # 原子重写：写到同目录临时文件再 rename
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(CSV_HEADER))
            writer.writeheader()
            for r in rows:
                # 向后兼容：旧台账行缺少新增的「磁带卷名」键，
                # DictWriter 会为缺失字段写出空字符串，无需补键。
                writer.writerow(r)
        os.replace(tmp, self.path)