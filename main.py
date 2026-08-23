"""CryoEM LTO-6 磁带归档工具 — 交互式主菜单。

磁带通过 LTFS 挂载为文件系统，每个 archive 是挂载点下的一个文件。

固定配置集中在文件顶部。运行：

    python main.py                                  # 交互式菜单
    python main.py /path/to/dataset                 # 直接进入「创建 Archives」
    python main.py /path --ltfs-mount /mnt/ltfs    # 指定 LTFS 挂载点

任何命令行未提供的参数都会在运行时由程序交互式询问。
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from datetime import datetime
from pathlib import Path

import archive
import catalog
import tape
from utils import (
    confirm,
    format_bytes,
    press_enter,
    prompt,
    setup_logging,
)

# ============================================================================
# 固定配置 — 修改这里即可
# ============================================================================

# 所有本地数据（数据库、日志、暂存目录）都放在程序目录下的 data/ 子目录中。
# 如需更改安装位置，只需移动整个项目目录；无需担心绝对路径散落在系统中。
_PROGRAM_DIR: Path = Path(__file__).resolve().parent
DATA_DIR:     Path = _PROGRAM_DIR / "data"

DATABASE:    Path = DATA_DIR / "catalog.sqlite3"
LOGFILE:     Path = DATA_DIR / "cryoem_archive.log"

LTFS_MOUNT_DEFAULT: str = "/mnt/ltfs"
"""Where the tape is mounted. Archives are written as files directly here."""

LTO6_CAPACITY: int = archive.LTO6_RAW_BYTES     # 2.5 TB
"""Tape raw capacity. Used when creating a new tape row.

注意这是磁带**裸容量**（厂商标称）：LTFS 格式化 + 索引开销后实际可用约少
10%（~2.2TB）。该值只用于台账记录与展示，写入决策一律以
``TapeDevice.free_bytes()`` 的实测剩余空间为准，所以偏大不会导致写满。
"""

BACKUP_DIR: Path = DATA_DIR / "backups"
"""目录库自动备份目录：每成功写入一个 archive 快照一次，保留最近
:data:`catalog.BACKUP_KEEP` 份。目录库是整套归档的唯一完整索引
（磁带上的 manifest sidecar 缺 sha256），丢了它恢复/校验都会很麻烦。"""


# ============================================================================
# 主菜单
# ============================================================================

MENU = """
========================================
  CryoEM 磁带归档工具
========================================
  1 创建 Archives（多 Dataset → 动态切分）
  2 恢复 Dataset（自动按序提取所有 archive）
  3 查询（datasets / archives / 文件）
  4 校验磁带
  5 查看台账
  6 导出台账 (CSV)
  7 重建目录库（从磁带 manifest）
  0 退出
========================================
"""


# ============================================================================
# CLI 解析
# ============================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the argparse parser.

    Only one positional argument is accepted: a source directory that
    pre-fills the first Dataset in the create flow. All other fields are
    prompted interactively.
    """
    p = argparse.ArgumentParser(
        prog="main.py",
        description="CryoEM 磁带归档工具（交互式）。",
    )
    p.add_argument(
        "source",
        nargs="?",
        help=(
            "可选：第一个 Dataset 的数据目录。"
            "指定后将直接进入「创建 Archives」，其他参数交互式补齐。"
        ),
    )
    p.add_argument(
        "--dataset",
        help="Dataset 名称（CLI 预填）。",
    )
    p.add_argument(
        "--project",
        help="Project 名称（CLI 预填）。",
    )
    p.add_argument(
        "--operator",
        help="操作员（CLI 预填）。",
    )
    p.add_argument(
        "--comment",
        help="备注（CLI 预填）。",
    )
    p.add_argument(
        "--tape-label",
        dest="tape_label",
        help="目标磁带标签（CLI 预填）。",
    )
    p.add_argument(
        "--ltfs-mount",
        dest="ltfs_mount",
        default=LTFS_MOUNT_DEFAULT,
        help=f"LTFS 挂载目录（默认 {LTFS_MOUNT_DEFAULT}）。",
    )
    p.add_argument(
        "--db",
        dest="db",
        help="目录库 SQLite 文件路径（默认 data/catalog.sqlite3）。",
    )
    p.add_argument(
        "--log",
        dest="log",
        help="日志文件路径（默认 data/cryoem_archive.log）。",
    )
    return p


# Settings that can be overridden by CLI args.
_db_path: Path = DATABASE
_log_path: Path = LOGFILE
_ltfs_mount: str = LTFS_MOUNT_DEFAULT


def apply_cli_overrides(args: argparse.Namespace) -> None:
    """Override module-level paths from CLI args."""
    global _db_path, _log_path, _ltfs_mount
    if args.db:
        _db_path = Path(args.db).expanduser()
    if args.log:
        _log_path = Path(args.log).expanduser()
    if args.ltfs_mount:
        _ltfs_mount = args.ltfs_mount


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 on clean exit, non-zero on error."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    apply_cli_overrides(args)

    # Ensure the data directory exists.
    _db_path.parent.mkdir(parents=True, exist_ok=True)

    logger = setup_logging(str(_log_path))
    logger.info(
        "cryoem-archive starting; ltfs=%s db=%s", _ltfs_mount, _db_path,
    )

    conn = catalog.open_db(str(_db_path))
    device = tape.TapeDevice(_ltfs_mount)

    try:
        # If a source directory was given on the CLI, go straight to create.
        if args.source:
            try:
                do_create_prefilled(conn, device, logger, args)
            except KeyboardInterrupt:
                print("\n  已中断。")
                return 130
            return 0

        # Otherwise, show the interactive menu.
        while True:
            print(MENU)
            try:
                choice = prompt("请选择")
            except EOFError:
                print()
                return 0
            print()
            try:
                if choice == "1":
                    do_create(conn, device, logger)
                elif choice == "2":
                    do_restore(conn, device, logger)
                elif choice == "3":
                    do_search(conn, logger)
                elif choice == "4":
                    do_verify_tape(conn, device, logger)
                elif choice == "5":
                    do_show_catalog(conn, logger)
                elif choice == "6":
                    do_export_csv(conn, logger)
                elif choice == "7":
                    do_rebuild_catalog(conn, device, logger)
                elif choice == "0":
                    print("再见。")
                    return 0
                else:
                    print(f"  无效选择：{choice!r}")
            except KeyboardInterrupt:
                print("\n  菜单项已中断。")
            except Exception as exc:
                logger.exception("menu item failed")
                print(f"  失败：{exc}")
    except KeyboardInterrupt:
        print("\n中断。")
        return 130
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass


# ============================================================================
# 菜单项
# ============================================================================


def do_create_prefilled(
    conn,
    device: tape.TapeDevice,
    logger: logging.Logger,
    args: argparse.Namespace,
) -> None:
    """Run the create flow with CLI-provided fields pre-filled.

    Anything missing from the CLI is prompted for.
    """
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        raise RuntimeError(f"{source} 不是目录")

    dataset_name = args.dataset or prompt("Dataset 名称", default=source.name)
    if not dataset_name:
        raise RuntimeError("Dataset 名称不能为空")
    project = args.project or prompt("Project（可留空）", default="") or None
    operator = args.operator or prompt("Operator（可留空）", default="") or None
    comment = args.comment or prompt("Comment（可留空）", default="") or None

    spec = archive.DatasetSpec(
        source=source,
        name=dataset_name,
        project=project,
        operator=operator,
        comment=comment,
    )

    in_use = catalog.find_in_use_tape(conn)
    if in_use is not None:
        default_label = in_use["label"]
        print(f"  当前正在使用的磁带：{default_label}")
    else:
        default_label = ""
    tape_label = args.tape_label or prompt("请输入目标磁带标签", default=default_label)
    if not tape_label:
        raise RuntimeError("磁带标签不能为空")

    print()
    print(f"  Source    : {source}")
    print(f"  Dataset   : {dataset_name}")
    print(f"  Project   : {project or '-'}")
    print(f"  Operator  : {operator or '-'}")
    print(f"  Comment   : {comment or '-'}")
    print(f"  Tape Label: {tape_label}")
    print(f"  LTFS      : {_ltfs_mount}")
    if not confirm("确认开始？", default=True):
        print("  已取消。")
        return

    written = archive.create_archives(
        conn,
        device,
        [spec],
        tape_label=tape_label,
        capacity_bytes=LTO6_CAPACITY,
        backup_dir=BACKUP_DIR,
    )

    print()
    print(f"  ✓ 全部完成：{len(written)} 个 archive")
    for w in written:
        print(
            f"    {w['name']}  {format_bytes(w['compressed_size'])}  "
            f"磁带 {w['tape_label']} file#{w['file_number']}  "
            f"含 {w['file_count']} 个文件"
        )


# ============================================================================
# 菜单项
# ============================================================================


def do_create(conn, device: tape.TapeDevice, logger: logging.Logger) -> None:
    """Menu item 1: pack one or more Datasets into dynamically-sized archives."""
    print("[1] 创建 Archives")
    print("-" * 60)

    # 1) Collect dataset specs from the operator.
    datasets: list[archive.DatasetSpec] = []
    while True:
        spec = _prompt_for_dataset(len(datasets) + 1)
        if spec is None:
            break
        datasets.append(spec)
        if not confirm("继续添加 Dataset？", default=False):
            break

    if not datasets:
        print("  没有输入任何 Dataset，已取消。")
        return

    # 2) Pick the destination tape (current in-use, or ask).
    in_use = catalog.find_in_use_tape(conn)
    if in_use is not None:
        default_label = in_use["label"]
        print(f"  当前正在使用的磁带：{default_label}")
    else:
        default_label = ""
    tape_label = prompt("请输入目标磁带标签", default=default_label)
    if not tape_label:
        print("  错误：磁带标签不能为空。")
        return

    # 3) Run the end-to-end create.
    try:
        written = archive.create_archives(
            conn,
            device,
            datasets,
            tape_label=tape_label,
            capacity_bytes=LTO6_CAPACITY,
            backup_dir=BACKUP_DIR,
        )
    except Exception:
        logger.exception("create_archives failed")
        raise

    print()
    print(f"  ✓ 全部完成：{len(written)} 个 archive")
    for w in written:
        print(
            f"    {w['name']}  {format_bytes(w['compressed_size'])}  "
            f"磁带 {w['tape_label']} file#{w['file_number']}  "
            f"含 {w['file_count']} 个文件"
        )
    press_enter()


def _prompt_for_dataset(idx: int) -> archive.DatasetSpec | None:
    """Prompt the operator for one dataset spec, or None to stop."""
    print()
    print(f"  [Dataset #{idx}]")
    if idx > 1 and not confirm("  继续添加？", default=True):
        return None

    source_str = prompt("  数据目录", default=str(Path.cwd()))
    source = Path(source_str).expanduser().resolve()
    if not source.is_dir():
        print(f"  错误：{source} 不是目录。")
        return None
    name = prompt("  Dataset 名称", default=source.name)
    if not name:
        print("  Dataset 名称不能为空。")
        return None
    project = prompt("  Project（可留空）", default="") or None
    operator = prompt("  Operator（可留空）", default="") or None
    comment = prompt("  Comment（可留空）", default="") or None
    return archive.DatasetSpec(
        source=source,
        name=name,
        project=project,
        operator=operator,
        comment=comment,
    )


def do_restore(conn, device: tape.TapeDevice, logger: logging.Logger) -> None:
    """Menu item 2: restore a Dataset."""
    print("[2] 恢复 Dataset")
    print("-" * 60)
    name = prompt("请输入 Dataset 名称")
    if not name:
        return
    dataset = catalog.get_dataset(conn, name)
    if dataset is None:
        print(f"  未找到 Dataset {name!r}")
        return
    archives = catalog.get_archives_for_dataset(conn, int(dataset["id"]))
    if not archives:
        print(f"  Dataset {name!r} 没有关联任何 archive。")
        return
    print(f"  Dataset {name!r} 涉及 {len(archives)} 个 archive：")
    for i, a in enumerate(archives, 1):
        print(
            f"    {i:>3}. {a['name']}  磁带 {a['tape_label']}  "
            f"file#={a['file_number']}  {format_bytes(a['archive_size'])}"
        )
    default_dest = str(Path.cwd() / f"restore_{name}")
    dest_str = prompt("请输入恢复目标目录", default=default_dest)
    dest = Path(dest_str).expanduser().resolve()
    if dest.exists() and any(dest.iterdir()):
        if not confirm(f"{dest} 非空，是否继续？", default=False):
            print("  已取消。")
            return
    try:
        archive.restore_dataset(conn, device, name, dest)
    except Exception:
        logger.exception("restore_dataset failed")
        raise
    press_enter()


def do_search(conn, logger: logging.Logger) -> None:
    """Menu item 3: search/list."""
    print("[3] 查询")
    print("-" * 60)
    print("  1 列出所有 Dataset")
    print("  2 列出所有 Archive")
    print("  3 按文件 glob 搜索")
    sub = prompt("请选择")
    print()
    if sub == "1":
        rows = catalog.list_datasets(conn)
        if not rows:
            print("  （无）")
        else:
            for r in rows:
                print(catalog.format_dataset_row(r))
    elif sub == "2":
        rows = catalog.list_archives(conn)
        if not rows:
            print("  （无）")
        else:
            for r in rows:
                print(catalog.format_archive_row(r))
    elif sub == "3":
        pattern = prompt("glob 模式（如 *.mrc）")
        if not pattern:
            return
        hits = archive.search_files(conn, pattern)
        if not hits:
            print("  无匹配。")
        else:
            print(f"  找到 {len(hits)} 个匹配：")
            for h in hits:
                print(
                    f"    {h['archive']} (dataset={h['dataset']}, tape={h['tape']}): {h['path']}"
                )
    else:
        print("  无效选择。")
    press_enter()


def do_verify_tape(conn, device: tape.TapeDevice, logger: logging.Logger) -> None:
    """Menu item 4: verify a tape by reading every archive."""
    print("[4] 校验磁带")
    print("-" * 60)
    label = prompt("请输入磁带标签")
    if not label:
        return
    archives = catalog.list_archives(conn, tape_label=label)
    if not archives:
        print(f"  磁带 {label} 上没有 archive。")
        return
    if not confirm(f"将读取 {len(archives)} 个 archive 校验 SHA256，是否继续？", default=False):
        return
    try:
        ok, fail = archive.verify_tape(conn, device, label)
    except Exception:
        logger.exception("verify_tape failed")
        raise
    print()
    if fail:
        print(f"  ✗ {fail} 个 archive 校验失败")
    else:
        print(f"  ✓ 全部 {ok} 个 archive SHA256 一致")
    press_enter()


def do_show_catalog(conn, logger: logging.Logger) -> None:
    """Menu item 5: catalog summary."""
    print("[5] 查看台账")
    print("-" * 60)
    tapes = catalog.list_tapes(conn)
    datasets = catalog.list_datasets(conn)
    archives = catalog.list_archives(conn)
    print(f"  磁带：{len(tapes)}  Dataset：{len(datasets)}  Archive：{len(archives)}")
    print()
    print("  [磁带]")
    if not tapes:
        print("    （无）")
    for t in tapes:
        print(catalog.format_tape_row(t))
    print()
    print("  [Dataset]")
    if not datasets:
        print("    （无）")
    for d in datasets:
        print(catalog.format_dataset_row(d))
    print()
    print("  [Archive]")
    if not archives:
        print("    （无）")
    for a in archives:
        print(catalog.format_archive_row(a))
    press_enter()


def do_export_csv(conn, logger: logging.Logger) -> None:
    """Menu item 6: export SQLite catalog to 5 CSV files (UTF-8-SIG)."""
    print("[6] 导出台账 (CSV)")
    print("-" * 60)
    default_dir = DATA_DIR / "csv_export" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_str = prompt("  输出目录", default=str(default_dir))
    out_dir = Path(out_str).expanduser().resolve()
    if out_dir.exists() and any(out_dir.iterdir()):
        if not confirm(f"  {out_dir} 非空，是否覆盖其中的 CSV？", default=False):
            print("  已取消。")
            return
    try:
        paths = catalog.export_to_csv(conn, out_dir)
    except Exception:
        logger.exception("export_to_csv failed")
        raise
    print()
    print(f"  ✓ 已导出 {len(paths)} 个 CSV 到 {out_dir}：")
    for p in paths:
        size = p.stat().st_size if p.exists() else 0
        print(f"    - {p.name:<20} ({format_bytes(size)})")
    press_enter()


def do_rebuild_catalog(
    conn, device: tape.TapeDevice, logger: logging.Logger
) -> None:
    """Menu item 7: rebuild catalog rows from manifest sidecars on the mounted tape.

    目录库丢失/损坏后的兜底：manifest sidecar 含 datasets/file_list/
    tape_label/archive_size/timestamp，唯独没有 sha256 -- 重建后对每盘磁带
    跑一次「校验磁带」（菜单 4）即可回填。已存在的记录不会被覆盖，逐盘
    磁带重复执行本命令是安全的。
    """
    print("[7] 重建目录库（从磁带 manifest）")
    print("-" * 60)
    print(f"  将扫描 {_ltfs_mount} 下的 *.manifest.json。")
    print("  请确认当前挂载的就是要重建的磁带；多盘磁带请逐盘挂载、逐盘执行。")
    if not confirm("继续？", default=False):
        print("  已取消。")
        return
    try:
        stats = archive.rebuild_catalog_from_manifests(
            conn, device, capacity_bytes=LTO6_CAPACITY
        )
    except Exception:
        logger.exception("rebuild_catalog_from_manifests failed")
        raise
    print()
    print(
        f"  ✓ 重建完成：磁带 {stats['tapes']}、Dataset {stats['datasets']}、"
        f"archive {stats['archives']}（已存在跳过 {stats['existing_archives']}）、"
        f"文件 {stats['files']} 条。"
    )
    if stats["archives"]:
        print("  提示：重建的 archive 尚无 SHA256 记录，请对该磁带执行")
        print("        「4 校验磁带」完成校验并自动回填。")
    press_enter()


# ============================================================================

if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n中断。")
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
