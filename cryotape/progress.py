"""磁带写入进度条（纯标准库，无第三方依赖）。

原理：
    tar 流式写入时，磁带根目录下的 .tar 文件会持续增长。
    后台线程按固定间隔轮询文件大小，把「已写入字节数 / 总体积」
    渲染成单行 ASCII 进度条。stderr 非 TTY 时自动禁用刷新。

特性：
    - 渲染格式: `[████░░░░] 42.5%  1.06GB/2.50GB  15.2MB/s  ETA 1m32s`
    - 线程安全：通过 ``threading.Lock`` 保护共享计数
    - 自动退出：``stop()`` 后线程自动结束，不会泄漏
    - 与 verbose 模式不冲突：本进度条总是输出到 stderr
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional, TextIO


def _fmt_size(n: float) -> str:
    """字节数 → 人类可读字符串（1024 进制，与 df -h 一致）。"""
    if n < 0:
        return "-" + _fmt_size(-n)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    i = 0
    v = float(n)
    while v >= 1024.0 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    if i == 0:
        return f"{int(v)} {units[i]}"
    return f"{v:.2f} {units[i]}"


def _fmt_eta(seconds: float) -> str:
    """秒数 → ETA 字符串 (Ns / NmSs /NhNNm)。"""
    # NaN / 负数表示不可估算
    if seconds != seconds or seconds < 0:
        return "--:--"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


class ProgressBar:
    """轮询文件大小驱动的 ASCII 进度条。

    使用方式::

        pb = ProgressBar(total_bytes=part.capacity_used_bytes)
        pb.start(archive_path)
        try:
            ...  # 执行 tar 子进程
        finally:
            pb.stop()

    非 TTY 场景下（管道 / CI / 重定向）``start()`` 是空操作，
    不会输出任何字节，也不会启动后台线程。
    """

    def __init__(
        self,
        total_bytes: int,
        *,
        width: int = 36,
        out: Optional[TextIO] = None,
        enabled: bool = True,
        poll_interval: float = 0.5,
        label: str = "",
    ) -> None:
        self.total_bytes = max(int(total_bytes), 1)
        self.width = max(int(width), 10)
        self.out: TextIO = out if out is not None else sys.stderr
        # 仅当 enabled 且输出目标是 TTY 时才真的渲染
        self.enabled = bool(enabled) and self._target_isatty()
        self.poll_interval = max(float(poll_interval), 0.05)
        self.label = label
        self._path: Optional[Path] = None
        self._start_time = 0.0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_bytes = 0
        self._lock = threading.Lock()
        self._finalized = False

    def _target_isatty(self) -> bool:
        """检查 ``self.out`` 是否是 TTY。``sys.stderr`` 之外的流也支持。"""
        try:
            return bool(self.out.isatty())
        except (AttributeError, ValueError):
            return False

    def start(self, path: Path) -> None:
        """开始监控指定文件的增长。必须在工作线程启动之前调用。"""
        if not self.enabled:
            return
        self._path = Path(path)
        self._start_time = time.monotonic()
        self._stop_event.clear()
        self._finalized = False
        self._thread = threading.Thread(
            target=self._run, name="CryoTape-ProgressBar", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """停止监控并刷新最后一行（包含 100% 完成态）。"""
        if not self.enabled:
            return
        # 先把当前最新大小吸进来，再渲染最终状态
        if self._path is not None:
            try:
                self._last_bytes = self._path.stat().st_size
            except OSError:
                pass
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_interval * 3)
            self._thread = None
        self._render(final=True)

    # ---------- 内部 ----------

    def _run(self) -> None:
        assert self._path is not None
        while not self._stop_event.wait(self.poll_interval):
            try:
                size = self._path.stat().st_size
            except OSError:
                continue
            with self._lock:
                self._last_bytes = size
            self._render(final=False)

    def _render(self, *, final: bool) -> None:
        with self._lock:
            written = self._last_bytes
            elapsed = max(time.monotonic() - self._start_time, 1e-6)
        ratio = min(written / self.total_bytes, 1.0) if self.total_bytes else 1.0
        speed = written / elapsed if elapsed > 0 else 0.0
        if speed > 0 and written < self.total_bytes:
            eta_s = (self.total_bytes - written) / speed
        else:
            eta_s = 0.0

        filled = int(self.width * ratio)
        # 避免越界：满格时不再放 '>'
        if filled >= self.width:
            bar = "█" * self.width
        elif filled == 0:
            bar = "·" + " " * (self.width - 1)
        else:
            bar = "█" * filled + "▏" + " " * (self.width - filled - 1)

        prefix = f"[{self.label}] " if self.label else "  "
        line = (
            f"\r{prefix}[{bar}] {ratio * 100:5.1f}%  "
            f"{_fmt_size(written)} / {_fmt_size(self.total_bytes)}  "
            f"{_fmt_size(speed)}/s  ETA {_fmt_eta(eta_s)}"
        )
        try:
            self.out.write(line)
            self.out.flush()
        except (OSError, ValueError):
            pass
        if final and not self._finalized:
            self._finalized = True
            try:
                self.out.write("\n")
                self.out.flush()
            except (OSError, ValueError):
                pass