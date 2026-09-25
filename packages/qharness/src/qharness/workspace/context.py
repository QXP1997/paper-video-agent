# -*- coding: utf-8 -*-
"""单次 Agent Run 使用的工作区上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qharness.workspace.path_guard import WorkspacePathGuard


@dataclass(frozen=True, slots=True, init=False)
class WorkspaceContext:
    """保存规范化工作区根目录，并提供统一安全路径入口。"""

    root: Path
    path_guard: WorkspacePathGuard

    def __init__(self, root: str | Path) -> None:
        """根据客户端选择的目录创建工作区上下文。"""

        path_guard = WorkspacePathGuard(root)
        object.__setattr__(self, "root", path_guard.root)
        object.__setattr__(self, "path_guard", path_guard)

    def resolve_path(
        self,
        path: str | Path,
        *,
        must_exist: bool = True,
    ) -> Path:
        """通过路径守卫解析普通工作区路径。"""

        return self.path_guard.resolve_path(path, must_exist=must_exist)

    def resolve_file(self, path: str | Path) -> Path:
        """通过路径守卫解析工作区文件。"""

        return self.path_guard.resolve_file(path)

    def resolve_directory(self, path: str | Path = ".") -> Path:
        """通过路径守卫解析工作区目录。"""

        return self.path_guard.resolve_directory(path)

    def relative_path(
        self,
        path: str | Path,
        *,
        must_exist: bool = True,
    ) -> str:
        """返回目标路径相对于工作区根目录的展示形式。"""

        return self.path_guard.relative_path(path, must_exist=must_exist)
