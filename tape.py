"""LTFS tape access.

The tape is mounted as a filesystem (LTFS) and each archive is a regular
file at the mount root: ``<mount>/<archive_name>.tar.zst``. There is no
per-tape subdirectory — when the operator swaps tapes they unmount the old
one and mount the new one at the same path.

Consequences of LTFS being a *sequential-write* filesystem, which shape
everything below:

* ``unlink`` frees the index entry, not the medium. Space consumed by a
  partial write is never reclaimed, so the only real defence against a
  full tape is refusing to start a write that might not fit.
* Positioning (``mt rewind`` / ``fsf``) is the driver's problem, not ours.
  Archives are addressed by filename.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import shutil
from dataclasses import dataclass
from collections.abc import Iterator
from pathlib import Path
from typing import IO

from utils import prompt

_LOGGER = logging.getLogger("cryoem_archive")


class TapeFullError(RuntimeError):
    """The tape ran out of space mid-write.

    Distinct from a generic write failure: the caller can recover by
    swapping tapes and retrying the same archive. The partial file has
    already been unlinked by the time this is raised — but that does not
    give the space back (see the module docstring), so this exception
    means part of the tape is permanently lost. Treat it as a bug in the
    pre-write capacity check, not as normal flow control.
    """


def _is_enospc(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and exc.errno == errno.ENOSPC


@dataclass
class TapeDevice:
    """An LTFS mount point.

    Args:
        mount: Directory where the tape is mounted. Archives are written
            as files directly under it.
    """

    mount: str

    def _mount_path(self) -> Path:
        p = Path(self.mount)
        if not p.is_dir():
            raise RuntimeError(f"LTFS 挂载点不存在或不是目录：{p}")
        return p

    def free_bytes(self) -> int | None:
        """Remaining space on the loaded tape, or ``None`` if unreadable.

        LTFS presents the tape as a filesystem, so ``statvfs`` gives a real
        (if drive-estimated) number.
        """
        try:
            return shutil.disk_usage(self.mount).free
        except OSError as exc:
            _LOGGER.warning("cannot stat LTFS mount %s: %s", self.mount, exc)
            return None

    @contextlib.contextmanager
    def archive_writer(
        self, archive_name: str, *, tape_label: str
    ) -> Iterator[IO[bytes]]:
        """Open a byte sink writing ``<mount>/<archive_name>``.

        Yields a writable binary stream. The caller pumps the compressed
        archive into it; nothing is buffered on local disk. On a clean exit
        the data is ``fsync``ed to the medium. On any exception the partial
        file is unlinked — a truncated archive that looks restorable is
        worse than a missing one — though that only reclaims the index
        entry, not the tape.

        Raises:
            TapeFullError: The tape filled up mid-write.
        """
        target = self._mount_path() / archive_name
        _LOGGER.info("LTFS: streaming -> %s", target)
        fh = open(target, "wb")
        try:
            yield fh
            fh.flush()
            os.fsync(fh.fileno())
        except BaseException as exc:
            fh.close()
            try:
                target.unlink()
                _LOGGER.info("removed incomplete archive %s", target)
            except OSError as unlink_exc:
                _LOGGER.warning("could not remove %s: %s", target, unlink_exc)
            if _is_enospc(exc):
                raise TapeFullError(
                    f"磁带 {tape_label} 空间不足，无法写完 {archive_name}。"
                    f"不完整的文件已删除，但磁带为顺序写介质，"
                    f"这部分空间无法回收。"
                ) from exc
            raise
        finally:
            if not fh.closed:
                fh.close()

    def open_archive(self, archive_name: str, *, expected_bytes: int | None = None) -> IO[bytes]:
        """Open ``<mount>/<archive_name>`` for streaming reads.

        不再把整个 archive 拷到本地磁盘（旧 :func:`read_archive` 的做法--
        单包可达 ~1TB，本地盘往往装不下）。``expected_bytes`` 给出时先做
        一次廉价的 stat 比对，大小不符立刻失败，避免白读几小时磁带。
        """
        src = self._mount_path() / archive_name
        if not src.exists():
            raise FileNotFoundError(f"LTFS: 磁带上没有 {archive_name}（{src}）")
        if expected_bytes is not None:
            actual = src.stat().st_size
            if actual != expected_bytes:
                raise RuntimeError(
                    f"LTFS 文件大小不符：{archive_name} 实际 {actual}，"
                    f"目录库记录 {expected_bytes}"
                )
        _LOGGER.info("LTFS: streaming %s", src)
        return open(src, "rb")

    def hash_archive(self, archive_name: str) -> str:
        """SHA256 of an archive on tape, streamed (no local copy)."""
        import hashlib

        h = hashlib.sha256()
        with self.open_archive(archive_name) as fh:
            while True:
                block = fh.read(8 * 1024 * 1024)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()

    def read_archive(
        self, dest: Path, expected_bytes: int, *, archive_name: str
    ) -> None:
        """Copy ``<mount>/<archive_name>`` to ``dest``, checking its size."""
        src = self._mount_path() / archive_name
        if not src.exists():
            raise FileNotFoundError(f"LTFS: 磁带上没有 {archive_name}（{src}）")
        dest.parent.mkdir(parents=True, exist_ok=True)
        _LOGGER.info("LTFS: reading %s -> %s", src, dest)
        shutil.copy2(src, dest)
        actual = dest.stat().st_size
        if actual != expected_bytes:
            raise RuntimeError(
                f"LTFS read size mismatch: got {actual}, expected {expected_bytes}"
            )

    def remove_archive(self, archive_name: str) -> None:
        """Drop an archive's index entry. Best-effort; never raises.

        Used to clean up after a write that succeeded but could not be
        cataloged, so the next run doesn't see a phantom archive. The tape
        space stays consumed either way.
        """
        try:
            (Path(self.mount) / archive_name).unlink()
            _LOGGER.info("removed orphan archive %s", archive_name)
        except FileNotFoundError:
            pass  # already gone — nothing to clean up
        except OSError as exc:
            _LOGGER.warning("could not remove orphan %s: %s", archive_name, exc)


# --- operator-facing prompts --------------------------------------------------


def prompt_for_tape_insertion(expected_label: str) -> str:
    """Ask the operator to insert and mount the right tape."""
    while True:
        print()
        print("=" * 60)
        print(f"请插入并挂载磁带：{expected_label}")
        print("=" * 60)
        answer = prompt("磁带标签")
        if answer == expected_label:
            return answer
        print(f"  错误：需要 {expected_label}，实际输入 {answer!r}")


def prompt_for_full_tape_swap(reason: str = "当前磁带已写满。") -> str:
    """Prompt when the current tape can't take the next archive."""
    print()
    print("=" * 60)
    print(reason)
    print("请卸载当前磁带，插入并挂载下一盘磁带。")
    print("=" * 60)
    while True:
        answer = prompt("下一盘磁带标签")
        if answer:
            return answer
        print("  标签不能为空")
