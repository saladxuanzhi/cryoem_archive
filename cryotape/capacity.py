"""动态容量识别：读取挂载点剩余空间，区分空磁带 vs 已有数据。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .exceptions import InsufficientSpaceError
from .types import CapacityPlan
from .utils import format_size

LOG = logging.getLogger("cryotape")


class TapeCapacityPlanner:
    """读取挂载点剩余空间，区分已有数据 vs 空磁带，规划本盘容量。"""

    def __init__(self, mount: Path, *,
                 new_tape_capacity_bytes: int,
                 safety_margin_bytes: int) -> None:
        self.mount = mount
        self.new_tape_capacity_bytes = new_tape_capacity_bytes
        self.safety_margin_bytes = safety_margin_bytes

    def plan(self) -> CapacityPlan:
        usage = shutil.disk_usage(self.mount)
        used = usage.used
        free = usage.free
        if used > 0:
            cap = free - self.safety_margin_bytes
            source = "existing_tape"
            if cap <= 0:
                raise InsufficientSpaceError(
                    f"挂载点 {self.mount} 已有数据，但剩余空间 {format_size(free)} "
                    f"不足以覆盖安全余量 {format_size(self.safety_margin_bytes)}。"
                )
        else:
            cap = self.new_tape_capacity_bytes - self.safety_margin_bytes
            source = "fresh_tape"
            if cap <= 0:
                raise InsufficientSpaceError(
                    f"标准空磁带容量 {format_size(self.new_tape_capacity_bytes)} "
                    f"小于安全余量 {format_size(self.safety_margin_bytes)}。"
                )
        LOG.info(
            "容量规划: source=%s, free=%s, cap=%s (margin=%s)",
            source, format_size(free), format_size(cap),
            format_size(self.safety_margin_bytes),
        )
        return CapacityPlan(
            cap_bytes=cap,
            source=source,
            free_bytes=free,
            used_bytes=used,
            margin_bytes=self.safety_margin_bytes,
            new_tape_capacity_bytes=self.new_tape_capacity_bytes,
        )