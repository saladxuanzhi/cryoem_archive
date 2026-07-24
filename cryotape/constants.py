"""常量：文件后缀白名单、默认值、状态枚举。"""
from __future__ import annotations

from pathlib import Path

# 冷冻电镜常见图像后缀（小写）
CRYO_EXTS: frozenset[str] = frozenset({
    ".eer",     # EER format (Falcon / K3)
    ".mrc",     # MRC
    ".mrcs",    # MRCS (stacks)
    ".tif",     # TIFF
    ".tiff",    # TIFF (long extension)
    ".gain",    # gain reference
    # ".dm4",     # Digital Micrograph 4
    # ".dm3",     # Digital Micrograph 3
})

# LTO-6 标称容量 2.5 TB，扣除索引与安全缓冲后默认 2.25 TB
DEFAULT_NEW_TAPE_CAPACITY_GB: float = 2250.0
DEFAULT_SAFETY_MARGIN_GB: float = 15.0
# 跨 project 共享磁带空间时的最小剩余阈值
DEFAULT_MIN_TAIL_GB: float = 100.0
# 项目完整度优先：True=不跨盘拆分项目；False=默认节省空间优先
DEFAULT_INTEGRITY_PRIORITY: bool = False

# 默认 LTFS 挂载点与本地 CSV 路径
DEFAULT_LTFS_MOUNT: str = "/mnt/ltfs"
DEFAULT_CSV_CATALOG: str = str(Path.home() / "cryotape_catalog.csv")
DEFAULT_LOG_DIR: str = "./logs"

# 状态机枚举
STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_ABORTED = "aborted"
ALL_STATUSES: tuple[str, ...] = (
    STATUS_PENDING, STATUS_DONE, STATUS_FAILED, STATUS_ABORTED,
)

# CSV 表头（本地总 CSV 与盘内 CSV 共用）。
# 末尾追加「磁带卷名」字段：标签纸标记的物理磁带标识符，用于
# 跨盘 / 跨会话识别同一盘磁带。该字段向后兼容（读取旧 CSV 时
# DictReader 返回 None，写入时 DictWriter 留空）。
CSV_HEADER: tuple[str, ...] = (
    "写入日期", "项目名称", "分卷编号", "归档文件名", "文件数量",
    "总体积", "起始文件路径", "终止文件路径", "磁带挂载点", "状态",
    "磁带卷名",
)