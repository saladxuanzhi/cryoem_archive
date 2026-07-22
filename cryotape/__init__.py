"""CryoTape — 冷冻电镜数据归档到 LTO-6 LTFS 磁带。"""
from __future__ import annotations

from .constants import (
    ALL_STATUSES,
    CRYO_EXTS,
    CSV_HEADER,
    DEFAULT_CSV_CATALOG,
    DEFAULT_LOG_DIR,
    DEFAULT_LTFS_MOUNT,
    DEFAULT_NEW_TAPE_CAPACITY_GB,
    DEFAULT_SAFETY_MARGIN_GB,
    STATUS_ABORTED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
)
from .exceptions import (
    CryoTapeError,
    InsufficientSpaceError,
    InvalidPathError,
    OversizedFileError,
    ProjectDetectionError,
    TapeNotMountedError,
    TapeWriteError,
    TarNotFoundError,
    UserAbortedError,
)
from .interactive import InteractiveConfigurator, RuntimeConfig
from .types import (
    CapacityPlan,
    FileEntry,
    ProjectInfo,
    TapePart,
)
from .workflow import Workflow

__version__ = "2.1.0"

__all__ = [
    # 版本
    "__version__",
    # 常量
    "ALL_STATUSES", "CRYO_EXTS", "CSV_HEADER",
    "DEFAULT_CSV_CATALOG", "DEFAULT_LOG_DIR", "DEFAULT_LTFS_MOUNT",
    "DEFAULT_NEW_TAPE_CAPACITY_GB", "DEFAULT_SAFETY_MARGIN_GB",
    "STATUS_ABORTED", "STATUS_DONE", "STATUS_FAILED", "STATUS_PENDING",
    # 异常
    "CryoTapeError", "InsufficientSpaceError", "InvalidPathError",
    "OversizedFileError", "ProjectDetectionError", "TapeNotMountedError",
    "TapeWriteError", "TarNotFoundError", "UserAbortedError",
    # 数据类型
    "CapacityPlan", "FileEntry", "ProjectInfo", "TapePart",
    # 组件
    "InteractiveConfigurator", "RuntimeConfig", "Workflow",
]