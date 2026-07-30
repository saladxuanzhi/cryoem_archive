"""Archive operations: dynamic packing, build, write, restore, search, list.

Design:

* An **Archive** is one ``.tar.zst`` written to tape in a single pass.
  Its size is decided **dynamically** by :func:`calculate_chunk_size` from
  the tape's remaining free space (50 GB 向下取整 / 20 GB 安全区间) —
  no fixed 100 GB chunks anymore.
* A **Dataset** is a user-facing group of files. A dataset may span
  several archives (if it is large), and an archive may contain files
  from several datasets (if they are small).
* The packing algorithm is sequential: process datasets in user order,
  fill each archive to its dynamic target, write it, re-check free
  space, repeat. TargetSize <= 0 → swap tapes.

The build pipeline is:

    tarfile (feeder thread) -> zstd -T0 -c -> LTFS mount

Members are named ``<dataset_name>/<rel_path>`` so the archive is
self-describing on restore. Nothing is written to local disk along the
way — the tar stream is generated in memory and the compressed bytes are
hashed and written to tape in a single pass.

Each archive is paired with a sidecar ``<name>.manifest.json`` next to
the tar file, holding metadata: project_name, datasets, file_list,
timestamp, archive_size, tape_label.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import catalog
import tape
from utils import (
    ByteProgress,
    confirm,
    ensure_tool,
    format_bytes,
    now_iso,
    progress,
    progress_done,
    sha256_file,
)

_LOGGER = logging.getLogger("cryoem_archive")


# --- constants ---------------------------------------------------------------


TARGET_ARCHIVE_BYTES: int = 100_000_000_000  # 100 GB
"""回退目标值。仅在 :func:`ByteProgress` 不知道预估体积时作为分母，
以及 :func:`pack_datasets` 的默认参数；正常写入流程的目标大小由
:func:`calculate_chunk_size` 根据磁带实际剩余空间动态计算。"""

LTO6_RAW_BYTES: int = 2_500_000_000_000        # 2.5 TB

STREAM_CHUNK_BYTES: int = 8 * 1024 * 1024
"""Pump size for the compress→tape stream, and the copy buffer used when
feeding file contents into the tar stream. Large enough that the per-chunk
Python overhead is negligible next to the drive's ~160 MB/s."""

TAR_BLOCK_BYTES: int = 1024 * 1024
"""Write size for the tar stream itself (must be a multiple of 512). The
``tarfile`` default is 10 KiB, which would mean millions of extra pipe
writes over a 2 TB job."""

SAFE_MARGIN_BYTES: int = 40_000_000_000  # 40 GB
"""动态切分时，写完当前包后磁带应有的最小剩余空间（GB 单位的安全区间）。
当公式算出 [剩余空间 - 向下取整后的整数倍空间] < 该值时，再退一档（再减
一个 :data:`CHUNK_MULTIPLE_BYTES`），以确保下一次复测时仍有 40GB 余量。"""

CHUNK_MULTIPLE_BYTES: int = 50_000_000_000  # 50 GB
"""动态切分基数。所有切分包大小向下取整到此值的整数倍；同时这也是最小可
切分包尺寸——剩余空间不足切出一个 50GB 包时返回 0，调用方应触发换磁带。"""


# --- dynamic chunking -------------------------------------------------------


def calculate_chunk_size(
    remaining_space: int,
    safe_margin: int = SAFE_MARGIN_BYTES,
    multiple: int = CHUNK_MULTIPLE_BYTES,
) -> int:
    """根据磁带剩余空间，动态计算下一个 archive 的切分目标大小（字节）。

    算法（沿用需求 1 第 2 点的规则，单位使用十进制 GB 与项目中其它常量保持一致）：

    1. 将 ``remaining_space`` 向下取整到 :data:`CHUNK_MULTIPLE_BYTES`（默认 50GB）
       的整数倍，记作 ``rounded``。
    2. 计算 ``leftover = remaining_space - rounded``。
       如果 ``leftover`` < :data:`SAFE_MARGIN_BYTES`（默认 20GB），则说明留出的空隙
       不够安全，再减去一个 ``multiple``（退一档）。
    3. 如果最终 ``rounded`` < ``multiple``（即剩余空间不足 50GB），返回 0；
       调用方应触发换磁带，而不是带 TargetSize=0 进入循环造成死循环。

    Args:
        remaining_space: 磁带剩余空间（bytes，函数内部换算为 GB 以避免大整数精度问题）。
        safe_margin: 切分包写完后磁带应保留的最小安全区间（bytes，默认 20GB）。
        multiple: 切分基数（bytes，默认 50GB），同时也是最小可切分包尺寸。

    Returns:
        下一个 archive 的目标大小（bytes）。返回 0 表示剩余空间不足触发换磁带。

    Examples:
        >>> # 973 GB -> 950 GB  (leftover 23 >= 20，安全)
        >>> calculate_chunk_size(973 * 1_000_000_000) == 950 * 1_000_000_000
        True
        >>> # 960 GB -> 900 GB  (leftover 10 < 20，再退一档)
        >>> calculate_chunk_size(960 * 1_000_000_000) == 900 * 1_000_000_000
        True
        >>> # 125 GB -> 100 GB
        >>> calculate_chunk_size(125 * 1_000_000_000) == 100 * 1_000_000_000
        True
        >>> # 115 GB ->  50 GB  (leftover 15 < 20，退一档)
        >>> calculate_chunk_size(115 * 1_000_000_000) ==  50 * 1_000_000_000
        True
        >>> # 60 GB -> 0  (不足以切出 50GB 安全包)
        >>> calculate_chunk_size(60 * 1_000_000_000) == 0
        True
    """
    if remaining_space <= 0:
        return 0
    # 单位换算至十进制 GB，避免百亿级 byte 出现计算误差
    gb = 1_000_000_000
    remaining_gb = remaining_space // gb
    margin_gb = safe_margin // gb
    multiple_gb = multiple // gb
    if multiple_gb <= 0:
        raise ValueError("multiple must be positive")
    if margin_gb < 0:
        raise ValueError("safe_margin must be non-negative")

    # Step 1: 向下取整到 multiple 的整数倍
    rounded_gb = (remaining_gb // multiple_gb) * multiple_gb

    # Step 2: 留出的空隙 < 安全区间 时再退一档
    leftover_gb = remaining_gb - rounded_gb
    if leftover_gb < margin_gb:
        rounded_gb -= multiple_gb

    # Step 3: 切不出至少一个 multiple → 返回 0，由调用方换磁带
    if rounded_gb < multiple_gb:
        return 0
    return rounded_gb * gb


# --- data shapes -------------------------------------------------------------


@dataclass
class ArchiveManifest:
    """Manifest 元数据，写入 sidecar 文件 ``<archive>.tar.zst.manifest.json``。

    字段含义：

    * ``project_name``：备份时由操作员指定的 Project 名称（可能为空字符串）。
    * ``datasets``：该 archive 实际包含的所有 dataset 列表（含 operator/comment）。
    * ``file_list``：文件清单，每项是 ``(dataset, path, size, mtime)`` 四元组；
      ``path`` 是 tar 内的 ``<dataset>/<rel_path>`` 形式。
    * ``timestamp``：打包时间（ISO 8601，UTC）。
    * ``archive_size``：archive 在磁带上的实际体积（写入后回填，bytes）。
    * ``tape_label``：磁带卷名，如 ``EM_data_1``。在向 LTFS 写盘时确定。
    """

    project_name: str = ""
    datasets: list[dict] = field(default_factory=list)
    file_list: list[dict] = field(default_factory=list)
    timestamp: str = ""
    archive_size: int = 0
    tape_label: str = ""

    def to_json_bytes(self) -> bytes:
        """序列化为格式化 JSON 字节串（ensure_ascii=False 以保留中文）。"""
        payload = {
            "manifest_version": "1.0",
            "project_name": self.project_name,
            "tape_label": self.tape_label,
            "dataset": self.datasets[0]["name"] if self.datasets else "",
            "datasets": list(self.datasets),
            "file_list": list(self.file_list),
            "timestamp": self.timestamp,
            "archive_size": self.archive_size,
        }
        return json.dumps(
            payload, indent=2, ensure_ascii=False, sort_keys=False
        ).encode("utf-8")


def build_archive_manifest(
    spec: "ArchiveSpec",
    dataset_lookup: dict[int, "DatasetSpec"],
    *,
    tape_label: str,
    archive_size: int = 0,
) -> ArchiveManifest:
    """根据 :class:`ArchiveSpec` 构造 manifest 字典。

    Args:
        spec: 即将写入的 archive 计划（包含所有文件）。
        dataset_lookup: ``dataset_id -> DatasetSpec`` 映射，用于回查
            project / operator / comment 等元数据。
        tape_label: 目标磁带卷名（写入时确定，所以早于体积回填也是合法的）。
        archive_size: 写入后的实际体积（bytes），写入前可传 0。
    """
    seen: set[int] = set()
    datasets: list[dict] = []
    for entry in spec.files:
        ds_id = entry[0]
        if ds_id in seen:
            continue
        seen.add(ds_id)
        meta = dataset_lookup.get(ds_id)
        if meta is None:
            continue
        datasets.append(
            {
                "id": int(ds_id),
                "name": meta.name,
                "project": meta.project or "",
                "operator": meta.operator or "",
                "comment": meta.comment or "",
            }
        )

    file_list: list[dict] = []
    for ds_id, ds_name, _abs, rel, size, mtime in spec.files:
        file_list.append(
            {
                "dataset": ds_name,
                "path": f"{ds_name}/{rel}",
                "size": int(size),
                "mtime": mtime,
            }
        )

    primary = datasets[0] if datasets else {}
    return ArchiveManifest(
        project_name=str(primary.get("project", "") or ""),
        datasets=datasets,
        file_list=file_list,
        timestamp=now_iso(),
        archive_size=int(archive_size),
        tape_label=tape_label,
    )


@dataclass
class DatasetSpec:
    """A single dataset to be archived, plus its discovered files."""

    source: Path
    name: str
    project: str | None = None
    operator: str | None = None
    comment: str | None = None
    # (abs_path, size, mtime_iso)  — populated by :func:`walk_dataset`
    files: list[tuple[Path, int, str]] = field(default_factory=list)


@dataclass
class ArchiveSpec:
    """A planned archive: its target name and the files (with dataset info)
    that will be packed into it."""

    name: str
    # (dataset_id, dataset_name, abs_path, rel_path_within_dataset, size, mtime)
    files: list[tuple[int, str, Path, str, int, str]] = field(default_factory=list)
    estimated_size: int = 0  # uncompressed bytes


# --- discovery ---------------------------------------------------------------


def walk_dataset(source: Path) -> list[tuple[Path, int, str]]:
    """Walk ``source`` and return ``[(abs_path, size, mtime_iso), ...]``.

    Sorted, hidden files skipped. Used by the packing algorithm to know
    what goes into the archive.
    """
    out: list[tuple[Path, int, str]] = []
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            p = Path(dirpath) / fname
            try:
                st = p.stat()
            except OSError as exc:
                raise RuntimeError(f"stat failed: {p}: {exc}") from exc
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(
                timespec="seconds"
            )
            out.append((p, st.st_size, mtime))
    return out


# --- packing algorithm -------------------------------------------------------


def pack_datasets(
    datasets: list[tuple[int, DatasetSpec]],
    archive_names: list[str],
    *,
    target_bytes: int = TARGET_ARCHIVE_BYTES,
) -> list[ArchiveSpec]:
    """Pack files from multiple datasets into chunks of up to ``target_bytes``.

    这是一个测试夹具（被 :mod:`tests_smoke` 复用）：贪心填满直至超阈值。
    生产环境（:func:`create_archives`）走的是「_take_one_archive + 动态
    TargetSize」实时循环，并不再使用本函数。

    Args:
        datasets: ``[(dataset_id, spec), ...]`` in user order.
        archive_names: Pre-allocated YYYYMMDD_NNNN names (one per archive).
        target_bytes: Per-archive size target. The packing closes an
            archive when the next file would push it over this size.

    Returns:
        A list of :class:`ArchiveSpec` in write order, each containing
        the files (with their dataset info) that belong to it.

    Raises:
        RuntimeError: If a single file is larger than ``target_bytes``.
    """
    archives: list[ArchiveSpec] = []
    current_files: list[tuple[int, str, Path, str, int, str]] = []
    current_size = 0
    name_idx = 0

    def flush() -> None:
        nonlocal current_files, current_size, name_idx
        if not current_files:
            return
        if name_idx >= len(archive_names):
            raise RuntimeError("ran out of pre-allocated archive names")
        archives.append(
            ArchiveSpec(
                name=archive_names[name_idx],
                files=list(current_files),
                estimated_size=current_size,
            )
        )
        name_idx += 1
        current_files = []
        current_size = 0

    for dataset_id, spec in datasets:
        for abs_path, size, mtime in spec.files:
            if size > target_bytes:
                raise RuntimeError(
                    f"文件过大：{abs_path} ({format_bytes(size)}) 超过单 Archive 限制 "
                    f"({format_bytes(target_bytes)})"
                )
            if current_size + size > target_bytes and current_files:
                flush()
            rel = abs_path.relative_to(spec.source).as_posix()
            current_files.append((dataset_id, spec.name, abs_path, rel, size, mtime))
            current_size += size

    flush()
    return archives


# --- build a single archive --------------------------------------------------


class _TarFeeder:
    """Writes ``spec``'s files as a tar stream into ``sink``, on its own thread.

    Runs in a thread because the compressor sits between two pipes we both
    feed and drain: writing tar into zstd's stdin while reading its stdout
    from the same thread would deadlock as soon as either pipe buffer fills.

    Members are named ``<dataset_name>/<rel_path>`` so the archive is
    self-describing on restore. Nothing is staged on disk — the previous
    design built a symlink tree under ``data/staging`` and pointed ``tar``
    at it, which meant a stray ``rm -rf`` of that directory (or anything
    else touching it) broke a running job hours in.
    """

    def __init__(self, spec: ArchiveSpec, sink) -> None:
        self.spec = spec
        self.sink = sink
        self.error: BaseException | None = None
        # (dataset_id, rel_path, size, mtime) for files actually archived.
        self.file_rows: list[tuple[int, str, int, str]] = []
        self.uncompressed_size = 0

    def run(self) -> None:
        try:
            # dereference=True matches the old `tar -h`: a symlink in the
            # source is archived as the file it points at, which is also
            # what walk_dataset measured with stat().
            with tarfile.open(
                fileobj=self.sink,
                mode="w|",
                dereference=True,
                bufsize=TAR_BLOCK_BYTES,
                copybufsize=STREAM_CHUNK_BYTES,
            ) as tf:
                for ds_id, ds_name, abs_path, rel_path, size, mtime in self.spec.files:
                    tf.add(
                        str(abs_path),
                        arcname=f"{ds_name}/{rel_path}",
                        recursive=False,
                    )
                    self.file_rows.append((ds_id, rel_path, size, mtime))
                    self.uncompressed_size += size
        except BaseException as exc:  # noqa: BLE001 — re-raised on the main thread
            self.error = exc
        finally:
            try:
                self.sink.close()
            except OSError:
                pass


def build_and_write_archive(
    spec: ArchiveSpec,
    device: tape.TapeDevice,
    tape_label: str,
    *,
    dataset_lookup: dict[int, "DatasetSpec"] | None = None,
) -> tuple[int, int, str, list[tuple[int, str, int, str]], "Path | None"]:
    """Stream ``spec`` onto the tape as ``<spec.name>.tar.zst``，并写出 sidecar manifest。

    Returns ``(compressed_size, uncompressed_size, sha256, file_rows, manifest_path)``
    其中 ``file_rows`` 是 ``(dataset_id, rel_path, size, mtime)`` 待入库的列表；
    ``manifest_path`` 是 sidecar manifest 文件的路径，若 ``dataset_lookup``
    未提供则为 ``None``。

    The pipeline is::

        tarfile (feeder thread)  ->  zstd -T0 -c  ->  LTFS mount

    Nothing touches local disk at any point: the tar stream is generated in
    memory, compressed by zstd, and the resulting bytes are hashed and
    written to the tape in one pass.
    """
    zstd = ensure_tool("zstd")
    zstd_argv = [zstd, "-T0", "-q", "-c"]
    _LOGGER.info(
        "running: tarfile(%d files) | %s > tape:%s",
        len(spec.files), " ".join(zstd_argv), f"{spec.name}.tar.zst",
    )

    start = time.time()
    # zstd's stderr goes to a temp file, not a pipe: a pipe nobody drains
    # until the end would deadlock the pipeline if zstd gets chatty.
    with tempfile.TemporaryFile() as zstd_err:
        zstd_proc = subprocess.Popen(
            zstd_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=zstd_err,
        )
        assert zstd_proc.stdin is not None and zstd_proc.stdout is not None

        feeder = _TarFeeder(spec, zstd_proc.stdin)
        thread = threading.Thread(
            target=feeder.run, name=f"tar-{spec.name}", daemon=True
        )
        thread.start()

        digest = hashlib.sha256()
        compressed_size = 0
        # The bar tracks compressed bytes landing on tape. For cryo-EM data
        # the ratio is close to 1, so the uncompressed estimate is a usable
        # denominator.
        bar = ByteProgress(
            f"写入 {spec.name}", spec.estimated_size or TARGET_ARCHIVE_BYTES
        )
        try:
            with device.archive_writer(
                f"{spec.name}.tar.zst", tape_label=tape_label
            ) as sink:
                while True:
                    chunk = zstd_proc.stdout.read(STREAM_CHUNK_BYTES)
                    if not chunk:
                        break
                    sink.write(chunk)
                    digest.update(chunk)
                    compressed_size += len(chunk)
                    bar.update(compressed_size)

                # Validate while still inside the writer context: anything
                # raised here makes archive_writer drop what it just wrote,
                # so a bad archive never survives on the medium. The feeder
                # is checked first — when it dies, zstd sees a clean EOF and
                # happily reports success on the truncated input.
                thread.join()
                if feeder.error is not None:
                    raise RuntimeError(
                        f"读取源文件失败：{feeder.error}"
                    ) from feeder.error
                zstd_proc.wait()
                if zstd_proc.returncode != 0:
                    raise RuntimeError(
                        f"zstd failed (rc={zstd_proc.returncode}): "
                        f"{_read_temp(zstd_err)}"
                    )
                # Guard against silently archiving nothing. A >1000x ratio
                # on a multi-GB dataset means the contents never made it in.
                if (
                    feeder.uncompressed_size > 1_000_000_000
                    and compressed_size * 1000 < feeder.uncompressed_size
                ):
                    raise RuntimeError(
                        f"归档结果异常：源数据 "
                        f"{format_bytes(feeder.uncompressed_size)}，"
                        f"但压缩后只有 {format_bytes(compressed_size)}。"
                        f"归档内很可能不含真实数据，已中止。"
                    )
        finally:
            # Tear the pipeline down even if the pump aborted early: closing
            # our read end makes zstd exit, which in turn unblocks a feeder
            # thread parked on a full stdin pipe.
            zstd_proc.stdout.close()
            zstd_proc.wait()
            thread.join(timeout=30)

    sha = digest.hexdigest()
    bar.done(
        f"写入 {spec.name}  {format_bytes(compressed_size)} "
        f"({time.time() - start:.1f}s)",
        final=compressed_size,
    )

    # 写入 sidecar manifest：与 archive 同一目录，不进入 tar 流
    # （写完后 archive_size 已是准确值，直接回填给 manifest）。
    manifest_path: Path | None = None
    if dataset_lookup:
        try:
            manifest = build_archive_manifest(
                spec,
                dataset_lookup,
                tape_label=tape_label,
                archive_size=compressed_size,
            )
            manifest_path = _write_manifest_sidecar(device, spec.name, manifest)
            _LOGGER.info("manifest -> %s", manifest_path)
        except OSError as exc:
            # Manifest 写失败不应影响主归档：catalog 仍有完整记录。
            _LOGGER.warning(
                "manifest sidecar failed for %s: %s", spec.name, exc,
            )

    return (
        compressed_size,
        feeder.uncompressed_size,
        sha,
        feeder.file_rows,
        manifest_path,
    )


def _write_manifest_sidecar(
    device: tape.TapeDevice,
    archive_name: str,
    manifest: "ArchiveManifest",
) -> "Path":
    """把 manifest 写到 ``<mount>/<archive_name>.manifest.json``。

    sidecar 与 ``.tar.zst`` 在同一目录，恢复时只需列出归档所在目录即可看到
    对应的清单（JSON 中文用 ``ensure_ascii=False`` 保留原字符）。
    """
    p = Path(device.mount) / f"{archive_name}.manifest.json"
    p.write_bytes(manifest.to_json_bytes())
    return p


def _read_temp(fh) -> str:
    """Read back a subprocess stderr temp file as text."""
    try:
        fh.seek(0)
        return fh.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


# --- end-to-end: create archives --------------------------------------------


def create_archives(
    conn,
    device: tape.TapeDevice,
    datasets: list[DatasetSpec],
    tape_label: str,
    capacity_bytes: int,
) -> list[dict]:
    """动态切分打包 datasets 并写入磁带（需求 1 的重构主入口）。

    主要流程（替代旧的「先 pack 再写」固定 100GB 切分）：

    1. **首次测量** 读取磁带剩余空间；
    2. **整包优先**：如果剩余空间能放下所有剩余文件（-安全区间），则将整个
       项目作为单一 archive 写入，不切分；
    3. 否则调用 :func:`calculate_chunk_size` 计算当前的 TargetSize；
    4. 从剩余文件里贪心切出 TargetSize 大小的一个 ArchiveSpec 写入磁带；
    5. 写入后再次读取剩余空间，复测 → 再算 → 再切，直到写完或 TargetSize<=0；
    6. TargetSize<=0 时自动换磁带后继续；不会因换磁带丢失任何文件。

    沿用职责：

    * dataset 行入库与续传（续传以 catalog 为准）；
    * 单个 archive 失败或 catalog 失败时回收 orphan 文件；
    * 任何 archive 都不能跨磁带。
    """
    if not datasets:
        raise RuntimeError("至少需要一个 Dataset")

    # 1) Walk each dataset to enumerate its files (with progress).
    print("扫描数据 ...")
    for i, spec in enumerate(datasets, 1):
        print(f"  [{i}/{len(datasets)}] {spec.name} ({spec.source})")
        spec.files = walk_dataset(spec.source)
        total = sum(f[1] for f in spec.files)
        print(f"        {len(spec.files)} 个文件，{format_bytes(total)}")

    # 2) Insert dataset rows + keep id->spec 映射（manifest 需要回查元数据）。
    dataset_ids: list[tuple[int, DatasetSpec]] = []
    dataset_lookup: dict[int, DatasetSpec] = {}
    for spec in datasets:
        ds_id = catalog.ensure_dataset(
            conn,
            spec.name,
            project=spec.project,
            operator=spec.operator,
            comment=spec.comment,
        )
        dataset_ids.append((ds_id, spec))
        dataset_lookup[ds_id] = spec

    # 2b) Resume: drop files an earlier run already committed to tape.
    resumed = 0
    for ds_id, spec in dataset_ids:
        done = catalog.get_archived_rel_paths(conn, ds_id)
        if not done:
            continue
        kept = [
            f for f in spec.files
            if f[0].relative_to(spec.source).as_posix() not in done
        ]
        skipped = len(spec.files) - len(kept)
        if skipped:
            resumed += skipped
            print(
                f"  续传 {spec.name}：已归档 {skipped} 个文件，"
                f"本次继续 {len(kept)} 个"
            )
        spec.files = kept

    if all(not spec.files for _, spec in dataset_ids):
        if resumed:
            print()
            print("  ✓ 所有文件均已归档完成，无需继续。")
            return []
        raise RuntimeError("所有 Dataset 均为空，没有可归档内容。")

    # 3) 把所有待写入的文件摊平成一个 list（保持 dataset 内部顺序）。
    #    条目 = (ds_id, ds_name, abs_path, rel_path, size, mtime)
    remaining: list[tuple[int, str, Path, str, int, str]] = []
    for ds_id, spec in dataset_ids:
        for abs_path, size, mtime in spec.files:
            rel = abs_path.relative_to(spec.source).as_posix()
            remaining.append((ds_id, spec.name, abs_path, rel, size, mtime))

    total_bytes = sum(f[4] for f in remaining)
    print()
    print(f"  源数据总量 {format_bytes(total_bytes)}（{len(remaining)} 个文件）。")
    free_now = device.free_bytes()
    if free_now is not None:
        print(f"  当前磁带 {tape_label} 剩余 {format_bytes(free_now)}。")

    if not confirm("确认开始写入？（动态切分，首包前会再次确认）", default=False):
        raise RuntimeError("已取消。")

    # 4) Make sure the destination tape exists.
    catalog.ensure_tape(conn, tape_label, capacity_bytes, status="in_use")
    current_tape = tape_label
    written: list[dict] = []
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    archive_index = 0  # 第 i+1 个 archive，仅用于提示

    # 5) 主循环：动态切分 + 动态换磁带，直到 remaining 为空。
    #    安全保护：循环步数有上限（远大于任何真实场景），避免潜在 bug 造成死循环。
    max_iterations = 1000
    while remaining:
        if archive_index > max_iterations:
            raise RuntimeError(
                f"异常：动态切分循环超过 {max_iterations} 次未写完所有文件；"
                "请检查磁带剩余空间或算法。"
            )

        # 5a) 取当前磁带剩余空间
        free = device.free_bytes()
        total_remaining_bytes = sum(f[4] for f in remaining)

        # 5b) 计算本轮 TargetSize。
        # 唯一的换磁带触发条件是剩余空间不够切出下一个 50GB 安全包；
        # 不再有「单盘 archive 数」的定额上限（动态切分自然按物理空间切）。
        if free is not None:
            # 优先整包：如果剩余空间能放下所有剩余文件，整体作为一个 archive。
            if total_remaining_bytes + SAFE_MARGIN_BYTES <= free:
                target_bytes = total_remaining_bytes
            else:
                target_bytes = calculate_chunk_size(free)
        else:
            # 设备无法读取剩余空间 → 引导操作员换盘
            target_bytes = 0

        # 5c) TargetSize<=0 → 换磁带。
        #    注意：仅换磁带而不写盘的迭代不该占 archive_index；只有真正要
        #    写入下一包时才递增（见 5f 处），否则「第 3 包」会把第 1 个 swap
        #    的空迭代也算进去。
        if target_bytes <= 0:
            reason = "剩余空间不足触发动态换磁带。"
            print()
            print(f"  ↻ {reason}")
            catalog.set_tape_status(conn, current_tape, "full")
            current_tape = tape.prompt_for_full_tape_swap(
                f"磁带 {current_tape} {reason}",
            )
            catalog.ensure_tape(conn, current_tape, capacity_bytes, status="in_use")
            continue  # 新磁带，继续外层循环

        # 5e) 贪心切出 1 个 ArchiveSpec（不超过 target_bytes）。
        chunk_files, chunk_size = _take_one_archive(remaining, target_bytes)
        if not chunk_files:
            # 防御性兜底：remaining 非空但切不动（全部文件都 > target）。
            # 给用户一个明确报错，包含那个超大文件的路径。
            big = max(remaining, key=lambda f: f[4])
            raise RuntimeError(
                f"无法切分：仍有 {len(remaining)} 个文件，但最大的 "
                f"{big[2]} ({format_bytes(big[4])}) 大于当前 TargetSize "
                f"{format_bytes(target_bytes)}。"
                f"请检查数据集或磁带状态。"
            )

        # 5f) 分配真实 archive 名 + 递增 archive_index（仅真正写盘时才递增，
        #    5c 处纯换磁带的迭代不占号）。
        archive_index += 1
        archive_name = catalog.next_archive_name(conn, today)
        spec = ArchiveSpec(
            name=archive_name,
            files=chunk_files,
            estimated_size=chunk_size,
        )

        print()
        print(
            f"[第 {archive_index} 包] 归档 {spec.name}  → 磁带 {current_tape}  "
            f"目标 {format_bytes(target_bytes)} / 本包 {format_bytes(chunk_size)}"
        )

        # 5g) 写入磁带（含 sidecar manifest）
        file_number = catalog.next_file_number(conn, current_tape)
        print(
            f"  目标：磁带 {current_tape} "
            f"(file #{file_number})"
        )
        try:
            (
                compressed_size,
                uncompressed_size,
                sha,
                file_rows,
                manifest_path,
            ) = build_and_write_archive(
                spec,
                device,
                current_tape,
                dataset_lookup=dataset_lookup,
            )
        except tape.TapeFullError as exc:
            # ENOSPC（理论上动态算法已经避开；如果发生说明预估偏差很大）
            print()
            print(f"  ✗ {exc}")
            print(f"     不完整的 {spec.name}.tar.zst 已删除，目录库未记录。")
            catalog.set_tape_status(conn, current_tape, "full")
            if not confirm("是否更换磁带并继续？", default=True):
                raise RuntimeError(
                    f"已中止。{spec.name} 及其后续内容未归档；"
                    f"重新运行同一条命令即可从此处续传。"
                ) from exc
            current_tape = tape.prompt_for_full_tape_swap(
                f"磁带 {current_tape} 空间不足。"
            )
            catalog.ensure_tape(conn, current_tape, capacity_bytes, status="in_use")
            continue  # 重新读取剩余空间、写同一逻辑

        print(f"  ✓ SHA256 {spec.name}  {sha[:12]}...")
        if manifest_path is not None:
            print(f"  ✓ manifest  → {manifest_path.name}")

        # 5h) 在一个事务里写 catalog + 增计数器；失败时移除 orphan 文件。
        try:
            with catalog.transaction(conn):
                catalog.insert_archive(
                    conn,
                    name=spec.name,
                    archive_size=compressed_size,
                    uncompressed_size=uncompressed_size,
                    sha256=sha,
                    tape_label=current_tape,
                    file_number=file_number,
                )
                for ds_id in sorted({ds_id for ds_id, *_ in file_rows}):
                    catalog.add_dataset_archive(
                        conn,
                        ds_id,
                        spec.name,
                        catalog.next_dataset_archive_sequence(conn, ds_id),
                    )
                catalog.insert_files_batch(
                    conn,
                    [
                        (spec.name, ds_id, rel_path, size, mtime)
                        for ds_id, rel_path, size, mtime in file_rows
                    ],
                )
                catalog.increment_tape_archive_count(conn, current_tape)
        except Exception:
            device.remove_archive(f"{spec.name}.tar.zst")
            # 也清掉对应的 sidecar manifest
            try:
                (Path(device.mount) / f"{spec.name}.manifest.json").unlink()
            except OSError:
                pass
            raise

        written.append({
            "name": spec.name,
            "tape_label": current_tape,
            "file_number": file_number,
            "compressed_size": compressed_size,
            "uncompressed_size": uncompressed_size,
            "sha256": sha,
            "file_count": len(file_rows),
            "manifest_path": str(manifest_path) if manifest_path else None,
        })
        print(
            f"  ✓ {spec.name}  {format_bytes(compressed_size)}  "
            f"磁带 {current_tape} file#{file_number}"
        )
        _report_free_space(device, current_tape)

    return written


def _take_one_archive(
    remaining: list[tuple[int, str, Path, str, int, str]],
    target_bytes: int,
) -> tuple[list[tuple[int, str, Path, str, int, str]], int]:
    """从 ``remaining`` 头部贪心切出最多 ``target_bytes`` 的一个 archive。

    修改入参 ``remaining``（pop 出已写入的文件）。返回 ``(chunk_files, chunk_size)``。
    当 ``remaining`` 为空或所有剩余文件都装不下时，返回 ``([], 0)``。

    关键规则：
    * 加入新文件后会超过 target 时立即关闭当前 chunk。
    * 单文件 > target 时允许「仅这一个文件」单独作为一个 chunk（前提条件是
      它已经能够放进当前剩余空间，否则由外层动态循环触发换磁带）。
    """
    chunk: list[tuple[int, str, Path, str, int, str]] = []
    chunk_size = 0
    while remaining:
        head = remaining[0]
        size = head[4]
        # 第一个文件就可以是「比 target 还大的单文件」：允许放行（外层会
        # 凭借总剩余空间决定是否换磁带）。
        if chunk and chunk_size + size > target_bytes:
            break
        chunk.append(remaining.pop(0))
        chunk_size += size
    return chunk, chunk_size


def _report_free_space(device: tape.TapeDevice, label: str) -> None:
    """Print remaining capacity after a successful write, when knowable."""
    free = device.free_bytes()
    if free is None:
        return
    print(f"     磁带 {label} 剩余 {format_bytes(free)}")


# --- restore -----------------------------------------------------------------


def restore_dataset(
    conn,
    device: tape.TapeDevice,
    dataset_name: str,
    dest_dir: Path,
) -> Path:
    """Restore a dataset by reading all its archives in sequence."""
    dataset = catalog.get_dataset(conn, dataset_name)
    if dataset is None:
        raise KeyError(f"目录库中没有 Dataset {dataset_name!r}")
    archives = catalog.get_archives_for_dataset(conn, int(dataset["id"]))
    if not archives:
        raise RuntimeError(f"Dataset {dataset_name!r} 在目录库中没有关联任何 archive。")

    print(f"  Dataset {dataset_name!r} 涉及 {len(archives)} 个 archive：")
    for i, a in enumerate(archives, 1):
        print(
            f"    {i:>3}. {a['name']}  磁带 {a['tape_label']}  "
            f"#{a['file_number']}  {format_bytes(a['archive_size'])}"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not confirm(f"恢复到 {dest_dir}？", default=True):
        raise RuntimeError("已取消。")

    zstd = ensure_tool("zstd")
    tar = ensure_tool("tar")

    current_tape: str | None = None
    for i, a in enumerate(archives, 1):
        if a["tape_label"] != current_tape:
            tape.prompt_for_tape_insertion(a["tape_label"])
            current_tape = a["tape_label"]

        print(
            f"  [{i}/{len(archives)}] 读取 {a['name']} 磁带 {a['tape_label']} ..."
        )
        with tempfile.TemporaryDirectory(prefix="cryoem_restore_") as tmp:
            staging = Path(tmp) / f"{a['name']}.tar.zst"
            device.read_archive(
                staging,
                expected_bytes=int(a["archive_size"]),
                archive_name=f"{a['name']}.tar.zst",
            )
            # Verify SHA256
            progress(f"SHA256 {a['name']}", 0, 1)
            actual_sha = sha256_file(staging)
            if actual_sha.lower() != a["sha256"].lower():
                progress_done()
                raise RuntimeError(
                    f"SHA256 校验失败：{a['name']} 磁带读到 {actual_sha}，"
                    f"目录库记录 {a['sha256']}"
                )
            progress_done(f"SHA256 {a['name']} OK")

            # Extract
            print(f"        解压到 {dest_dir} ...")
            try:
                with open(staging, "rb") as src_fh:
                    zstd_proc = subprocess.Popen(
                        [zstd, "-T0", "-q", "-d", "-c"],
                        stdin=src_fh, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
            except FileNotFoundError as exc:
                raise RuntimeError(f"zstd not found: {exc}") from exc
            if zstd_proc.stdout is None:
                raise RuntimeError("zstd produced no stdout")
            try:
                tar_proc = subprocess.Popen(
                    [tar, "-xf", "-", "-C", str(dest_dir)],
                    stdin=zstd_proc.stdout, stderr=subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                zstd_proc.kill()
                raise RuntimeError(f"tar not found: {exc}") from exc
            finally:
                zstd_proc.stdout.close()
            _, tar_err = tar_proc.communicate()
            zstd_proc.wait()
            if tar_proc.returncode != 0:
                raise RuntimeError(
                    f"tar extract failed: {tar_err.decode('utf-8', errors='replace')}"
                )

    print(f"  ✓ Dataset {dataset_name} 已恢复到 {dest_dir}")
    return dest_dir


# --- verify ------------------------------------------------------------------


def verify_archive(
    conn,
    device: tape.TapeDevice,
    archive_name: str,
    *,
    assume_mounted: bool = False,
) -> dict:
    """Read one archive back from tape and check its SHA256.

    Args:
        assume_mounted: Skip the "insert tape" prompt because the caller
            already confirmed the right tape is mounted. Set by
            :func:`verify_tape`, which would otherwise re-prompt for the
            same tape once per archive.
    """
    row = catalog.get_archive(conn, archive_name)
    if row is None:
        raise KeyError(f"目录库中没有 archive {archive_name!r}")

    tape_label = row["tape_label"]
    if not assume_mounted:
        tape.prompt_for_tape_insertion(tape_label)
    with tempfile.TemporaryDirectory(prefix="cryoem_verify_") as tmp:
        tmp_path = Path(tmp) / f"{archive_name}.tar.zst"
        device.read_archive(
            tmp_path,
            expected_bytes=int(row["archive_size"]),
            archive_name=f"{archive_name}.tar.zst",
        )
        actual = sha256_file(tmp_path)

    if actual.lower() != row["sha256"].lower():
        raise RuntimeError(
            f"SHA256 校验失败（磁带 {tape_label}）：{actual} != {row['sha256']}"
        )
    print(f"  OK  {archive_name}  SHA256 一致  来源：磁带 {tape_label}")
    return {"name": archive_name, "ok": True, "source": f"磁带 {tape_label}"}


def verify_tape(conn, device: tape.TapeDevice, label: str) -> tuple[int, int]:
    """Verify every archive on a tape. Returns ``(ok_count, fail_count)``."""
    archives = catalog.list_archives(conn, tape_label=label)
    if not archives:
        print(f"  磁带 {label} 上没有 archive。")
        return 0, 0
    tape.prompt_for_tape_insertion(label)
    ok = 0
    fail = 0
    for a in archives:
        try:
            verify_archive(conn, device, a["name"], assume_mounted=True)
            ok += 1
        except Exception as exc:
            print(f"  FAIL  {a['name']}  {exc}")
            fail += 1
    return ok, fail


# --- search ------------------------------------------------------------------


def glob_to_sql_like(pattern: str) -> str:
    """Translate a shell glob to a SQL ``LIKE`` pattern."""
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append("%")
        elif ch == "?":
            out.append("_")
        elif ch == "%":
            out.append(r"\%")
        elif ch == "_":
            out.append(r"\_")
        elif ch == "\\":
            out.append("\\\\")
        else:
            out.append(ch)
    return "".join(out)


def search_files(
    conn,
    pattern: str,
    *,
    dataset_name: str | None = None,
    archive_name: str | None = None,
    tape_label: str | None = None,
) -> list[dict]:
    return [
        {
            "archive": r["archive_name"],
            "dataset": r["dataset_name"],
            "path": r["rel_path"],
            "size": r["size"],
            "tape": r["tape_label"],
        }
        for r in catalog.search_files(
            conn,
            glob_to_sql_like(pattern),
            dataset_name=dataset_name,
            archive_name=archive_name,
            tape_label=tape_label,
        )
    ]
