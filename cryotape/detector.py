"""项目目录判定：单项目 vs 多项目父目录。"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch  # noqa: F401  (typing for monkeypatch users)

from .constants import CRYO_EXTS
from .exceptions import ProjectDetectionError, UserAbortedError
from .utils import format_size, prompt_selection

LOG = logging.getLogger("cryotape")


class ProjectDetector:
    """
    项目目录判定逻辑：
        - 根下直接含 Cryo-EM 图像 → 单项目，直接开始
        - 否则遍历子目录，每个含数据的子目录当作候选，列出目录树让用户选择
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _has_images_in_dir(self, d: Path) -> bool:
        """判断目录 d 是否含 Cryo-EM 图像文件（任意层级）。"""
        try:
            for p in d.rglob("*"):
                if p.is_file() and p.suffix.lower() in CRYO_EXTS:
                    return True
        except OSError:
            return False
        return False

    def _images_directly_in_root(self) -> bool:
        """根下直接（不递归）含图像。"""
        try:
            for p in self.root.iterdir():
                if p.is_file() and p.suffix.lower() in CRYO_EXTS:
                    return True
        except OSError:
            return False
        return False

    def _candidate_subdirs(self) -> list[Path]:
        """根的直接子目录中含数据文件者。"""
        out: list[Path] = []
        for d in sorted(self.root.iterdir(), key=lambda x: x.name):
            if d.is_dir() and not d.name.startswith(".") and self._has_images_in_dir(d):
                out.append(d)
        return out

    def detect(self, *, non_interactive: bool, assume_yes: bool) -> list[Path]:
        """
        返回需要归档的项目根列表。
        单项目时返回 [self.root]；多项目时返回用户选中的子目录列表。
        """
        LOG.info("正在扫描项目目录: %s", self.root)
        if self._images_directly_in_root():
            LOG.info("检测到根目录下直接包含 Cryo-EM 图像 → 单项目模式，直接开始")
            return [self.root]

        candidates = self._candidate_subdirs()
        if not candidates:
            raise ProjectDetectionError(
                f"目录 {self.root} 既不含图像文件，也没有含图像数据的子目录。"
            )
        LOG.info("检测到多个候选项目（共 %d 个）:", len(candidates))
        self._print_tree(candidates)
        if assume_yes:
            LOG.info("--yes 已设置，归档全部 %d 个候选项目", len(candidates))
            return candidates
        idxs = prompt_selection(
            "请选择要归档的项目（默认全部）：",
            [c.name for c in candidates],
            non_interactive=non_interactive,
        )
        return [candidates[i] for i in idxs]

    def _print_tree(self, candidates: list[Path]) -> None:
        """打印 ASCII 目录树（仅展示前两层，避免输出爆炸）。"""
        for c in candidates:
            file_count = sum(1 for _ in c.rglob("*") if _.is_file())
            sub_count = sum(1 for _ in c.iterdir() if _.is_dir())
            total_size = sum(p.stat().st_size for p in c.rglob("*") if p.is_file())
            print(f"  📁 {c.name}/")
            print(f"     文件数: {file_count}  子目录数: {sub_count}  "
                  f"总体积: {format_size(total_size)}")
            # 展示子目录
            shown = 0
            for sub in sorted(c.iterdir()):
                if sub.is_dir() and not sub.name.startswith("."):
                    print(f"     ├─ {sub.name}/")
                    shown += 1
                    if shown >= 5:
                        print(f"     └─ ... (更多子目录省略)")
                        break