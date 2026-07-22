"""贪心装箱：按字典序累积，绝不允许单文件切分。"""
from __future__ import annotations

from typing import Sequence

from .exceptions import OversizedFileError
from .types import FileEntry, TapePart


class TapePacker:
    """贪心按字典序装箱。绝不允许单文件切分。"""

    def __init__(self, cap_bytes: int) -> None:
        if cap_bytes <= 0:
            raise ValueError("cap_bytes 必须为正")
        self.cap_bytes = cap_bytes

    def pack(self, project_name: str, files: Sequence[FileEntry]) -> list[TapePart]:
        """
        返回该 project 的分卷列表。每个 part 内文件按字典序连续。
        """
        parts: list[list[FileEntry]] = []
        current: list[FileEntry] = []
        current_size = 0

        for f in files:
            if f.size_bytes > self.cap_bytes:
                # 单文件超过容量：不允许切分
                raise OversizedFileError(
                    f"文件 {f.rel_path} 大小 {f.size_bytes} 字节 "
                    f"超过本盘容量上限 {self.cap_bytes} 字节。"
                    f"无法在不切分的前提下归档，请人工切分项目。"
                )
            if current_size + f.size_bytes > self.cap_bytes and current:
                parts.append(current)
                current = []
                current_size = 0
            current.append(f)
            current_size += f.size_bytes
        if current:
            parts.append(current)

        return [
            TapePart(
                project_name=project_name,
                part_index=i + 1,
                files=tuple(p),
                capacity_used_bytes=sum(x.size_bytes for x in p),
                capacity_cap_bytes=self.cap_bytes,
            )
            for i, p in enumerate(parts)
        ]