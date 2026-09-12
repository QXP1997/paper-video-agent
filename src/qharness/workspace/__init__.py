# -*- coding: utf-8 -*-
"""QHarness 工作区上下文和安全路径解析。"""

from qharness.workspace.context import WorkspaceContext
from qharness.workspace.history import (
    SqliteWorkspaceHistoryRepository,
    StoredOperation,
    WorkspaceHistoryRepository,
)
from qharness.workspace.models import ChangeStatus, FileChange, FileMutationResult
from qharness.workspace.mutation import WorkspaceMutationService
from qharness.workspace.path_guard import WorkspacePathGuard
from qharness.workspace.version import DulwichFileVersionStore, FileVersionStore

__all__ = [
    "ChangeStatus",
    "DulwichFileVersionStore",
    "FileChange",
    "FileMutationResult",
    "FileVersionStore",
    "SqliteWorkspaceHistoryRepository",
    "StoredOperation",
    "WorkspaceContext",
    "WorkspaceHistoryRepository",
    "WorkspaceMutationService",
    "WorkspacePathGuard",
]
