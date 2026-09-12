# -*- coding: utf-8 -*-
"""QHarness 工作区上下文和安全路径解析。"""

from qharness.workspace.context import WorkspaceContext
from qharness.workspace.config import (
    WorkspaceHistoryConfig,
    load_workspace_history_config,
)
from qharness.workspace.history import (
    SqlAlchemyWorkspaceHistoryRepository,
    StoredOperation,
    WorkspaceHistoryRepository,
)
from qharness.workspace.models import ChangeStatus, FileChange, FileMutationResult
from qharness.workspace.mutation import WorkspaceMutationService
from qharness.workspace.path_guard import WorkspacePathGuard
from qharness.workspace.version import (
    DulwichFileVersionStore,
    FileHistoryEntry,
    FileVersionStore,
    WorkspaceCommitResult,
    WorkspaceStatus,
)

__all__ = [
    "ChangeStatus",
    "DulwichFileVersionStore",
    "FileChange",
    "FileHistoryEntry",
    "FileMutationResult",
    "FileVersionStore",
    "SqlAlchemyWorkspaceHistoryRepository",
    "StoredOperation",
    "WorkspaceContext",
    "WorkspaceHistoryConfig",
    "WorkspaceCommitResult",
    "WorkspaceStatus",
    "WorkspaceHistoryRepository",
    "WorkspaceMutationService",
    "WorkspacePathGuard",
    "load_workspace_history_config",
]
