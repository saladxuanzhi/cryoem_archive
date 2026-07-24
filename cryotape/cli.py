"""CLI 入口：argparse 装配 + main() orchestration。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from .constants import (
    DEFAULT_CSV_CATALOG,
    DEFAULT_INTEGRITY_PRIORITY,
    DEFAULT_LOG_DIR,
    DEFAULT_LTFS_MOUNT,
    DEFAULT_MIN_TAIL_GB,
    DEFAULT_NEW_TAPE_CAPACITY_GB,
    DEFAULT_SAFETY_MARGIN_GB,
)
from .interactive import InteractiveConfigurator, RuntimeConfig
from .utils import setup_logging
from .workflow import Workflow

__version__ = "2.2.0"


def build_arg_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器。支持位置参数（project_dir）。"""
    p = argparse.ArgumentParser(
        prog="cryotape",
        description="CryoTape — 冷冻电镜数据归档到 LTO-6 LTFS 磁带",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # 位置参数（与 --project-dir 等价，但更便捷）
    p.add_argument("project_dir_pos", nargs="?",
                   help="项目目录路径（也可使用 --project-dir）")
    p.add_argument("--project-dir",
                   help="待归档的项目目录（单项目根或多项目父目录）")
    p.add_argument("--ltfs-mount", default=DEFAULT_LTFS_MOUNT,
                   help="LTFS 磁带挂载点")
    p.add_argument("--csv-catalog", default=DEFAULT_CSV_CATALOG,
                   help="本地总 CSV 台账路径")
    p.add_argument("--safety-margin", type=float,
                   default=DEFAULT_SAFETY_MARGIN_GB,
                   help="磁带安全预留空间（GB）")
    p.add_argument("--new-tape-capacity", type=float,
                   default=DEFAULT_NEW_TAPE_CAPACITY_GB,
                   help="标准空磁带可用容量（GB）")
    p.add_argument("--min-tail-gb", type=float,
                   default=DEFAULT_MIN_TAIL_GB,
                   help="跨项目共享磁带空间时的最小剩余阈值（GB）"
                        "——剩余超过此值且下一项目能放下时不开新磁带")
    p.add_argument("--prefer-project-integrity", action="store_true",
                   default=DEFAULT_INTEGRITY_PRIORITY,
                   help="项目完整度优先：一个项目只放在一盘磁带上，"
                        "整体放不下就开新磁带（默认关闭：节省空间优先，"
                        "放不下时自动拆分 project 跨多盘）")
    p.add_argument("--dry-run", action="store_true",
                   help="预演模式：仅计算与打印摘要，不写入磁带")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="实时显示 tar 输出（默认仅写日志）")
    p.add_argument("--non-interactive", action="store_true",
                   help="跳过所有交互确认")
    p.add_argument("-y", "--yes", action="store_true",
                   help="所有提示默认确认")
    p.add_argument("--log-dir", default=DEFAULT_LOG_DIR,
                   help="per-tape tar 日志目录")
    p.add_argument("--no-review", action="store_true",
                   help="跳过执行前的参数确认环节（与 --non-interactive 不同，"
                        "本选项仅跳过开头的 review，不影响后续磁带确认）")
    p.add_argument("--no-progress", dest="show_progress",
                   action="store_false", default=True,
                   help="禁用 tar 写入进度条（默认开启；非 TTY 时自动禁用）")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    return p


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # 位置参数与 --project-dir 二选一，位置参数优先
    if args.project_dir_pos and args.project_dir:
        parser.error("--project-dir 与位置参数不能同时使用")
    if args.project_dir_pos:
        args.project_dir = args.project_dir_pos
    elif not args.project_dir:
        # 默认进入交互式引导（保留一些常见默认值）
        parser.error("必须提供项目目录路径（位置参数 或 --project-dir）")

    return args


def args_to_config(args: argparse.Namespace) -> RuntimeConfig:
    """把 argparse Namespace 转成 RuntimeConfig。"""
    return RuntimeConfig(
        project_dir=Path(args.project_dir).expanduser().resolve(),
        ltfs_mount=Path(args.ltfs_mount).expanduser(),
        csv_catalog=Path(args.csv_catalog).expanduser(),
        safety_margin_gb=float(args.safety_margin),
        new_tape_capacity_gb=float(args.new_tape_capacity),
        min_tail_gb=float(args.min_tail_gb),
        integrity_priority=bool(args.prefer_project_integrity),
        dry_run=bool(args.dry_run),
        verbose=bool(args.verbose),
        non_interactive=bool(args.non_interactive),
        yes=bool(args.yes),
        log_dir=Path(args.log_dir).expanduser(),
        show_progress=bool(args.show_progress),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI 入口。返回进程退出码。"""
    args = parse_args(argv)
    config = args_to_config(args)
    setup_logging(config.verbose)

    # 参数确认 / 修改（除非 --non-interactive 或 --no-review）
    if not config.non_interactive and not getattr(args, "no_review", False):
        configurator = InteractiveConfigurator(config)
        config = configurator.run()

    workflow = Workflow(config)
    return workflow.run()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())