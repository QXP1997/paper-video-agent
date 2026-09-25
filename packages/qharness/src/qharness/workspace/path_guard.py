# -*- coding: utf-8 -*-
"""工作区路径解析与越界保护。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from qharness.exception import (
    WorkspaceConfigurationError,
    WorkspacePathError,
)


@dataclass(frozen=True, slots=True, init=False)
class WorkspacePathGuard:
    """确保所有文件系统路径都位于指定工作区根目录内。"""

    root: Path

    def __init__(self, root: str | Path) -> None:
        """解析并验证工作区根目录。"""

        try:
            resolved_root = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise WorkspaceConfigurationError(
                f"工作区根目录无法访问：{root}，原因：{error}"
            ) from error

        if not resolved_root.is_dir():
            raise WorkspaceConfigurationError(
                f"工作区根路径不是目录：{resolved_root}"
            )

        object.__setattr__(self, "root", resolved_root)

    def resolve_path(
        self,
        path: str | Path,
        *,
        must_exist: bool = True,
    ) -> Path:
        """解析工作区路径，并拒绝任何逃逸到根目录之外的结果。"""

        candidate = self._to_candidate(path)
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise WorkspacePathError(
                f"工作区路径无法解析：{path}，原因：{error}"
            ) from error

        # resolve(strict=False) 会解析当前已存在的符号链接，因此既能阻止
        # 普通的 ../ 越界，也能阻止通过工作区内符号链接逃逸。
        if not resolved.is_relative_to(self.root):
            raise WorkspacePathError(
                f"路径超出工作区范围：{path}；工作区根目录：{self.root}"
            )

        if must_exist and not resolved.exists():
            raise WorkspacePathError(f"工作区路径不存在：{path}")
        return resolved

    def resolve_file(self, path: str | Path) -> Path:
        """解析必须存在的普通文件路径。"""

        resolved = self.resolve_path(path, must_exist=True)
        if not resolved.is_file():
            raise WorkspacePathError(f"工作区路径不是文件：{path}")
        return resolved

    def resolve_directory(self, path: str | Path = ".") -> Path:
        """解析必须存在的目录路径。"""

        resolved = self.resolve_path(path, must_exist=True)
        if not resolved.is_dir():
            raise WorkspacePathError(f"工作区路径不是目录：{path}")
        return resolved

    def relative_path(
        self,
        path: str | Path,
        *,
        must_exist: bool = True,
    ) -> str:
        """返回统一使用正斜杠的工作区相对路径。"""

        resolved = self.resolve_path(path, must_exist=must_exist)
        relative = resolved.relative_to(self.root)
        return relative.as_posix() if relative.parts else "."

    def contains(self, path: str | Path, *, must_exist: bool = False) -> bool:
        """判断路径是否位于工作区内，不向调用方抛出路径异常。"""

        try:
            self.resolve_path(path, must_exist=must_exist)
        except WorkspacePathError:
            return False
        return True

    def _to_candidate(self, path: str | Path) -> Path:
        """将用户输入转换为候选绝对路径，并过滤 Windows 特殊路径。"""

        if not isinstance(path, (str, Path)):
            raise WorkspacePathError("工作区路径必须是字符串或 pathlib.Path。")
        if isinstance(path, str) and "\x00" in path:
            raise WorkspacePathError("工作区路径不能包含空字符。")

        raw_path = Path(path).expanduser()
        if os.name == "nt":
            self._validate_windows_parts(raw_path)
        return raw_path if raw_path.is_absolute() else self.root / raw_path

    @staticmethod
    def _validate_windows_parts(path: Path) -> None:
        """拒绝 Windows 设备名、数据流以及尾随点或空格。"""

        parts = path.parts[1:] if path.drive else path.parts
        for part in parts:
            if ":" in part:
                raise WorkspacePathError(
                    f"Windows 工作区路径不能包含备用数据流：{path}"
                )
            if part.endswith((".", " ")):
                raise WorkspacePathError(
                    f"Windows 工作区路径组件不能以点或空格结尾：{path}"
                )
            if PureWindowsPath(part).is_reserved():
                raise WorkspacePathError(
                    f"Windows 工作区路径不能使用系统保留名称：{path}"
                )
