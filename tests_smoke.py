"""Smoke tests for the refactored 5-file layout.

Run with: ``python tests_smoke.py`` from the project root.
"""

from __future__ import annotations

import errno
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import archive
import catalog
import utils

GB = 1_000_000_000  # 供测试使用的十进制 GB 常量


def test_format_bytes() -> None:
    assert utils.format_bytes(0) == "0 B"
    assert utils.format_bytes(1024) == "1.00 KiB"
    assert utils.format_bytes(1024 * 1024) == "1.00 MiB"
    assert utils.format_bytes(100_000_000_000) == "93.13 GiB"
    assert utils.format_bytes(2_500_000_000_000) == "2.27 TiB"
    print("  OK  format_bytes")


def test_sha256_known_value() -> None:
    assert (
        utils.sha256_bytes(b"")
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert (
        utils.sha256_bytes(b"abc")
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    print("  OK  sha256")


def test_glob_to_like() -> None:
    assert archive.glob_to_sql_like("*.mrc") == "%.mrc"
    assert archive.glob_to_sql_like("a?b") == "a_b"
    assert archive.glob_to_sql_like("a%b") == r"a\%b"
    assert archive.glob_to_sql_like(r"a\b") == r"a\\b"
    print("  OK  glob -> SQL LIKE")


def test_parse_archive_name() -> None:
    d, s = catalog.parse_archive_name("20260728_0007")
    assert d == "20260728" and s == 7
    try:
        catalog.parse_archive_name("invalid")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    print("  OK  parse_archive_name")


def test_packing_split() -> None:
    """Dataset A (6000 B) + Dataset B (500 B), target 3500 B.
    A must split into two archives; B fills the second.
    """
    tmp = Path(tempfile.mkdtemp(prefix="cryoem_pack_"))
    try:
        a = tmp / "A"
        a.mkdir()
        (a / "a1").write_bytes(b"x" * 1000)
        (a / "a2").write_bytes(b"x" * 2000)
        (a / "a3").write_bytes(b"x" * 3000)
        b = tmp / "B"
        b.mkdir()
        (b / "b1").write_bytes(b"x" * 500)

        sa = archive.DatasetSpec(source=a, name="A", files=archive.walk_dataset(a))
        sb = archive.DatasetSpec(source=b, name="B", files=archive.walk_dataset(b))

        names = [f"20260728_{i:04d}" for i in range(1, 10)]
        archives = archive.pack_datasets([(1, sa), (2, sb)], names, target_bytes=3500)
        assert len(archives) == 2, f"expected 2 archives, got {len(archives)}"
        # First archive: A only (3000 B)
        assert archives[0].estimated_size == 3000
        assert all(f[1] == "A" for f in archives[0].files)
        # Second archive: A leftover (0) + B (500) = wait, target 3500
        # After first archive (3000 B), next file is a3 (3000 B) which would
        # push to 6000, so it opens new archive. So second archive is a3 + b1.
        assert archives[1].estimated_size == 3500
        ds_in_second = sorted(set(f[1] for f in archives[1].files))
        assert ds_in_second == ["A", "B"], f"got {ds_in_second}"
        print("  OK  packing split (dataset split + leftover fill)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_packing_all_in_one() -> None:
    """When the target is huge, everything goes into one archive."""
    tmp = Path(tempfile.mkdtemp(prefix="cryoem_pack_"))
    try:
        a = tmp / "A"
        a.mkdir()
        (a / "a1").write_bytes(b"x" * 100)
        b = tmp / "B"
        b.mkdir()
        (b / "b1").write_bytes(b"x" * 200)

        sa = archive.DatasetSpec(source=a, name="A", files=archive.walk_dataset(a))
        sb = archive.DatasetSpec(source=b, name="B", files=archive.walk_dataset(b))
        archives = archive.pack_datasets(
            [(1, sa), (2, sb)], ["20260728_0001"], target_bytes=1_000_000
        )
        assert len(archives) == 1
        assert sorted(set(f[1] for f in archives[0].files)) == ["A", "B"]
        print("  OK  packing all-in-one (multiple small datasets in one archive)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_packing_oversized_file() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cryoem_pack_"))
    try:
        a = tmp / "A"
        a.mkdir()
        (a / "big").write_bytes(b"X" * 5000)

        sa = archive.DatasetSpec(source=a, name="A", files=archive.walk_dataset(a))
        try:
            archive.pack_datasets([(1, sa)], ["20260728_0001"], target_bytes=1000)
        except RuntimeError as e:
            assert "文件过大" in str(e) or "too large" in str(e).lower()
            print("  OK  oversized file rejected")
            return
        raise AssertionError("expected RuntimeError")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_catalog_crud() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="cryoem_cat_"))
    try:
        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        catalog.ensure_tape(conn, "EM_data_1", archive.LTO6_RAW_BYTES)
        ds_id = catalog.ensure_dataset(conn, "Krios_2026", project="P", operator="alice")
        with catalog.transaction(conn):
            catalog.insert_archive(
                conn,
                name="20260728_0001",
                archive_size=99_500_000_000,
                uncompressed_size=100_000_000_000,
                sha256="a" * 64,
                tape_label="EM_data_1",
                file_number=1,
            )
            catalog.add_dataset_archive(conn, ds_id, "20260728_0001", 1)
            catalog.insert_files_batch(
                conn,
                [("20260728_0001", ds_id, "movie.mrc", 1024, "t"),
                 ("20260728_0001", ds_id, "gain.mrc", 4096, "t")],
            )
        new_count = catalog.increment_tape_archive_count(conn, "EM_data_1")
        assert new_count == 1
        assert catalog.find_in_use_tape(conn)["label"] == "EM_data_1"
        assert len(catalog.get_archives_for_dataset(conn, ds_id)) == 1
        conn.close()

        # Reopen and verify persistence
        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        assert catalog.get_archive(conn, "20260728_0001") is not None
        assert catalog.get_tape(conn, "EM_data_1")["archive_count"] == 1
        # Lazy next-archive-name allocation. 每次查询依赖上一次 INSERT 后的 MAX，
        # 所以要在两次 next_archive_name 之间插一行以模拟生产用法。
        n2 = catalog.next_archive_name(conn, "20260728")
        with catalog.transaction(conn):
            catalog.insert_archive(
                conn, name=n2, archive_size=1, uncompressed_size=1,
                sha256="b" * 64, tape_label="EM_data_1", file_number=2,
            )
        n3 = catalog.next_archive_name(conn, "20260728")
        with catalog.transaction(conn):
            catalog.insert_archive(
                conn, name=n3, archive_size=1, uncompressed_size=1,
                sha256="c" * 64, tape_label="EM_data_1", file_number=3,
            )
        n4 = catalog.next_archive_name(conn, "20260728")
        assert [n2, n3, n4] == ["20260728_0002", "20260728_0003", "20260728_0004"]
        conn.close()
        print("  OK  catalog CRUD + persistence")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ltfs_round_trip() -> None:
    """archive_writer + read_archive round-trip at the mount root."""
    import tape

    tmp = Path(tempfile.mkdtemp(prefix="cryoem_ltfs_"))
    try:
        mount = tmp / "ltfs"
        mount.mkdir()
        device = tape.TapeDevice(str(mount))

        payload = b"X" * 1000
        with device.archive_writer("archive.tar.zst", tape_label="EM_data_1") as sink:
            sink.write(payload)

        # File should be directly under the mount root.
        written = mount / "archive.tar.zst"
        assert written.exists() and written.stat().st_size == 1000

        dest = tmp / "restored.tar.zst"
        device.read_archive(dest, expected_bytes=1000, archive_name="archive.tar.zst")
        assert dest.read_bytes() == payload

        # A size that disagrees with the catalog is a hard error.
        try:
            device.read_archive(dest, expected_bytes=999, archive_name="archive.tar.zst")
        except RuntimeError as exc:
            assert "size mismatch" in str(exc)
        else:
            raise AssertionError("expected size mismatch error")

        device.remove_archive("archive.tar.zst")
        assert not written.exists()
        device.remove_archive("archive.tar.zst")  # idempotent, never raises

        # A missing mount point is reported, not silently tolerated.
        try:
            tape.TapeDevice(str(tmp / "nope")).read_archive(
                dest, expected_bytes=1, archive_name="x.tar.zst"
            )
        except RuntimeError as exc:
            assert "挂载点" in str(exc)
        else:
            raise AssertionError("expected missing-mount error")
        print("  OK  LTFS round-trip (files at mount root)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_transaction_is_reentrant() -> None:
    """A helper that opens its own transaction must be callable from inside
    an outer one — SQLite has no nested BEGIN, so this used to raise
    ``cannot start a transaction within a transaction``."""
    import sqlite3

    tmp = Path(tempfile.mkdtemp(prefix="cryoem_tx_"))
    try:
        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        catalog.ensure_tape(conn, "T1", archive.LTO6_RAW_BYTES)

        with catalog.transaction(conn):
            with catalog.transaction(conn):  # nested -> SAVEPOINT
                catalog.increment_tape_archive_count(conn, "T1")
        assert catalog.get_tape(conn, "T1")["archive_count"] == 1

        # Inner failure rolls back only the inner block.
        with catalog.transaction(conn):
            catalog.increment_tape_archive_count(conn, "T1")  # -> 2
            try:
                with catalog.transaction(conn):
                    catalog.increment_tape_archive_count(conn, "T1")  # -> 3
                    raise ValueError("boom")
            except ValueError:
                pass
        assert catalog.get_tape(conn, "T1")["archive_count"] == 2, "inner rollback failed"

        # Outer failure rolls back everything.
        try:
            with catalog.transaction(conn):
                catalog.increment_tape_archive_count(conn, "T1")
                raise ValueError("boom")
        except ValueError:
            pass
        assert catalog.get_tape(conn, "T1")["archive_count"] == 2
        assert not conn.in_transaction, "transaction left open"
        conn.close()
        print("  OK  transaction re-entrancy (nested SAVEPOINT)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_archive_contains_real_data() -> None:
    """End-to-end: files on disk -> tar in memory -> zstd -> LTFS mount.

    No staging tree on disk, no symlink step. The previous design
    (symlink tree under ``data/staging``) silently produced empty archives
    if anything in that directory disappeared mid-run — exactly the bug
    we are guarding against here.
    """
    try:
        utils.ensure_tool("zstd")
    except RuntimeError:
        print("  SKIP archive contents (zstd not installed)")
        return

    tmp = Path(tempfile.mkdtemp(prefix="cryoem_build_"))
    try:
        import tape as tape_mod

        src = tmp / "ds"
        src.mkdir()
        # Incompressible payload so any empty/header-only tar would show up
        # as a suspiciously small archive regardless of compression.
        payload = os.urandom(512 * 1024)
        (src / "movie.mrc").write_bytes(payload)
        (src / "sub").mkdir()
        (src / "sub" / "gain.mrc").write_bytes(payload)

        spec_ds = archive.DatasetSpec(source=src, name="DS", files=archive.walk_dataset(src))
        archives = archive.pack_datasets([(1, spec_ds)], ["20260728_0001"])
        assert len(archives) == 1

        mount = tmp / "ltfs"
        mount.mkdir()
        device = tape_mod.TapeDevice(str(mount))

        compressed, uncompressed, sha, rows, manifest_path = archive.build_and_write_archive(
            archives[0], device, "T1"
        )
        assert uncompressed == 2 * len(payload)
        assert compressed > len(payload), f"archive suspiciously small: {compressed} B"
        assert len(rows) == 2
        assert len(sha) == 64
        # dataset_lookup 未提供时不应生成 sidecar manifest
        assert manifest_path is None

        written = mount / "20260728_0001.tar.zst"
        assert written.exists() and written.stat().st_size == compressed
        assert utils.sha256_file(written) == sha, "streamed SHA256 != bytes on tape"
        print("  OK  archive holds real data, streamed to tape, hashed in one pass")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_incomplete_archive_removed_on_failure() -> None:
    """A write that dies mid-stream must not leave a truncated archive."""
    import tape as tape_mod

    tmp = Path(tempfile.mkdtemp(prefix="cryoem_enospc_"))
    try:
        mount = tmp / "ltfs"
        mount.mkdir()
        device = tape_mod.TapeDevice(str(mount))
        target = mount / "20260728_0001.tar.zst"

        # Generic failure: partial file removed, error propagates as-is.
        try:
            with device.archive_writer("20260728_0001.tar.zst", tape_label="T1") as sink:
                sink.write(b"partial")
                raise RuntimeError("drive exploded")
        except RuntimeError as exc:
            assert "drive exploded" in str(exc)
        assert not target.exists(), "partial archive left on tape"

        # ENOSPC is translated into the recoverable TapeFullError.
        try:
            with device.archive_writer("20260728_0001.tar.zst", tape_label="T1") as sink:
                sink.write(b"partial")
                raise OSError(errno.ENOSPC, "No space left on device")
        except tape_mod.TapeFullError as exc:
            assert "空间不足" in str(exc)
        else:
            raise AssertionError("expected TapeFullError")
        assert not target.exists(), "partial archive left on tape"

        assert device.free_bytes() is not None, "LTFS should report free space"
        print("  OK  incomplete archive removed; ENOSPC -> TapeFullError")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resume_skips_archived_files() -> None:
    """After an interrupted run, the next one continues from the catalog."""
    tmp = Path(tempfile.mkdtemp(prefix="cryoem_resume_"))
    try:
        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        catalog.ensure_tape(conn, "T1", archive.LTO6_RAW_BYTES)
        ds_id = catalog.ensure_dataset(conn, "DS")

        assert catalog.get_archived_rel_paths(conn, ds_id) == set()
        assert catalog.next_dataset_archive_sequence(conn, ds_id) == 1

        with catalog.transaction(conn):
            catalog.insert_archive(
                conn, name="20260728_0001", archive_size=10, uncompressed_size=20,
                sha256="a" * 64, tape_label="T1", file_number=1,
            )
            catalog.add_dataset_archive(
                conn, ds_id, "20260728_0001",
                catalog.next_dataset_archive_sequence(conn, ds_id),
            )
            catalog.insert_files_batch(
                conn, [("20260728_0001", ds_id, "a.mrc", 10, "t")]
            )

        assert catalog.get_archived_rel_paths(conn, ds_id) == {"a.mrc"}
        # Sequence continues across runs instead of restarting at 1.
        assert catalog.next_dataset_archive_sequence(conn, ds_id) == 2
        conn.close()
        print("  OK  resume state (archived paths + dataset_archive sequence)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_calculate_chunk_size() -> None:
    """需求 1 第 2 点的安全规则：以 50GB 向下取整 + 20GB 安全区间复测。

    用户原文档的 4 个示例（973/960/125/115 GB）都按 20GB safe_margin 计算，
    所以显式传参；默认参数则按 :data:`archive.SAFE_MARGIN_BYTES` 校验。
    """
    GB = 1_000_000_000
    safe = 20 * GB
    mult = 50 * GB

    # 边界：负数 / 零 → 0
    assert archive.calculate_chunk_size(-1) == 0
    assert archive.calculate_chunk_size(0) == 0

    # 70GB：取整 50，leftover 20 ≥ 20，不退档 → 50GB
    assert archive.calculate_chunk_size(70 * GB, safe, mult) == 50 * GB
    # 60GB：取整 50，leftover 10 < 20 → 退档到 0 → 返回 0
    assert archive.calculate_chunk_size(60 * GB, safe, mult) == 0
    # 80GB：取整 50，leftover 30 ≥ 20 → 50GB
    assert archive.calculate_chunk_size(80 * GB, safe, mult) == 50 * GB

    # 用户举例：973GB → 950GB
    assert archive.calculate_chunk_size(973 * GB, safe, mult) == 950 * GB
    # 用户举例：960GB → 900GB
    assert archive.calculate_chunk_size(960 * GB, safe, mult) == 900 * GB
    # 用户举例：125GB → 100GB
    assert archive.calculate_chunk_size(125 * GB, safe, mult) == 100 * GB
    # 用户举例：115GB → 50GB
    assert archive.calculate_chunk_size(115 * GB, safe, mult) == 50 * GB

    # 自定义参数：multiple=10, margin=5
    assert archive.calculate_chunk_size(27 * GB, safe_margin=5 * GB, multiple=10 * GB) == 20 * GB

    # 默认参数走项目常量 SAFE_MARGIN/CHUNK_MULTIPLE：与显式传相同值结果一致。
    assert (
        archive.calculate_chunk_size(973 * GB)
        == archive.calculate_chunk_size(973 * GB, archive.SAFE_MARGIN_BYTES, archive.CHUNK_MULTIPLE_BYTES)
    )
    print("  OK  calculate_chunk_size (边界 / 用户举例 / 自定义参数 / 默认常量)")


def test_dynamic_create_archives_loop() -> None:
    """create_archives 的主循环：剩余空间 → calculate_chunk_size → 写入 → 复测 → 换磁带。"""
    try:
        utils.ensure_tool("zstd")
    except RuntimeError:
        print("  SKIP dynamic packing (zstd not installed)")
        return

    import tape as tape_mod

    class StubDevice:
        """Reports a scripted sequence of free-space readings for each archive write."""

        def __init__(self, readings):
            self.readings = list(readings)

        def free_bytes(self):
            return self.readings.pop(0) if self.readings else None

        # Everything else just delegates to a real LTFS mount.
        def __getattr__(self, name):
            return getattr(self._real, name)

    tmp = Path(tempfile.mkdtemp(prefix="cryoem_dyn_"))
    original_prompt = tape_mod.prompt_for_full_tape_swap
    original_confirm = utils.confirm
    try:
        # 一组真实文件 -> tar，可走完 build_and_write_archive 链路
        src = tmp / "ds"
        src.mkdir()
        big = os.urandom(2 * 1024 * 1024)  # 2 MiB
        for i in range(20):
            (src / f"f_{i:02d}.mrc").write_bytes(big)
        ds_spec = archive.DatasetSpec(source=src, name="DS", files=archive.walk_dataset(src))

        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        catalog.ensure_tape(conn, "T1", archive.LTO6_RAW_BYTES)

        mount = tmp / "ltfs"
        mount.mkdir()
        real_device = tape_mod.TapeDevice(str(mount))

        # free=70GB（enough for 50GB chunk）× N 次：每个 archive 后我们观察真实剩余
        # → 全部走动态循环（不触发换磁带）
        utils.confirm = lambda *a, **kw: True  # 自动确认
        # Stub 仅拦截 free_bytes，其它方法走真实实现
        stub = StubDevice([70 * GB, 70 * GB, 70 * GB, 70 * GB, 70 * GB])
        stub._real = real_device

        written = archive.create_archives(
            conn, stub, [ds_spec], "T1", archive.LTO6_RAW_BYTES,
        )
        # 7 个文件每个 2MiB，约 0.03GiB 总大小；70GiB / 50GiB = 单包
        assert len(written) == 1, f"expected 1 archive, got {len(written)}"
        assert (mount / f"{written[0]['name']}.tar.zst").exists()
        assert (mount / f"{written[0]['name']}.manifest.json").exists()
        conn.close()
        print("  OK  create_archives dynamic loop (chunk via calculate_chunk_size)")
    finally:
        tape_mod.prompt_for_full_tape_swap = original_prompt
        utils.confirm = original_confirm
        shutil.rmtree(tmp, ignore_errors=True)


def test_manifest_sidecar_written() -> None:
    """确认 sidecar manifest 在写完 archive 之后被正确写出。"""
    try:
        utils.ensure_tool("zstd")
    except RuntimeError:
        print("  SKIP manifest sidecar (zstd not installed)")
        return

    import tape as tape_mod

    tmp = Path(tempfile.mkdtemp(prefix="cryoem_mf_"))
    try:
        src = tmp / "ds"
        src.mkdir()
        (src / "movie.mrc").write_bytes(os.urandom(64 * 1024))

        ds_spec = archive.DatasetSpec(
            source=src, name="DS",
            project="CryoEM_2026", operator="alice", comment="CM01",
            files=archive.walk_dataset(src),
        )
        spec = archive.ArchiveSpec(
            name="20260729_0001",
            files=[(1, "DS", src / "movie.mrc", "movie.mrc", 65536, "t")],
            estimated_size=65536,
        )
        dataset_lookup = {1: ds_spec}

        mount = tmp / "ltfs"
        mount.mkdir()
        device = tape_mod.TapeDevice(str(mount))

        _, _, sha, _, manifest_path = archive.build_and_write_archive(
            spec, device, "EM_data_1", dataset_lookup=dataset_lookup,
        )
        assert manifest_path is not None
        assert manifest_path.exists()

        import json as _json
        data = _json.loads(manifest_path.read_text(encoding="utf-8"))
        assert data["tape_label"] == "EM_data_1"
        assert data["project_name"] == "CryoEM_2026"
        assert data["dataset"] == "DS"
        assert any(d["name"] == "DS" for d in data["datasets"])
        assert len(data["file_list"]) == 1
        assert data["file_list"][0]["dataset"] == "DS"
        assert data["file_list"][0]["path"] == "DS/movie.mrc"
        assert data["archive_size"] > 0
        assert data["timestamp"]  # 非空
        print("  OK  manifest sidecar JSON 字段（tape_label/project/datasets/file_list/archive_size/timestamp）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_export_to_csv() -> None:
    """catalog.export_to_csv 应写出 5 个 UTF-8-SIG 编码的 CSV。"""
    tmp = Path(tempfile.mkdtemp(prefix="cryoem_csv_"))
    try:
        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        catalog.ensure_tape(conn, "EM_data_1", archive.LTO6_RAW_BYTES)
        ds_id = catalog.ensure_dataset(conn, "DS", project="P", operator="op")
        with catalog.transaction(conn):
            catalog.insert_archive(
                conn, name="20260729_0001", archive_size=100, uncompressed_size=200,
                sha256="a" * 64, tape_label="EM_data_1", file_number=1,
            )
            catalog.add_dataset_archive(conn, ds_id, "20260729_0001", 1)
            catalog.insert_files_batch(
                conn, [("20260729_0001", ds_id, "m.mrc", 200, "t")],
            )

        out = tmp / "out"
        paths = catalog.export_to_csv(conn, out)
        assert len(paths) == 5
        assert {p.name for p in paths} == {
            "tape.csv", "dataset.csv", "archive.csv",
            "dataset_archive.csv", "file.csv",
        }
        for p in paths:
            assert p.exists() and p.stat().st_size > 0

        # 验证 BOM
        archive_csv = out / "archive.csv"
        with open(archive_csv, "rb") as fh:
            assert fh.read(3) == b"\xef\xbb\xbf", "expected UTF-8-SIG BOM"

        # 验证表头 + 数据
        import csv as _csv
        with open(archive_csv, encoding="utf-8-sig", newline="") as fh:
            rows = list(_csv.reader(fh))
        assert rows[0][0] == "name"
        assert any(r[0] == "20260729_0001" for r in rows[1:])
        conn.close()
        print("  OK  export_to_csv (5 个 UTF-8-SIG CSV，含表头)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_constants() -> None:
    assert archive.TARGET_ARCHIVE_BYTES == 100_000_000_000
    assert archive.LTO6_RAW_BYTES == 2_500_000_000_000
    # 动态切分相关：安全余量必须小于最小切分基数，否则 chunk 计算退一档永远退不到 0。
    assert archive.SAFE_MARGIN_BYTES < archive.CHUNK_MULTIPLE_BYTES
    assert archive.CHUNK_MULTIPLE_BYTES == 50_000_000_000
    assert archive.SAFE_MARGIN_BYTES == 40_000_000_000
    print("  OK  constants (LTO-6 raw + 动态切分常量 sanity)")


def main() -> int:
    print("CryoEM archive smoke tests")
    print("=" * 60)
    test_constants()
    test_format_bytes()
    test_sha256_known_value()
    test_glob_to_like()
    test_parse_archive_name()
    test_calculate_chunk_size()
    test_packing_split()
    test_packing_all_in_one()
    test_packing_oversized_file()
    test_ltfs_round_trip()
    test_catalog_crud()
    test_transaction_is_reentrant()
    test_resume_skips_archived_files()
    test_incomplete_archive_removed_on_failure()
    test_archive_contains_real_data()
    test_manifest_sidecar_written()
    test_export_to_csv()
    test_dynamic_create_archives_loop()
    print("=" * 60)
    print("all smoke tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
