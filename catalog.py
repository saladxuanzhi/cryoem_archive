"""SQLite catalog: schema, transaction, CRUD.

Five tables:

* ``tape``            — one row per physical tape (label, capacity, archive_count, ...)
* ``dataset``         — one row per logical dataset (user-facing)
* ``archive``         — one row per ``.tar.zst`` (the physical write unit;
                       体积由 :func:`archive.calculate_chunk_size` 动态决定)
* ``dataset_archive`` — many-to-many: which archives contain which datasets, in order
* ``file``            — per-file metadata for restore and search

Every multi-row write goes through :func:`transaction`. Foreign keys are
enforced. The schema is small enough that schema changes are manual: bump
the constants in :data:`SCHEMA` and have the user re-init the catalog.
"""

from __future__ import annotations

import contextlib
import csv
import itertools
import logging
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from utils import now_iso

_LOGGER = logging.getLogger("cryoem_archive")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tape (
    label          TEXT PRIMARY KEY,
    capacity_bytes INTEGER NOT NULL,
    archive_count  INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL CHECK (status IN ('empty','in_use','full','retired')),
    first_used     TEXT NOT NULL,
    last_used      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,
    project     TEXT,
    operator    TEXT,
    comment     TEXT,
    create_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS archive (
    name              TEXT PRIMARY KEY,
    create_time       TEXT NOT NULL,
    archive_size      INTEGER NOT NULL,    -- tar.zst size on tape (动态切分)
    uncompressed_size INTEGER NOT NULL,    -- sum of files in the archive
    sha256            TEXT NOT NULL,
    tape_label        TEXT NOT NULL REFERENCES tape(label),
    file_number       INTEGER NOT NULL    -- 1-based write order on the tape
);

CREATE TABLE IF NOT EXISTS dataset_archive (
    dataset_id   INTEGER NOT NULL REFERENCES dataset(id)   ON DELETE CASCADE,
    archive_name TEXT    NOT NULL REFERENCES archive(name) ON DELETE CASCADE,
    sequence     INTEGER NOT NULL,        -- 1-based order within this dataset
    PRIMARY KEY (dataset_id, archive_name)
);

CREATE TABLE IF NOT EXISTS file (
    archive_name TEXT    NOT NULL REFERENCES archive(name) ON DELETE CASCADE,
    dataset_id   INTEGER NOT NULL REFERENCES dataset(id)   ON DELETE CASCADE,
    rel_path     TEXT    NOT NULL,
    size         INTEGER NOT NULL,
    mtime        TEXT    NOT NULL,
    PRIMARY KEY (archive_name, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_dataset_archive_order ON dataset_archive(dataset_id, sequence);
CREATE INDEX IF NOT EXISTS idx_archive_tape          ON archive(tape_label);
CREATE INDEX IF NOT EXISTS idx_file_dataset          ON file(dataset_id);
"""

# --- connection / transactions ----------------------------------------------


def open_db(path: str) -> sqlite3.Connection:
    """Open (or create) the catalog database. Apply schema if missing."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


_SAVEPOINT_SEQ = itertools.count(1)


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` / ``COMMIT`` / ``ROLLBACK`` block.

    Re-entrant. SQLite has no nested ``BEGIN``: issuing one inside an open
    transaction raises ``cannot start a transaction within a transaction``.
    So when a transaction is already active on this connection we open a
    ``SAVEPOINT`` instead — the inner block still rolls back independently
    on failure, but only the outermost block commits.

    This makes it safe for a helper that owns its own transaction to be
    called from inside a larger one (e.g. ``increment_tape_archive_count``
    from :func:`archive.create_archives`).
    """
    nested = conn.in_transaction
    savepoint = f"sp_{next(_SAVEPOINT_SEQ)}" if nested else ""
    if nested:
        conn.execute(f"SAVEPOINT {savepoint}")
    else:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            if nested:
                # ROLLBACK TO leaves the savepoint on the stack; RELEASE pops it.
                conn.execute(f"ROLLBACK TO {savepoint}")
                conn.execute(f"RELEASE {savepoint}")
            else:
                conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        if nested:
            conn.execute(f"RELEASE {savepoint}")
        else:
            conn.execute("COMMIT")


# --- archive naming -----------------------------------------------------------


_NAME_RE = re.compile(r"^(\d{8})_(\d{4})$")


def parse_archive_name(name: str) -> tuple[str, int]:
    m = _NAME_RE.match(name)
    if not m:
        raise ValueError(f"invalid archive name: {name!r}")
    return m.group(1), int(m.group(2))


def next_archive_name(conn: sqlite3.Connection, today: str) -> str:
    """Return the next sequential archive name for the given UTC day.

    懒分配：每次调用只为下一个 archive 预留名字，让动态切分循环边写边取，
    避免「一次性预分配 N 个，结果实际只写了 M 个」的浪费。
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT name FROM archive WHERE name LIKE ? ORDER BY name DESC LIMIT 1",
            (f"{today}_%",),
        ).fetchone()
        if row is None:
            return f"{today}_0001"
        existing_date, existing_seq = parse_archive_name(row["name"])
        if existing_date == today:
            return f"{today}_{existing_seq + 1:04d}"
        return f"{today}_0001"


# --- tape CRUD ----------------------------------------------------------------


def get_tape(conn: sqlite3.Connection, label: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tape WHERE label = ?", (label,)).fetchone()


def find_in_use_tape(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM tape WHERE status = 'in_use' ORDER BY last_used DESC LIMIT 1"
    ).fetchone()


def list_tapes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM tape ORDER BY label"))


def next_file_number(conn: sqlite3.Connection, tape_label: str) -> int:
    """Next 1-based write-order number on a tape.

    On LTFS archives are addressed by filename, so this is bookkeeping and
    display ("第 3/23 个") rather than a seek position.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(file_number), 0) AS n FROM archive WHERE tape_label = ?",
        (tape_label,),
    ).fetchone()
    return int(row["n"]) + 1


TAPE_STATUSES = ("empty", "in_use", "full", "retired")


def ensure_tape(
    conn: sqlite3.Connection,
    label: str,
    capacity_bytes: int,
    *,
    status: str = "in_use",
) -> None:
    """Create the tape row if it does not exist; otherwise refresh it.

    ``status`` is applied to existing rows too, not just new ones. This is
    only ever called for a tape we are about to write to, and a tape the
    operator has just (re-)inserted as the destination must not stay
    ``full`` in the ledger while it takes writes — otherwise
    :func:`find_in_use_tape` stops seeing the tape that is actually loaded.
    """
    if status not in TAPE_STATUSES:
        raise ValueError(f"invalid tape status: {status!r}")
    now = now_iso()
    existing = get_tape(conn, label)
    with transaction(conn):
        if existing is None:
            conn.execute(
                "INSERT INTO tape (label, capacity_bytes, archive_count, status, first_used, last_used) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (label, capacity_bytes, status, now, now),
            )
        else:
            conn.execute(
                "UPDATE tape SET last_used = ?, status = ? WHERE label = ?",
                (now, status, label),
            )
        if status == "in_use":
            # One drive, one loaded tape: nothing else can still be in use.
            conn.execute(
                "UPDATE tape SET status = 'full' WHERE status = 'in_use' AND label != ?",
                (label,),
            )


def set_tape_status(conn: sqlite3.Connection, label: str, status: str) -> None:
    if status not in TAPE_STATUSES:
        raise ValueError(f"invalid tape status: {status!r}")
    with transaction(conn):
        conn.execute("UPDATE tape SET status = ? WHERE label = ?", (status, label))


def increment_tape_archive_count(conn: sqlite3.Connection, label: str) -> int:
    """Bump ``archive_count`` by 1 and return the new count.

    Caller owns the transaction.
    """
    conn.execute(
        "UPDATE tape SET archive_count = archive_count + 1 WHERE label = ?",
        (label,),
    )
    row = conn.execute(
        "SELECT archive_count FROM tape WHERE label = ?", (label,)
    ).fetchone()
    return int(row["archive_count"])


# --- dataset CRUD -------------------------------------------------------------


def get_dataset(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM dataset WHERE name = ?", (name,)).fetchone()


def list_datasets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM dataset ORDER BY name"))


def ensure_dataset(
    conn: sqlite3.Connection,
    name: str,
    *,
    project: str | None = None,
    operator: str | None = None,
    comment: str | None = None,
) -> int:
    """Insert a dataset row (idempotent on name) and return its id."""
    existing = get_dataset(conn, name)
    if existing is not None:
        return int(existing["id"])
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO dataset (name, project, operator, comment, create_time) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, project, operator, comment, now_iso()),
        )
        return int(cur.lastrowid)


# --- archive CRUD -------------------------------------------------------------


def get_archive(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM archive WHERE name = ?", (name,)).fetchone()


def list_archives(conn: sqlite3.Connection, *, tape_label: str | None = None) -> list[sqlite3.Row]:
    if tape_label is not None:
        return list(
            conn.execute(
                "SELECT * FROM archive WHERE tape_label = ? ORDER BY file_number",
                (tape_label,),
            )
        )
    return list(conn.execute("SELECT * FROM archive ORDER BY name"))


def insert_archive(
    conn: sqlite3.Connection,
    *,
    name: str,
    archive_size: int,
    uncompressed_size: int,
    sha256: str,
    tape_label: str,
    file_number: int,
    create_time: str | None = None,
) -> None:
    """Insert one archive row. Caller is responsible for the transaction."""
    if create_time is None:
        create_time = now_iso()
    conn.execute(
        "INSERT INTO archive "
        "(name, create_time, archive_size, uncompressed_size, sha256, tape_label, file_number) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, create_time, archive_size, uncompressed_size, sha256, tape_label, file_number),
    )


# --- dataset_archive CRUD -----------------------------------------------------


def add_dataset_archive(
    conn: sqlite3.Connection, dataset_id: int, archive_name: str, sequence: int
) -> None:
    """Insert one (dataset, archive) link. Caller owns the transaction."""
    conn.execute(
        "INSERT INTO dataset_archive (dataset_id, archive_name, sequence) "
        "VALUES (?, ?, ?)",
        (dataset_id, archive_name, sequence),
    )


def get_archives_for_dataset(
    conn: sqlite3.Connection, dataset_id: int
) -> list[sqlite3.Row]:
    """Return archives holding this dataset, ordered by sequence."""
    return list(
        conn.execute(
            "SELECT a.*, da.sequence "
            "FROM archive a JOIN dataset_archive da ON da.archive_name = a.name "
            "WHERE da.dataset_id = ? "
            "ORDER BY da.sequence, a.name",
            (dataset_id,),
        )
    )


def next_dataset_archive_sequence(conn: sqlite3.Connection, dataset_id: int) -> int:
    """Next 1-based ``sequence`` for this dataset's archive chain.

    Derived from the table rather than a per-run counter so that a run
    resuming an interrupted archive continues the numbering instead of
    restarting it at 1.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS n FROM dataset_archive WHERE dataset_id = ?",
        (dataset_id,),
    ).fetchone()
    return int(row["n"]) + 1


def get_archived_rel_paths(conn: sqlite3.Connection, dataset_id: int) -> set[str]:
    """Every ``rel_path`` already committed to tape for this dataset.

    This is what makes an interrupted run resumable: rows land in ``file``
    only in the same transaction that records the archive, so anything in
    here is known-good on tape and can be skipped next time.
    """
    return {
        row["rel_path"]
        for row in conn.execute(
            "SELECT rel_path FROM file WHERE dataset_id = ?", (dataset_id,)
        )
    }


# --- file CRUD ----------------------------------------------------------------


def insert_file(
    conn: sqlite3.Connection,
    *,
    archive_name: str,
    dataset_id: int,
    rel_path: str,
    size: int,
    mtime: str,
) -> None:
    """Insert one file row. Caller owns the transaction."""
    conn.execute(
        "INSERT INTO file (archive_name, dataset_id, rel_path, size, mtime) "
        "VALUES (?, ?, ?, ?, ?)",
        (archive_name, dataset_id, rel_path, size, mtime),
    )


def insert_files_batch(
    conn: sqlite3.Connection,
    rows: list[tuple[str, int, str, int, str]],
) -> None:
    """Bulk insert. Each row: ``(archive_name, dataset_id, rel_path, size, mtime)``.

    Caller owns the transaction.
    """
    if not rows:
        return
    conn.executemany(
        "INSERT INTO file (archive_name, dataset_id, rel_path, size, mtime) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )


def search_files(
    conn: sqlite3.Connection,
    pattern_sql: str,
    *,
    dataset_name: str | None = None,
    archive_name: str | None = None,
    tape_label: str | None = None,
) -> list[sqlite3.Row]:
    """Return rows whose ``rel_path`` matches the SQL ``LIKE`` pattern.

    Translation from shell glob to ``LIKE`` happens in :mod:`archive`.
    """
    sql = (
        "SELECT f.archive_name, f.dataset_id, f.rel_path, f.size, f.mtime, "
        "       d.name AS dataset_name, a.tape_label "
        "FROM file f "
        "JOIN dataset d ON d.id = f.dataset_id "
        "JOIN archive a ON a.name = f.archive_name "
        "WHERE f.rel_path LIKE ?"
    )
    args: list = [pattern_sql]
    if dataset_name is not None:
        sql += " AND d.name = ?"
        args.append(dataset_name)
    if archive_name is not None:
        sql += " AND f.archive_name = ?"
        args.append(archive_name)
    if tape_label is not None:
        sql += " AND a.tape_label = ?"
        args.append(tape_label)
    sql += " ORDER BY f.archive_name, f.rel_path"
    return list(conn.execute(sql, args))


# --- display helpers ----------------------------------------------------------


# --- CSV export -------------------------------------------------------------


# 5 张业务表的固定顺序。导出按此顺序生成 5 个 CSV 文件，便于多次导出对比。
EXPORT_TABLES: tuple[str, ...] = (
    "tape",
    "dataset",
    "archive",
    "dataset_archive",
    "file",
)


def export_to_csv(conn: sqlite3.Connection, output_dir: Path) -> list[Path]:
    """将 SQLite 目录库导出为 5 个标准 CSV 文件。

    命名规则：每张表对应 ``<output_dir>/<table>.csv``。UTF-8-SIG 编码（BOM）
    以确保 Excel / WPS 打开 CSV 时能正确识别中文。

    Args:
        conn: 目标 SQLite 连接（不会修改，仅 ``SELECT``）。
        output_dir: 输出目录；如不存在会自动创建。

    Returns:
        已生成 CSV 文件的路径列表（5 个；空表也会生成仅有表头的文件）。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for table in EXPORT_TABLES:
        path = output_dir / f"{table}.csv"
        cur = conn.execute(f"SELECT * FROM {table}")
        # 拿表头（即便表为空也能写出 header）
        columns = [d[0] for d in cur.description] if cur.description else []
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            if columns:
                writer.writerow(columns)
            # sqlite3.Row 取值要做 None 兼容：NULL 字段直接写空字符串。
            for row in cur:
                writer.writerow([
                    "" if row[col] is None else row[col] for col in columns
                ])
        paths.append(path)
    return paths


def format_tape_row(row: sqlite3.Row) -> str:
    cap = int(row["capacity_bytes"])
    n = int(row["archive_count"])
    return (
        f"  {row['label']:<24} status={row['status']:<8} "
        f"archives={n:<4}  capacity={cap:<14d}  last_used={row['last_used']}"
    )


def format_archive_row(row: sqlite3.Row) -> str:
    return (
        f"  {row['name']}  size={row['archive_size']:>14d}  "
        f"uncompressed={row['uncompressed_size']:>14d}  "
        f"sha256={row['sha256'][:12]}...  tape={row['tape_label']}  "
        f"file#={row['file_number']}"
    )


def format_dataset_row(row: sqlite3.Row) -> str:
    return (
        f"  id={row['id']:>3}  {row['name']:<32}  "
        f"project={row['project'] or '-':<20}  "
        f"operator={row['operator'] or '-':<12}  "
        f"created={row['create_time']}"
    )
