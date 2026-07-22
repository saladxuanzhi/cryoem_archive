"""启动期路径与系统依赖校验。"""
from __future__ import annotations

import shutil
from pathlib import Path

from .exceptions import (
    InvalidPathError,
    TapeNotMountedError,
    TarNotFoundError,
)


class PathValidator:
    """在执行任何 IO 之前校验路径与系统依赖。"""

    @staticmethod
    def check_tar_available() -> str:
        """确认 tar 可执行文件存在；返回绝对路径。"""
        path = shutil.which("tar")
        if path is None:
            raise TarNotFoundError(
                "系统中找不到 tar 可执行文件。请安装 GNU tar "
                "(Windows 推荐 Git-Bash / WSL)。"
            )
        return path

    @staticmethod
    def check_project_dir(path: Path) -> None:
        if not path.exists():
            raise InvalidPathError(f"项目目录不存在: {path}")
        if not path.is_dir():
            raise InvalidPathError(f"项目路径不是目录: {path}")

    @staticmethod
    def check_ltfs_mount(path: Path) -> None:
        if not path.exists():
            raise TapeNotMountedError(
                f"LTFS 挂载点不存在: {path}（是否未挂载？）"
            )
        if not path.is_dir():
            raise TapeNotMountedError(f"LTFS 挂载点不是目录: {path}")
        # 可写性测试
        try:
            test_file = path / ".cryotape_write_test"
            test_file.touch()
            test_file.unlink()
        except OSError as exc:
            raise TapeNotMountedError(
                f"LTFS 挂载点不可写: {path}（{exc}）"
            ) from exc

    @staticmethod
    def check_csv_catalog(path: Path) -> None:
        # CSV 不要求预先存在，允许创建
        parent = path.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise InvalidPathError(
                    f"无法创建 CSV 父目录 {parent}: {exc}"
                ) from exc
        if path.exists() and not path.is_file():
            raise InvalidPathError(f"CSV 路径存在但不是文件: {path}")