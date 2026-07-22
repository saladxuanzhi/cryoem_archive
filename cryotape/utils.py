"""通用工具函数：大小格式化、交互提示。"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import sys
from typing import Sequence

from .exceptions import UserAbortedError

LOG = logging.getLogger("cryotape")


def format_size(size_bytes: int) -> str:
    """将字节数格式化为人类可读字符串，使用 1024 进制（与 df -h 一致）。"""
    if size_bytes < 0:
        return f"-{format_size(-size_bytes)}"
    if size_bytes == 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    i = 0
    n = float(size_bytes)
    while n >= 1024.0 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.2f} {units[i]}"


def gb_to_bytes(gb: float) -> int:
    """将 GB 转为字节数。CLI 默认使用 1000 进制（存储厂商口径）。"""
    return int(gb * (1000 ** 3))


def setup_logging(verbose: bool) -> None:
    """配置根 logger。verbose=False 时只显示 INFO+。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr,
        force=True,
    )


def now_iso() -> str:
    """返回当前时间戳（YYYY-MM-DD HH:MM:SS）。"""
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_date() -> str:
    """返回日期（YYYY-MM-DD）。"""
    return _dt.datetime.now().strftime("%Y-%m-%d")


def prompt(question: str, *, default_yes: bool, non_interactive: bool) -> bool:
    """
    交互式确认。non_interactive=True 时直接返回 default_yes。
    用户输入 y/yes → True；n/no → False；空回车 → default_yes。
    """
    if non_interactive:
        return default_yes
    suffix = "[Y/n]" if default_yes else "[y/N]"
    while True:
        try:
            ans = input(f"{question} {suffix}: ").strip().lower()
        except EOFError:
            return default_yes
        if not ans:
            return default_yes
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("请输入 y 或 n。")


def prompt_choice(question: str, choices: Sequence[str], *,
                  non_interactive: bool) -> str:
    """
    显示一个选择问题，返回用户输入的选项。non_interactive 时返回 choices[0]。
    """
    if non_interactive:
        return choices[0]
    print(question)
    for i, c in enumerate(choices, 1):
        print(f"  {i}. {c}")
    while True:
        try:
            ans = input(f"请输入编号 (1-{len(choices)}) 或 q 取消: ").strip().lower()
        except EOFError:
            return choices[0]
        if ans == "q":
            raise UserAbortedError("用户取消")
        try:
            idx = int(ans)
            if 1 <= idx <= len(choices):
                return choices[idx - 1]
        except ValueError:
            pass
        print("无效输入。")


def prompt_selection(question: str, candidates: Sequence[str], *,
                     non_interactive: bool) -> list[int]:
    """
    多选提示：返回选中的索引列表。空回车 = 全部。输入 "n" 或 "q" = 取消。
    non_interactive 时返回全部索引。
    """
    if non_interactive:
        return list(range(len(candidates)))
    print(question)
    for i, c in enumerate(candidates, 1):
        print(f"  [{i}] {c}")
    print('  输入 "all" 或直接回车 = 全部；输入编号（逗号或空格分隔）= 选择子集；"n" = 取消')
    while True:
        try:
            ans = input("> ").strip().lower()
        except EOFError:
            return list(range(len(candidates)))
        if ans in ("", "all"):
            return list(range(len(candidates)))
        if ans in ("n", "no", "q", "quit"):
            raise UserAbortedError("用户取消多项目选择")
        # 解析逗号/空格分隔的编号
        tokens = re.split(r"[,\s]+", ans)
        try:
            idxs = [int(t) - 1 for t in tokens if t]
        except ValueError:
            print("包含无效编号，请重试。")
            continue
        if any(i < 0 or i >= len(candidates) for i in idxs):
            print("编号超出范围，请重试。")
            continue
        return idxs


def prompt_text(question: str, *, default: str = "",
                non_interactive: bool) -> str:
    """
    文本输入。空回车 = default；non_interactive 时直接返回 default。
    """
    if non_interactive:
        return default
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{question}{suffix}: ").strip()
    except EOFError:
        return default
    return ans if ans else default


def prompt_bool(question: str, *, default: bool,
                non_interactive: bool) -> bool:
    """布尔输入：y/yes/n/no/空回车。"""
    if non_interactive:
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            ans = input(f"{question} {suffix}: ").strip().lower()
        except EOFError:
            return default
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("请输入 y 或 n。")


def prompt_float(question: str, *, default: float,
                 non_interactive: bool) -> float:
    """浮点数输入。"""
    if non_interactive:
        return default
    try:
        ans = input(f"{question} [{default}]: ").strip()
    except EOFError:
        return default
    if not ans:
        return default
    try:
        return float(ans)
    except ValueError:
        print(f"无效数字，使用默认值 {default}")
        return default