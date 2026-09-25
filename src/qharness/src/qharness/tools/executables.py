# -*- coding: utf-8 -*-
"""QHarness 使用的第三方可执行文件发现逻辑。"""

from __future__ import annotations

import platform
import shutil
import sys
from pathlib import Path

from qharness.exception import ToolProviderError


_BUNDLED_RIPGREP_DIRECTORIES: dict[tuple[str, str], str] = {
    ("win32", "amd64"): "windows-x64",
    ("win32", "x86_64"): "windows-x64",
}


def resolve_ripgrep_path(explicit_path: str | Path | None = None) -> str | None:
    """按显式路径、内置资源、系统 PATH 的顺序查找 ripgrep。"""

    if explicit_path is not None:
        candidate = Path(explicit_path).expanduser().resolve(strict=False)
        if not candidate.is_file():
            raise ToolProviderError(
                f"显式配置的 ripgrep 可执行文件不存在：{candidate}"
            )
        return str(candidate)

    bundled_path = _bundled_ripgrep_path()
    if bundled_path is not None and bundled_path.is_file():
        return str(bundled_path)

    return shutil.which("rg")


def _bundled_ripgrep_path() -> Path | None:
    """返回当前操作系统和处理器架构对应的内置 ripgrep 路径。"""

    machine = platform.machine().casefold()
    resource_directory = _BUNDLED_RIPGREP_DIRECTORIES.get(
        (sys.platform, machine)
    )
    if resource_directory is None:
        return None

    package_root = Path(__file__).resolve().parents[1]
    return (
        package_root
        / "resources"
        / "ripgrep"
        / resource_directory
        / "rg.exe"
    )
