"""流式 tar 写入磁带：零压缩、零跨盘切分、单原子临时文件。"""
from __future__ import annotations

import atexit
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from .exceptions import TapeWriteError
from .progress import ProgressBar
from .types import FileEntry
from .utils import format_size, now_iso
from .validator import PathValidator

LOG = logging.getLogger("cryotape")


class _TempFileRegistry:
    """注册需要清理的临时文件路径，进程退出时兜底清理。"""

    def __init__(self) -> None:
        self._paths: set[Path] = set()
        atexit.register(self.cleanup_all)

    def register(self, path: Path) -> None:
        self._paths.add(path)

    def unregister(self, path: Path) -> None:
        self._paths.discard(path)

    def cleanup_all(self) -> None:
        for p in list(self._paths):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
            self._paths.discard(p)


_TEMP_REGISTRY = _TempFileRegistry()


class TarStreamer:
    """
    通过 tar -C <parent> -T <list> -cf <archive> 流式写入磁带挂载点。
    严格遵守：零 CPU 压缩、零跨盘切分、单原子临时文件。
    """

    def __init__(self, *, parent_dir: Path, archive_path: Path,
                 log_path: Path, verbose: bool,
                 show_progress: bool = True,
                 progress_label: str = "") -> None:
        self.parent_dir = parent_dir
        self.archive_path = archive_path
        self.log_path = log_path
        self.verbose = verbose
        self.show_progress = show_progress
        self.progress_label = progress_label
        # 仅做存在性预检；实际命令使用裸 'tar' 避免 Windows tar.EXE 解析 bug
        PathValidator.check_tar_available()

    def stream(self, files: Sequence[FileEntry]) -> None:
        """
        把 files 流式打包到 self.archive_path。
        流程：
            1. 创建临时文件列表（KB 级）
            2. tar -cf <archive> -C <parent> -T <list>
            3. 删除临时文件列表
        """
        if not files:
            raise ValueError("files 不能为空")

        # 1. 写临时文件列表
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".cryotape.lst",
            prefix=f"{self.archive_path.stem}_",
            dir=tempfile.gettempdir(),
            delete=False,
            encoding="utf-8",
        ) as lst:
            for f in files:
                lst.write(f.rel_path + "\n")
            lst_path = Path(lst.name)
        _TEMP_REGISTRY.register(lst_path)

        # 计算本 part 总体积（作为进度条分母）
        total_bytes = sum(f.size_bytes for f in files)

        try:
            self._run_tar(lst_path, total_bytes=total_bytes)
        finally:
            # 3. 清理临时列表
            try:
                lst_path.unlink(missing_ok=True)
            finally:
                _TEMP_REGISTRY.unregister(lst_path)

    def _run_tar(self, list_path: Path, *, total_bytes: int = 0) -> None:
        # 确保父目录存在（磁带根目录理论上已存在，但日志目录需要创建）
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        # 在 Windows 上 Git Bash 自带的 tar 不能识别反斜杠路径，
        # 必须把所有传给 tar 的路径转换为 POSIX 风格（正斜杠）。
        # tar 的 list 内容里写的 rel_path 本身已经是 POSIX 格式。
        #
        # 关键坑：Windows 下若用 tar.EXE 的绝对路径（如
        # `C:\Program Files\Git\usr\bin\tar.EXE`）调用，它会把参数中的
        # `C:` 当成远程主机语法（host:path），从而报
        # "Cannot connect to C: resolve failed"。
        # 因此统一使用裸命令名 `tar`，让 subprocess 走 PATH 解析。
        archive_arg = self.archive_path.as_posix()
        parent_arg = self.parent_dir.as_posix()
        list_arg = list_path.as_posix()

        cmd = [
            "tar",
            "-cf", archive_arg,
            "-C", parent_arg,
            "-T", list_arg,
            # 严禁：-z / -j / -J / -M / --multi-volume / --newer 等
        ]

        LOG.info("执行: %s", " ".join(cmd))

        # 启动进度条（非 TTY 时自动空操作）
        progress = ProgressBar(
            total_bytes=total_bytes,
            enabled=self.show_progress,
            label=self.progress_label or self.archive_path.name,
        )
        progress.start(self.archive_path)

        try:
            with self.log_path.open("w", encoding="utf-8", errors="replace") as logf:
                logf.write(f"# CryoTape tar log\n# command: {' '.join(cmd)}\n# started: {now_iso()}\n\n")
                logf.flush()
                if self.verbose:
                    # 实时把 tar 输出同时写到日志与终端
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        bufsize=1,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                        logf.write(line)
                        logf.flush()
                    ret = proc.wait()
                else:
                    proc = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        check=False,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    ret = proc.returncode
                    logf.write(proc.stdout)
                logf.write(f"\n# finished: {now_iso()}\n# returncode: {ret}\n")
        finally:
            progress.stop()

        if ret != 0:
            raise TapeWriteError(
                f"tar 写入失败 (returncode={ret})，详细日志: {self.log_path}"
            )

    def stream_manifest_file(self, manifest_path: Path, files: Sequence[FileEntry],
                             total_size: int) -> None:
        """把 Manifest 文件直接写入磁带根目录（不是 tar 的一部分）。"""
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with manifest_path.open("w", encoding="utf-8") as f:
            f.write("# CryoTape Manifest\n")
            f.write(f"# archive: {self.archive_path.name}\n")
            f.write(f"# generated: {now_iso()}\n")
            f.write(f"# file_count: {len(files)}\n")
            f.write(f"# total_size: {format_size(total_size)}\n")
            f.write(f"# parent_dir: {self.parent_dir}\n")
            f.write("# ----- file list (sorted) -----\n")
            for fe in files:
                f.write(fe.rel_path + "\n")
        LOG.info("Manifest 已写入: %s", manifest_path)