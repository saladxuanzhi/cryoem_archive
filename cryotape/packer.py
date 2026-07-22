"""
装箱策略：

TapePacker (per-project)
    简单的贪心装箱：给定一个 project 的文件列表，按字典序累积，超过 cap 就开新 part。
    一个 project 内部可以切分到多个 part（每个 part 在不同磁带上），但 part 内文件必须连续。

GlobalPacker (cross-project)
    把多个 project 的文件当作一个大的有序序列，按字典序/项目顺序贪心装箱。
    关键约束：
        - 每个 Part 只属于一个 project（保持 {project}_Part{nn}.tar 命名）
        - 跨 project 共享磁带空间：写完一个 project 后，若剩余 > min_tail_bytes
          且下一个 project 能整体放下，则不开新磁带
        - 若下一个 project 不能整体放下，开新磁带
        - 单 project 内仍按 TapePacker 规则切分
        - 单文件 > cap_bytes 抛 OversizedFileError（不切分）
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from .exceptions import OversizedFileError
from .types import FileEntry, ProjectInfo, TapePart

LOG = logging.getLogger("cryotape")


class TapePacker:
    """贪心按字典序装箱（per-project）。绝不允许单文件切分。"""

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


class GlobalPacker:
    """
    跨 project 的全局装箱器。

    输入：projects 列表（每个含已排序的 files）。
    输出：TapePart 列表，按写入顺序排列。**多个 Part 可共享同一盘磁带**。

    两种策略：
        1. **节省空间优先**（默认，integrity_priority=False）：
           下一个 project 整体放不下当前磁带剩余空间时，自动拆分
           为多 Part 跨磁带（例如：C_Part01 在 Tape01 末尾，
           C_Part02 在 Tape02 开头）。最大化磁带利用率。

        2. **项目完整度优先**（integrity_priority=True）：
           一个 project 只能整体放入一盘磁带。若当前磁带剩余空间放不下
           下一个 project 整体，则开新磁带给该 project。同一 project
           不会被拆分到两盘（除非 project > cap_bytes 不得不拆）。

    共同规则：
        1. 项目按输入顺序处理（每个 project 内部文件已按字典序排序）
        2. 单 Part 永远属于一个 project（保持 {project}_Part{nn}.tar 命名）
        3. 切换 project 时关闭当前 Part
        4. 剩余空间 < min_tail_bytes 时必须开新磁带（两种策略都一样）
        5. 单 project 内若 file > cap_bytes → OversizedFileError
    """

    def __init__(self, cap_bytes: int, min_tail_bytes: int = 0, *,
                 integrity_priority: bool = False) -> None:
        if cap_bytes <= 0:
            raise ValueError("cap_bytes 必须为正")
        if min_tail_bytes < 0:
            raise ValueError("min_tail_bytes 必须非负")
        if min_tail_bytes >= cap_bytes:
            raise ValueError(
                f"min_tail_bytes ({min_tail_bytes}) 必须小于 cap_bytes ({cap_bytes})"
            )
        self.cap_bytes = cap_bytes
        self.min_tail_bytes = min_tail_bytes
        self.integrity_priority = integrity_priority

    def pack(self, projects: Sequence[ProjectInfo]) -> list[TapePart]:
        """
        跨 project 全局装箱。返回 TapePart 列表，按写入顺序排列。
        """
        if not projects:
            return []

        parts: list[TapePart] = []
        # 每个 project 的下一个 part 编号（1-based）
        part_counter: dict[str, int] = {p.name: 1 for p in projects}

        # 当前物理磁带已用空间
        tape_used = 0

        # 当前正在填充的 Part 状态
        cur_files: list[FileEntry] = []
        cur_size = 0
        cur_project: Optional[str] = None

        def commit_part() -> None:
            """把当前 Part 加入 parts，重置 cur 状态。"""
            nonlocal tape_used, cur_size, cur_project
            if not cur_files:
                return
            idx = part_counter[cur_project]
            parts.append(TapePart(
                project_name=cur_project,
                part_index=idx,
                files=tuple(cur_files),
                capacity_used_bytes=cur_size,
                capacity_cap_bytes=self.cap_bytes,
            ))
            part_counter[cur_project] = idx + 1
            tape_used += cur_size
            cur_files.clear()
            cur_size = 0
            cur_project = None

        for project in projects:
            # 切换 project：先关闭当前 Part
            if cur_project is not None and cur_project != project.name:
                commit_part()
                remaining = self.cap_bytes - tape_used
                if remaining < self.min_tail_bytes:
                    # 剩余空间太少，开新磁带
                    tape_used = 0
                elif self.integrity_priority and project.total_bytes > remaining:
                    # 完整度优先：下一个 project 放不下整体 → 开新磁带
                    tape_used = 0
                # 否则（空间优先）：让内层循环自然拆分下一个 project

            # 若没有正在填充的 Part，开始新 Part
            if cur_project is None:
                cur_project = project.name

            # 当前 project 的所有文件贪心加入
            for file in project.files:
                # 检查单文件是否超过单盘容量
                if file.size_bytes > self.cap_bytes:
                    raise OversizedFileError(
                        f"文件 {file.rel_path} 大小 {file.size_bytes} 字节 "
                        f"超过本盘容量上限 {self.cap_bytes} 字节。"
                        f"无法在不切分的前提下归档，请人工切分项目。"
                    )
                # 检查：当前 tape (tape_used + cur_size) 装不下此文件？
                if tape_used + cur_size + file.size_bytes > self.cap_bytes:
                    # 关闭当前 Part
                    if cur_size == 0:
                        # 防御性：cur 空但加不进，说明此文件单独都放不下当前 tape 剩余
                        raise OversizedFileError(
                            f"文件 {file.rel_path} 无法放入空 Part"
                        )
                    commit_part()
                    # 检查：当前磁带的剩余空间是否能放下此文件？
                    if file.size_bytes + tape_used > self.cap_bytes:
                        # 当前磁带剩余空间不够，必须开新磁带
                        tape_used = 0
                    # 否则继续用同一盘磁带
                    cur_project = project.name
                cur_files.append(file)
                cur_size += file.size_bytes

        # 收尾
        commit_part()
        return parts

    def tape_assignment(self, parts: Sequence[TapePart]) -> list[int]:
        """
        给定 pack() 产出的 parts 列表，返回每个 part 所在的物理磁带编号（1-based）。
        连续相邻的 parts 若能放在同一磁带（remaining >= min_tail），则同号。

        用途：让 Workflow 在 part 边界处知道是否需要提示用户换磁带。
        """
        if not parts:
            return []

        tape_idx: list[int] = []
        current_tape = 1
        tape_used = 0
        prev_project: Optional[str] = None

        for part in parts:
            # 跨 project 时：检查剩余空间
            if prev_project is not None and prev_project != part.project_name:
                remaining = self.cap_bytes - tape_used
                if remaining < self.min_tail_bytes:
                    current_tape += 1
                    tape_used = 0
                elif self.integrity_priority and part.capacity_used_bytes > remaining:
                    # 完整度优先：跨 project 时若当前 part 放不下剩余空间 → 新磁带
                    current_tape += 1
                    tape_used = 0
            # 单 part 大于当前磁带剩余 → 开新磁带（同一 project 跨磁带的情况）
            if part.capacity_used_bytes > self.cap_bytes - tape_used:
                current_tape += 1
                tape_used = 0
            tape_idx.append(current_tape)
            tape_used += part.capacity_used_bytes
            prev_project = part.project_name
        return tape_idx