"""交互式配置器：在执行前打印所有参数，允许用户逐项确认或修改。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Optional

from .constants import (
    DEFAULT_CSV_CATALOG,
    DEFAULT_INTEGRITY_PRIORITY,
    DEFAULT_LOG_DIR,
    DEFAULT_LTFS_MOUNT,
    DEFAULT_MIN_TAIL_GB,
    DEFAULT_NEW_TAPE_CAPACITY_GB,
    DEFAULT_SAFETY_MARGIN_GB,
)
from .exceptions import UserAbortedError
from .utils import (
    prompt_bool,
    prompt_float,
    prompt_text,
)

LOG = logging.getLogger("cryotape")


@dataclass
class RuntimeConfig:
    """
    运行时配置：既能从 CLI 解析得到，也能从交互输入得到。
    与 argparse Namespace 解耦，避免硬绑定 argparse 字段名。
    """
    project_dir: Path
    ltfs_mount: Path
    csv_catalog: Path
    safety_margin_gb: float
    new_tape_capacity_gb: float
    min_tail_gb: float
    integrity_priority: bool
    dry_run: bool
    verbose: bool
    non_interactive: bool
    yes: bool
    log_dir: Path
    show_progress: bool

    @classmethod
    def with_defaults(cls, project_dir: Path) -> "RuntimeConfig":
        """用所有默认值填充，仅 project_dir 由用户提供。"""
        return cls(
            project_dir=project_dir,
            ltfs_mount=Path(DEFAULT_LTFS_MOUNT),
            csv_catalog=Path(DEFAULT_CSV_CATALOG),
            safety_margin_gb=DEFAULT_SAFETY_MARGIN_GB,
            new_tape_capacity_gb=DEFAULT_NEW_TAPE_CAPACITY_GB,
            min_tail_gb=DEFAULT_MIN_TAIL_GB,
            integrity_priority=DEFAULT_INTEGRITY_PRIORITY,
            dry_run=False,
            verbose=False,
            non_interactive=False,
            yes=False,
            log_dir=Path(DEFAULT_LOG_DIR),
            show_progress=True,
        )


class InteractiveConfigurator:
    """
    交互式配置流程：
        1. 打印当前所有参数
        2. 用户选择：确认 / 修改 / 取消
        3. 若修改，列出可修改项，让用户选编号并输入新值
        4. 循环直到确认或取消
    """

    # 各字段的展示名与编辑器
    FIELD_EDITORS = {
        "project_dir": ("项目目录", "_edit_path"),
        "ltfs_mount": ("LTFS 挂载点", "_edit_path"),
        "csv_catalog": ("本地 CSV 台账", "_edit_path"),
        "log_dir": ("日志目录", "_edit_path"),
        "safety_margin_gb": ("安全余量 (GB)", "_edit_float"),
        "new_tape_capacity_gb": ("标准空磁带容量 (GB)", "_edit_float"),
        "min_tail_gb": ("跨项目最小剩余 (GB)", "_edit_float"),
        "integrity_priority": ("项目完整度优先", "_edit_bool"),
        "dry_run": ("仅预演 (--dry-run)", "_edit_bool"),
        "verbose": ("实时显示 tar 输出 (--verbose)", "_edit_bool"),
        "non_interactive": ("跳过交互确认", "_edit_bool"),
        "yes": ("默认全部确认 (--yes)", "_edit_bool"),
    }

    # 敏感字段（不应该在交互中随意改）
    PROTECTED = {"project_dir"}

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config

    def run(self) -> RuntimeConfig:
        """主循环：打印 → 询问 → 修改/确认。返回最终配置。"""
        if self.config.non_interactive:
            return self.config

        while True:
            self._print_summary()
            print("\n请选择操作：")
            print("  [Y] 确认配置，开始执行")
            print("  [e] 修改某项参数")
            print("  [n] 取消")
            choice = prompt_text(
                ">",
                default="Y",
                non_interactive=self.config.non_interactive,
            ).strip().lower()
            if choice in ("y", "yes", ""):
                return self.config
            if choice in ("n", "no", "q", "quit"):
                raise UserAbortedError("用户在配置确认阶段取消")
            if choice in ("e", "edit"):
                self._edit_loop()
                continue
            print("无效输入，请输入 Y / e / n。")

    def _print_summary(self) -> None:
        print("\n" + "=" * 60)
        print("📋 CryoTape 当前运行配置")
        print("=" * 60)
        print(f"  项目目录:           {self.config.project_dir}")
        print(f"  LTFS 挂载点:        {self.config.ltfs_mount}")
        print(f"  本地 CSV 台账:      {self.config.csv_catalog}")
        print(f"  安全余量:           {self.config.safety_margin_gb} GB")
        print(f"  标准空磁带容量:     {self.config.new_tape_capacity_gb} GB")
        print(f"  跨项目最小剩余:     {self.config.min_tail_gb} GB")
        print(f"  项目完整度优先:     {self.config.integrity_priority}")
        print(f"  日志目录:           {self.config.log_dir}")
        print(f"  --dry-run:          {self.config.dry_run}")
        print(f"  --verbose:          {self.config.verbose}")
        print(f"  --non-interactive:  {self.config.non_interactive}")
        print(f"  --yes:              {self.config.yes}")
        print("=" * 60)

    def _edit_loop(self) -> None:
        """修改循环：列出可改字段，让用户选编号后输入新值。"""
        editable = [f for f in fields(self.config) if f.name in self.FIELD_EDITORS]
        while True:
            print("\n可修改的参数：")
            for i, f in enumerate(editable, 1):
                label, _ = self.FIELD_EDITORS[f.name]
                current = getattr(self.config, f.name)
                print(f"  [{i}] {label} = {current}")
            print("  [0] 完成修改，返回确认")
            ans = prompt_text(
                "请选择要修改的参数编号",
                default="0",
                non_interactive=self.config.non_interactive,
            ).strip()
            if ans in ("0", "", "q"):
                return
            try:
                idx = int(ans) - 1
                if not (0 <= idx < len(editable)):
                    print("编号超出范围。")
                    continue
            except ValueError:
                print("请输入数字。")
                continue

            field_name = editable[idx].name
            new_value = self._edit_one(field_name)
            if new_value is not None:
                self.config = replace(self.config, **{field_name: new_value})
                print(f"  ✓ 已更新 {self.FIELD_EDITORS[field_name][0]} = {new_value}")

    def _edit_one(self, field_name: str) -> object:
        """调用对应的编辑器。"""
        editor_name = self.FIELD_EDITORS[field_name][1]
        editor = getattr(self, editor_name)
        current = getattr(self.config, field_name)
        return editor(current)

    def _edit_path(self, current: Path) -> Optional[Path]:
        new = prompt_text(
            "新路径",
            default=str(current),
            non_interactive=self.config.non_interactive,
        )
        if not new or new == str(current):
            return None
        return Path(new).expanduser()

    def _edit_float(self, current: float) -> Optional[float]:
        return prompt_float(
            "新值",
            default=current,
            non_interactive=self.config.non_interactive,
        )

    def _edit_bool(self, current: bool) -> Optional[bool]:
        return prompt_bool(
            "新值",
            default=current,
            non_interactive=self.config.non_interactive,
        )