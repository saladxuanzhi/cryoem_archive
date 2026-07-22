"""CryoTape 自定义异常体系。"""
from __future__ import annotations


class CryoTapeError(Exception):
    """CryoTape 所有异常的基类。"""


class InvalidPathError(CryoTapeError):
    """路径不存在或不是预期的类型（目录 / 挂载点）。"""


class TarNotFoundError(CryoTapeError):
    """系统中找不到 tar 可执行文件。"""


class InsufficientSpaceError(CryoTapeError):
    """磁带剩余空间减去安全余量后不足以容纳任何分卷。"""


class OversizedFileError(CryoTapeError):
    """单个文件超过磁带容量上限（违反不切分约束）。"""


class ProjectDetectionError(CryoTapeError):
    """既不是单项目也没有任何候选子项目。"""


class TapeNotMountedError(CryoTapeError):
    """LTFS 挂载点不可写或未挂载。"""


class TapeWriteError(CryoTapeError):
    """tar 子进程非 0 退出或 stdout/stderr 提示致命错误。"""


class UserAbortedError(CryoTapeError):
    """用户主动中止（Ctrl-C 或输入取消指令）。"""