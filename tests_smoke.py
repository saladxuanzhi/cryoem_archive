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
    # 十进制单位（1 KB = 1000 B）：与项目容量常量、磁带标称容量同口径。
    assert utils.format_bytes(0) == "0 B"
    assert utils.format_bytes(999) == "999 B"
    assert utils.format_bytes(1024) == "1.02 KB"
    assert utils.format_bytes(1024 * 1024) == "1.05 MB"
    assert utils.format_bytes(100_000_000_000) == "100.00 GB"
    assert utils.format_bytes(2_500_000_000_000) == "2.50 TB"
    print("  OK  format_bytes (十进制单位)")


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
    """需求 1 第 2 点的安全规则：以 50GB 向下取整 + 安全区间复测。

    项目常量 :data:`archive.SAFE_MARGIN_BYTES` 定为 40GB（代码为准）；
    用户原文档的 4 个示例（973/960/125/115 GB）按 20GB safe_margin 计算，
    所以显式传参校验。默认参数（40GB）单独断言。
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

    # 默认参数（SAFE_MARGIN_BYTES=40GB，项目实际行为）：
    # 973GB：取整 950，leftover 23 < 40 -> 退一档 -> 900GB
    assert archive.calculate_chunk_size(973 * GB) == 900 * GB
    # 125GB：取整 100，leftover 25 < 40 -> 退一档 -> 50GB
    assert archive.calculate_chunk_size(125 * GB) == 50 * GB
    # 140GB：取整 100，leftover 40 >= 40（恰好等于安全区间，不退档）-> 100GB
    assert archive.calculate_chunk_size(140 * GB) == 100 * GB
    # 70GB：取整 50，leftover 20 < 40 -> 退档到 0 -> 换磁带
    assert archive.calculate_chunk_size(70 * GB) == 0
    print("  OK  calculate_chunk_size (边界 / 20GB 示例 / 自定义参数 / 默认 40GB 常量)")


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
    original_archive_confirm = archive.confirm
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
        # create_archives/walk_dataset 用的是 archive 命名空间里的 confirm
        # （from utils import confirm），必须补丁在 archive 上才生效。
        archive.confirm = lambda *a, **kw: True
        # Stub 仅拦截 free_bytes，其它方法走真实实现
        stub = StubDevice([70 * GB, 70 * GB, 70 * GB, 70 * GB, 70 * GB])
        stub._real = real_device

        written = archive.create_archives(
            conn, stub, [ds_spec], "T1", archive.LTO6_RAW_BYTES,
            backup_dir=tmp / "backups",
        )
        # 7 个文件每个 2MiB，约 0.03GiB 总大小；70GiB / 50GiB = 单包
        assert len(written) == 1, f"expected 1 archive, got {len(written)}"
        assert (mount / f"{written[0]['name']}.tar.zst").exists()
        assert (mount / f"{written[0]['name']}.manifest.json").exists()
        # 每成功入库一个 archive 应留一份目录库快照
        backups = list((tmp / "backups").glob("catalog-*.sqlite3"))
        assert len(backups) == 1, f"expected 1 catalog backup, got {len(backups)}"
        conn.close()
        print("  OK  create_archives dynamic loop (chunk via calculate_chunk_size)")
    finally:
        tape_mod.prompt_for_full_tape_swap = original_prompt
        utils.confirm = original_confirm
        archive.confirm = original_archive_confirm
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


def test_file_table_migration() -> None:
    """v1 (archive_name, rel_path) 主键的旧库打开时必须被无损迁移到 v2。

    v1 的隐患：一个 archive 混装多个 dataset 时，不同 dataset 的相同
    rel_path 会撞主键 -> insert_files_batch 失败 -> 整包被当 orphan 删除。
    """
    import sqlite3

    tmp = Path(tempfile.mkdtemp(prefix="cryoem_mig_"))
    try:
        db = tmp / "old.sqlite3"
        # 手工搭一个 v1 库（file 表主键没有 dataset_id）
        old = sqlite3.connect(str(db))
        old.executescript(catalog.SCHEMA.replace(
            "PRIMARY KEY (archive_name, dataset_id, rel_path)",
            "PRIMARY KEY (archive_name, rel_path)",
        ))
        old.execute(
            "INSERT INTO tape (label, capacity_bytes, archive_count, status, first_used, last_used) "
            "VALUES ('T1', 1, 0, 'in_use', 't', 't')"
        )
        old.execute("INSERT INTO dataset (name, create_time) VALUES ('DS', 't')")
        old.execute(
            "INSERT INTO archive (name, create_time, archive_size, uncompressed_size, sha256, tape_label, file_number) "
            "VALUES ('20260729_0001', 't', 1, 1, 'x', 'T1', 1)"
        )
        old.executemany(
            "INSERT INTO file (archive_name, dataset_id, rel_path, size, mtime) VALUES (?,?,?,?,?)",
            [("20260729_0001", 1, "Movies/a.mrc", 10, "t"),
             ("20260729_0001", 1, "Movies/b.mrc", 20, "t")],
        )
        old.commit()
        old.close()

        conn = catalog.open_db(str(db))  # 触发迁移
        n = conn.execute("SELECT COUNT(*) FROM file").fetchone()[0]
        assert n == 2, "migration must not lose rows"
        assert catalog._file_table_pk(conn) == catalog._FILE_TABLE_PK_COLUMNS

        # 迁移后，v1 时代必然失败的「同 archive 不同 dataset 相同 rel_path」
        # 现在可以入库了。
        conn.execute("INSERT INTO dataset (name, create_time) VALUES ('DS2', 't')")
        old_err = None
        try:
            conn.execute(
                "INSERT INTO file (archive_name, dataset_id, rel_path, size, mtime) "
                "VALUES ('20260729_0001', 2, 'Movies/a.mrc', 10, 't')"
            )
        except sqlite3.IntegrityError as exc:  # pragma: no cover
            old_err = exc
        assert old_err is None, f"duplicate rel_path across datasets still rejected: {old_err}"

        # 再次打开：不重复迁移，数据完好
        conn.close()
        conn = catalog.open_db(str(db))
        assert conn.execute("SELECT COUNT(*) FROM file").fetchone()[0] == 3
        conn.close()
        print("  OK  file 表 v1->v2 主键迁移（无损 + 可重入 + 多 dataset 共包）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_search_like_escape() -> None:
    """LIKE 无 ESCAPE 子句时，含下划线的 glob 必然搜不到（曾经的真实 bug）。"""
    tmp = Path(tempfile.mkdtemp(prefix="cryoem_like_"))
    try:
        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        catalog.ensure_tape(conn, "T1", archive.LTO6_RAW_BYTES)
        ds_id = catalog.ensure_dataset(conn, "DS")
        with catalog.transaction(conn):
            catalog.insert_archive(
                conn, name="20260729_0001", archive_size=1, uncompressed_size=1,
                sha256="a" * 64, tape_label="T1", file_number=1,
            )
            catalog.insert_files_batch(conn, [
                ("20260729_0001", ds_id, "Movies/foo_bar.mrc", 1, "t"),
                ("20260729_0001", ds_id, "Movies/foobar.mrc", 1, "t"),
                ("20260729_0001", ds_id, "a%b.mrc", 1, "t"),
                ("20260729_0001", ds_id, "axb.mrc", 1, "t"),
            ])

        hits = archive.search_files(conn, "*foo_bar.mrc")
        assert [h["path"] for h in hits] == ["Movies/foo_bar.mrc"], (
            f"underscore pattern broken: {hits}"
        )
        # 字面 % 也不应被当通配符
        hits = archive.search_files(conn, "a%b.mrc")
        assert [h["path"] for h in hits] == ["a%b.mrc"], (
            f"percent pattern broken: {hits}"
        )
        # glob 通配符本身仍然工作
        hits = archive.search_files(conn, "Movies/foo*.mrc")
        assert sorted(h["path"] for h in hits) == [
            "Movies/foo_bar.mrc", "Movies/foobar.mrc",
        ]
        conn.close()
        print("  OK  glob 搜索（字面 _ / % 正确转义）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_walk_dataset_warnings() -> None:
    """目录软链、空目录、stat 失败都必须可见，而不是静默丢数据。"""
    tmp = Path(tempfile.mkdtemp(prefix="cryoem_walk_"))
    try:
        src = tmp / "ds"
        src.mkdir()
        (src / "ok.mrc").write_bytes(b"x" * 10)
        (src / "empty").mkdir()                      # 空目录 -> 提示但不报错
        real = tmp / "real_dir"
        real.mkdir()
        (real / "inside.mrc").write_bytes(b"y" * 10)
        (src / "link").symlink_to(real, target_is_directory=True)  # 目录软链 -> 警告
        (src / "broken").symlink_to(tmp / "nope")    # 断链 -> stat 失败

        # 默认（非交互）：stat 失败必须中止，且错误里带文件路径。
        try:
            archive.walk_dataset(src, interactive=False)
        except RuntimeError as exc:
            assert "broken" in str(exc), f"error should name the bad file: {exc}"
        else:
            raise AssertionError("expected RuntimeError for unreadable file")

        # 交互模式下操作员选择跳过：返回的清单不含坏文件，好文件保留。
        original_confirm = archive.confirm
        archive.confirm = lambda *a, **kw: True
        try:
            files = archive.walk_dataset(src)
        finally:
            archive.confirm = original_confirm
        names = sorted(f[0].name for f in files)
        assert names == ["ok.mrc"], f"unexpected file list: {names}"

        # 目录软链内容（inside.mrc）确实不在清单里，且函数没因此报错。
        assert not any("inside.mrc" in str(f[0]) for f in files)
        print("  OK  walk_dataset（目录软链警告 / stat 失败中止或确认跳过 / 空目录提示）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_archived_file_meta_newest_wins() -> None:
    """文件被修改后重新归档：get_archived_file_meta 应取最新版本。"""
    tmp = Path(tempfile.mkdtemp(prefix="cryoem_meta_"))
    try:
        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        catalog.ensure_tape(conn, "T1", archive.LTO6_RAW_BYTES)
        ds_id = catalog.ensure_dataset(conn, "DS")
        with catalog.transaction(conn):
            for name, size in (("20260729_0001", 10), ("20260730_0001", 99)):
                catalog.insert_archive(
                    conn, name=name, archive_size=1, uncompressed_size=1,
                    sha256="a" * 64, tape_label="T1", file_number=1,
                )
                catalog.add_dataset_archive(
                    conn, ds_id, name, catalog.next_dataset_archive_sequence(conn, ds_id),
                )
                catalog.insert_files_batch(
                    conn, [(name, ds_id, "a.mrc", size, "t")],
                )
        meta = catalog.get_archived_file_meta(conn, ds_id)
        assert meta == {"a.mrc": (99, "t")}, f"newest version should win: {meta}"
        # 新旧两个版本都留在目录库（磁带上确实两份都在）
        n = conn.execute(
            "SELECT COUNT(*) FROM file WHERE dataset_id = ? AND rel_path = 'a.mrc'",
            (ds_id,),
        ).fetchone()[0]
        assert n == 2
        conn.close()
        print("  OK  get_archived_file_meta（重归档取最新版本，历史记录保留）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_backup_db() -> None:
    """backup API 快照可独立打开，且只保留最近 keep 份。"""
    import sqlite3

    tmp = Path(tempfile.mkdtemp(prefix="cryoem_bak_"))
    try:
        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        catalog.ensure_tape(conn, "T1", archive.LTO6_RAW_BYTES)
        bdir = tmp / "backups"
        p1 = catalog.backup_db(conn, bdir, keep=2)
        assert p1 is not None and p1.exists()
        # 快照可以独立打开且包含数据
        snap = sqlite3.connect(str(p1))
        assert snap.execute("SELECT COUNT(*) FROM tape WHERE label='T1'").fetchone()[0] == 1
        snap.close()
        # 写入更多数据后再备份两次 -> 只保留最近 2 份
        catalog.ensure_tape(conn, "T2", archive.LTO6_RAW_BYTES)
        catalog.backup_db(conn, bdir, keep=2)
        p3 = catalog.backup_db(conn, bdir, keep=2)
        backups = sorted(bdir.glob("catalog-*.sqlite3"))
        assert len(backups) == 2, f"keep=2 should prune old backups: {backups}"
        snap = sqlite3.connect(str(p3))
        assert snap.execute("SELECT COUNT(*) FROM tape").fetchone()[0] == 2
        snap.close()
        conn.close()
        print("  OK  backup_db（快照可独立打开 / keep 修剪）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_rebuild_catalog_from_manifests() -> None:
    """目录库丢失后，从磁带上的 manifest sidecar 重建记录（幂等）。"""
    import json as _json
    import tape as tape_mod

    tmp = Path(tempfile.mkdtemp(prefix="cryoem_rb_"))
    try:
        mount = tmp / "ltfs"
        mount.mkdir()
        device = tape_mod.TapeDevice(str(mount))

        # 两个 archive 的 manifest，同一盘磁带
        for name, ds_files in (
            ("20260729_0001", {"DS1": ["a.mrc", "b.mrc"]}),
            ("20260729_0002", {"DS1": ["c.mrc"], "DS2": ["d.mrc"]}),
        ):
            file_list = [
                {"dataset": ds, "path": f"{ds}/{rel}", "size": 10, "mtime": "t"}
                for ds, rels in ds_files.items()
                for rel in rels
            ]
            (mount / f"{name}.manifest.json").write_text(
                _json.dumps({
                    "manifest_version": "1.0",
                    "project_name": "P",
                    "tape_label": "T1",
                    "dataset": "DS1",
                    "datasets": [
                        {"id": 1, "name": "DS1", "project": "P", "operator": "op", "comment": ""},
                        {"id": 2, "name": "DS2", "project": "P", "operator": "op", "comment": ""},
                    ],
                    "file_list": file_list,
                    "timestamp": "2026-07-29T00:00:00+00:00",
                    "archive_size": 123,
                }, ensure_ascii=False),
                encoding="utf-8",
            )

        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        stats = archive.rebuild_catalog_from_manifests(
            conn, device, capacity_bytes=archive.LTO6_RAW_BYTES,
        )
        assert stats["archives"] == 2 and stats["files"] == 4
        assert stats["existing_archives"] == 0

        # tape/dataset/archive/file 行都恢复了；sha256 为空（manifest 里没有）
        assert catalog.get_tape(conn, "T1") is not None
        a1 = catalog.get_archive(conn, "20260729_0001")
        assert a1["sha256"] == "" and a1["archive_size"] == 123
        assert a1["file_number"] == 1
        assert catalog.get_archive(conn, "20260729_0002")["file_number"] == 2
        ds1 = catalog.get_dataset(conn, "DS1")
        assert ds1 is not None and ds1["project"] == "P"
        assert len(catalog.get_archives_for_dataset(conn, int(ds1["id"]))) == 2
        rel_paths = {
            r["rel_path"] for r in conn.execute("SELECT rel_path FROM file")
        }
        assert rel_paths == {"a.mrc", "b.mrc", "c.mrc", "d.mrc"}

        # 幂等：再跑一次不重复插入
        stats2 = archive.rebuild_catalog_from_manifests(
            conn, device, capacity_bytes=archive.LTO6_RAW_BYTES,
        )
        assert stats2["archives"] == 0 and stats2["existing_archives"] == 2
        assert conn.execute("SELECT COUNT(*) FROM file").fetchone()[0] == 4
        conn.close()
        print("  OK  rebuild_catalog_from_manifests（重建 + 幂等 + file_number 顺序）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_verify_archive_streaming_and_backfill() -> None:
    """verify_archive 流式校验；sha256 为空（重建行）时校验通过后回填。"""
    import tape as tape_mod

    tmp = Path(tempfile.mkdtemp(prefix="cryoem_vfy_"))
    try:
        mount = tmp / "ltfs"
        mount.mkdir()
        device = tape_mod.TapeDevice(str(mount))
        payload = os.urandom(300_000)
        (mount / "X.tar.zst").write_bytes(payload)
        real_sha = utils.sha256_file(mount / "X.tar.zst")

        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        catalog.ensure_tape(conn, "T1", archive.LTO6_RAW_BYTES)
        with catalog.transaction(conn):
            catalog.insert_archive(
                conn, name="X", archive_size=len(payload), uncompressed_size=0,
                sha256="", tape_label="T1", file_number=1,
            )
        result = archive.verify_archive(conn, device, "X", assume_mounted=True)
        assert result["ok"]
        # 空 sha256 已回填为真实值
        assert catalog.get_archive(conn, "X")["sha256"] == real_sha

        # 记录值不符 -> 报错（流式，不落盘）
        with catalog.transaction(conn):
            catalog.update_archive_sha256(conn, "X", "0" * 64)
        try:
            archive.verify_archive(conn, device, "X", assume_mounted=True)
        except RuntimeError as exc:
            assert "SHA256" in str(exc)
        else:
            raise AssertionError("expected SHA mismatch error")

        # 大小与目录库不符 -> 立刻失败
        with catalog.transaction(conn):
            conn.execute(
                "UPDATE archive SET archive_size = ? WHERE name = 'X'",
                (len(payload) + 1,),
            )
        try:
            archive.verify_archive(conn, device, "X", assume_mounted=True)
        except RuntimeError as exc:
            assert "大小" in str(exc) or "size" in str(exc).lower()
        else:
            raise AssertionError("expected size mismatch error")
        conn.close()
        print("  OK  verify_archive（流式 / 空 sha256 回填 / 不匹配报错）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_restore_streaming_and_filtered() -> None:
    """流式恢复：不再整包落盘，且只解出目标 dataset 的文件。"""
    try:
        utils.ensure_tool("zstd")
        utils.ensure_tool("tar")
    except RuntimeError:
        print("  SKIP streaming restore (zstd/tar not installed)")
        return

    import tape as tape_mod

    tmp = Path(tempfile.mkdtemp(prefix="cryoem_rst_"))
    original_confirm = archive.confirm
    original_prompt = tape_mod.prompt_for_tape_insertion
    try:
        # 两个 dataset 混装进同一个 archive
        src_a = tmp / "dsA"
        src_a.mkdir()
        (src_a / "a.mrc").write_bytes(os.urandom(64 * 1024))
        src_b = tmp / "dsB"
        src_b.mkdir()
        (src_b / "a.mrc").write_bytes(os.urandom(64 * 1024))  # 与 dsA 同名！
        (src_b / "b.mrc").write_bytes(os.urandom(64 * 1024))

        spec_a = archive.DatasetSpec(source=src_a, name="dsA", files=archive.walk_dataset(src_a))
        spec_b = archive.DatasetSpec(source=src_b, name="dsB", files=archive.walk_dataset(src_b))
        files = []
        for ds_id, sp in ((1, spec_a), (2, spec_b)):
            for p, s, m in sp.files:
                files.append((ds_id, sp.name, p, p.relative_to(sp.source).as_posix(), s, m))
        spec = archive.ArchiveSpec(
            name="20260729_0001", files=files,
            estimated_size=sum(f[4] for f in files),
        )

        mount = tmp / "ltfs"
        mount.mkdir()
        device = tape_mod.TapeDevice(str(mount))
        compressed, _, sha, file_rows, _ = archive.build_and_write_archive(
            spec, device, "T1",
        )

        conn = catalog.open_db(str(tmp / "test.sqlite3"))
        catalog.ensure_tape(conn, "T1", archive.LTO6_RAW_BYTES)
        ds_a = catalog.ensure_dataset(conn, "dsA")
        ds_b = catalog.ensure_dataset(conn, "dsB")
        with catalog.transaction(conn):
            catalog.insert_archive(
                conn, name="20260729_0001", archive_size=compressed,
                uncompressed_size=compressed, sha256=sha,
                tape_label="T1", file_number=1,
            )
            catalog.add_dataset_archive(conn, ds_a, "20260729_0001", 1)
            catalog.add_dataset_archive(conn, ds_b, "20260729_0001", 1)
            catalog.insert_files_batch(
                conn, [("20260729_0001", d, r, s, m) for d, r, s, m in file_rows],
            )

        archive.confirm = lambda *a, **kw: True
        tape_mod.prompt_for_tape_insertion = lambda label: label

        dest = tmp / "out"
        archive.restore_dataset(conn, device, "dsA", dest)
        # 只有 dsA 的文件被解出；dsB（包括与 dsA 同名的 a.mrc）不在
        assert (dest / "dsA" / "a.mrc").exists()
        assert not (dest / "dsB").exists(), "restore must not extract other datasets' files"

        # SHA 不符（翻转一个字节，保持大小一致以通过大小预检）
        # -> 报错并清掉本次解出的内容。损坏可能先在 zstd/tar 环节暴露，
        # 也可能撑到 SHA 比对才暴露 -- 两者都是正确的失败路径。
        arch_file = mount / "20260729_0001.tar.zst"
        corrupted = bytearray(arch_file.read_bytes())
        corrupted[0] ^= 0xFF
        arch_file.write_bytes(bytes(corrupted))
        dest2 = tmp / "out2"
        try:
            archive.restore_dataset(conn, device, "dsA", dest2)
        except RuntimeError:
            pass  # tar/zstd/SHA 任一环节失败都是正确行为（数据已损坏）
        else:
            raise AssertionError("expected failure on corrupted archive")
        assert not (dest2 / "dsA").exists(), "failed restore must clean up"
        conn.close()
        print("  OK  流式恢复（不落盘 / 按 dataset 过滤 / SHA 失败清理）")
    finally:
        archive.confirm = original_confirm
        tape_mod.prompt_for_tape_insertion = original_prompt
        shutil.rmtree(tmp, ignore_errors=True)



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
    test_file_table_migration()
    test_search_like_escape()
    test_walk_dataset_warnings()
    test_archived_file_meta_newest_wins()
    test_backup_db()
    test_rebuild_catalog_from_manifests()
    test_verify_archive_streaming_and_backfill()
    test_incomplete_archive_removed_on_failure()
    test_archive_contains_real_data()
    test_manifest_sidecar_written()
    test_export_to_csv()
    test_dynamic_create_archives_loop()
    test_restore_streaming_and_filtered()
    print("=" * 60)
    print("all smoke tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
