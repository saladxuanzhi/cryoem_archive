"""核心数据结构（dataclass）。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileEntry:
    """单个待归档文件的元信息。"""
    rel_path: str       # 相对父目录的 POSIX 路径（tar 内将保留此形式）
    abs_path: Path      # 绝对路径，用于 tar -T
    size_bytes: int     # 文件字节数

    @property
    def name(self) -> str:
        """文件名（不含父目录）。"""
        return self.abs_path.name


@dataclass(frozen=True)
class ProjectInfo:
    """一个独立项目的元信息。"""
    name: str                    # 项目名，取自目录名
    root: Path                   # 项目根目录（可能等于 parent_dir，单项目时）
    parent_dir: Path             # 用于 tar -C 的公共父目录
    files: tuple[FileEntry, ...]  # 已字典序排序

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def file_count(self) -> int:
        return len(self.files)


@dataclass(frozen=True)
class CapacityPlan:
    """容量规划结果。"""
    cap_bytes: int           # 当前可写入上限
    source: str              # "fresh_tape" 或 "existing_tape"
    free_bytes: int          # 挂载点当前剩余
    used_bytes: int          # 挂载点当前已用
    margin_bytes: int        # 安全余量
    new_tape_capacity_bytes: int  # 标准空磁带容量（用于报告）

    @property
    def headroom_ratio(self) -> float:
        if self.cap_bytes <= 0:
            return 0.0
        return self.margin_bytes / (self.cap_bytes + self.margin_bytes)


@dataclass(frozen=True)
class TapePart:
    """一个分卷的完整规划。"""
    project_name: str
    part_index: int          # 1-based
    files: tuple[FileEntry, ...]
    capacity_used_bytes: int
    capacity_cap_bytes: int

    @property
    def archive_name(self) -> str:
        return f"{self.project_name}_Part{self.part_index:02d}.tar"

    @property
    def manifest_name(self) -> str:
        return f"Manifest_Part{self.part_index:02d}.txt"

    @property
    def catalog_name(self) -> str:
        return f"Catalog_Part{self.part_index:02d}.csv"

    @property
    def first_file(self) -> str:
        return self.files[0].rel_path if self.files else ""

    @property
    def last_file(self) -> str:
        return self.files[-1].rel_path if self.files else ""

    @property
    def file_count(self) -> int:
        return len(self.files)