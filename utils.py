"""Small helpers shared by main, archive, tape, catalog.

Nothing fancy. Anything that needs state lives in tape.py; anything that
needs to talk to SQLite lives in catalog.py. This module is for things
that have no home elsewhere: hashing, formatting, prompts, logging.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming chunk


# ---- logging -----------------------------------------------------------------


def setup_logging(logfile: str) -> logging.Logger:
    """Configure root logger: file + stderr, both with timestamps.

    Returns the application logger.
    """
    logger = logging.getLogger("cryoem_archive")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    try:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        # If we can't open the log file, log to stderr only. Don't crash.
        pass

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ---- hashing -----------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """SHA256 of a file, as lowercase hex."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK_SIZE)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---- formatting --------------------------------------------------------------


def format_bytes(n: int) -> str:
    """Format byte count as a human-readable string (decimal units).

    十进制单位（1 KB = 1000 B）：与项目所有容量常量（LTO6_RAW_BYTES、
    CHUNK_MULTIPLE_BYTES、SAFE_MARGIN_BYTES…）以及磁带厂商的标称容量同一
    口径。曾用二进制单位（KiB/GiB），操作员拿显示值对磁带容量时会差 ~10%。
    """
    n = int(n)
    if n < 0:
        raise ValueError(f"size must be non-negative, got {n}")
    if n < 1000:
        return f"{n} B"
    units = ("KB", "MB", "GB", "TB", "PB")
    v = float(n)
    for unit in units:
        v /= 1000.0
        if v < 1000.0:
            return f"{v:.2f} {unit}"
    return f"{v:.2f} PB"


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


# ---- subprocess --------------------------------------------------------------


def ensure_tool(name: str) -> str:
    """Return absolute path to ``name`` or raise."""
    from shutil import which

    path = which(name)
    if path is None:
        raise RuntimeError(f"required tool not found on PATH: {name!r}")
    return path


# ---- interactive prompts -----------------------------------------------------


def prompt(message: str, default: str = "") -> str:
    """Print a prompt, read a line, return the answer (or default)."""
    suffix = f" [{default}]" if default else ""
    sys.stdout.write(f"{message}{suffix}: ")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    answer = line.rstrip("\n").rstrip("\r").strip()
    if not answer and default:
        return default
    return answer


def confirm(message: str, default: bool = False) -> bool:
    """Ask a yes/no question. Empty answer returns ``default``."""
    suffix = " [Y/n]" if default else " [y/N]"
    sys.stdout.write(f"{message}{suffix}: ")
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    answer = line.strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def press_enter(message: str = "按 Enter 继续...") -> None:
    """Block until the operator presses Enter."""
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()
    sys.stdin.readline()


# ---- misc --------------------------------------------------------------------


# ---- progress bars ----------------------------------------------------------


def progress(label: str, done: int, total: int, *, width: int = 30) -> None:
    """Write a single-line progress bar to stderr.

    Use for long-running operations where the total is known up front
    (e.g. hashing N files, writing N archives to tape).
    """
    if total <= 0:
        return
    pct = done / total * 100.0
    filled = int(pct / 100.0 * width)
    bar = "=" * filled + " " * (width - filled)
    sys.stderr.write(f"\r  {label}: [{bar}] {pct:5.1f}% ({done}/{total})")
    sys.stderr.flush()


def progress_done(label: str = "") -> None:
    """Finish a progress line: newline + clear the bar."""
    if label:
        sys.stderr.write(f"\r  ✓ {label}" + " " * 40 + "\n")
    else:
        sys.stderr.write("\n")
    sys.stderr.flush()


class ByteProgress:
    """Single-line byte-based progress bar with speed and ETA.

    Usage::

        bar = ByteProgress("压缩", total_bytes)
        for chunk in stream:
            bar.update(len(chunk))
        bar.done()

    Speed is averaged over the elapsed wall time. ETA is
    ``remaining / speed`` with a one-second floor so we never show
    ``ETA 0s`` while there's still data to go.
    """

    def __init__(self, label: str, total: int, *, width: int = 30) -> None:
        if total < 0:
            raise ValueError(f"total must be non-negative, got {total}")
        self.label = label
        self.total = total
        self.width = width
        self.start = time.monotonic()
        self.last_redraw = self.start
        self._done = 0

    def update(self, done: int) -> None:
        """Set the absolute ``done`` count. Redraws at most ~4×/sec."""
        self._done = max(0, min(done, self.total))
        now = time.monotonic()
        # Throttle redraws to ~4/sec unless the operation just finished.
        if now - self.last_redraw < 0.25 and self._done < self.total:
            return
        self.last_redraw = now
        self._draw()

    def add(self, n: int) -> None:
        """Add ``n`` to the current ``done`` count."""
        self.update(self._done + n)

    def done(self, label: str = "", *, final: int | None = None) -> None:
        """Mark finished; print a final line.

        ``final`` is the true byte count. Pass it whenever the total was
        only an estimate — otherwise the bar reports a tidy 100% of a
        number that was never reached, which hides exactly the kind of
        failure a progress bar exists to surface.
        """
        if final is None:
            self._done = self.total
        else:
            self._done = max(0, final)
            self.total = max(self.total, self._done)
        self._draw()
        suffix = f"  {label}" if label else ""
        sys.stderr.write(f"{suffix}\n")
        sys.stderr.flush()

    def _draw(self) -> None:
        elapsed = max(time.monotonic() - self.start, 1e-6)
        if self.total == 0:
            pct, frac = 100.0, 1.0
        else:
            pct = self._done / self.total * 100.0
            frac = pct / 100.0
        filled = int(frac * self.width)
        bar = "=" * filled + " " * (self.width - filled)
        speed = self._done / elapsed if elapsed > 0 else 0.0
        remaining = self.total - self._done
        eta = remaining / speed if speed > 0 else 0.0
        speed_str = f"{format_bytes(int(speed))}/s"
        eta_str = _format_eta(eta)
        sys.stderr.write(
            f"\r  {self.label}: [{bar}] {pct:5.1f}% "
            f"{format_bytes(self._done)}/{format_bytes(self.total)}  "
            f"{speed_str}  ETA {eta_str}"
        )
        sys.stderr.flush()


def _format_eta(seconds: float) -> str:
    """Format a duration as a compact ETA string."""
    if seconds < 0 or seconds != seconds:  # NaN
        return "?"
    seconds = max(seconds, 1.0)  # never show 0s while there's still work
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"
