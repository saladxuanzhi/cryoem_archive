"""递归枚举项目目录的所有文件，按相对路径字典序排序。"""
from __future__ import annotations

import logging
from pathlib import Path

from .types import FileEntry

LOG = logging.getLogger("cryotape")


class FileDiscovery:
    """递归枚举目录下的所有文件，按相对路径字典序排序。"""

    def __init__(self, project_root: Path) -> None:
        self.root = project_root

    def discover(self) -> tuple[FileEntry, ...]:
        """
        返回已按 rel_path 字典序排序的 FileEntry 元组。
        跳过符号链接（避免循环）、隐藏文件（以 . 开头）。
        """
        entries: list[FileEntry] = []
        parent = self.root.parent
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            if any(part.startswith(".") for part in p.parts):
                continue
            # 跳过符号链接
            if p.is_symlink():
                continue
            try:
                size = p.stat().st_size
            except OSError as exc:
                LOG.warning("无法 stat 文件 %s: %s", p, exc)
                continue
            rel = p.relative_to(parent).as_posix()
            entries.append(FileEntry(rel_path=rel, abs_path=p, size_bytes=size))
        entries.sort(key=lambda e: e.rel_path)
        return tuple(entries)