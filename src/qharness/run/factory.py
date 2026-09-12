# -*- coding: utf-8 -*-
"""RunContext 的标准创建入口。"""

from __future__ import annotations

from pathlib import Path

from qharness.run.context import RunContext
from qharness.runtime import RuntimeManager
from qharness.sandbox.config import SandboxConfig
from qharness.sandbox.factory import create_sandbox_backend
from qharness.workspace import (
    DulwichFileVersionStore,
    FileVersionStore,
    SqliteWorkspaceHistoryRepository,
    WorkspaceContext,
    WorkspaceHistoryRepository,
    WorkspaceMutationService,
)


def create_run_context(
    *,
    tenant_id: str,
    workspace_id: str,
    run_id: str,
    workspace_root: str | Path,
    sandbox_config: SandboxConfig,
    runtime_manager: RuntimeManager | None = None,
    history_root: str | Path | None = None,
    history_repository: WorkspaceHistoryRepository | None = None,
    version_store: FileVersionStore | None = None,
) -> RunContext:
    """为一次 Run 创建独立工作区和沙箱，并可复用进程级运行时管理器。

    ``workspace_root`` 必须由客户端或服务端在完成租户授权后传入。本函数只
    建立运行边界，不根据 tenant_id 拼接或推断目录，避免标识被当成路径。
    传入 ``history_root`` 时还会创建文件变更服务；私有历史目录必须位于
    Agent 可访问的工作区之外。服务端也可以注入其他元数据与版本存储实现。
    """

    workspace = WorkspaceContext(workspace_root)
    sandbox = create_sandbox_backend(
        sandbox_config,
        workspace,
        runtime_manager,
    )
    if (history_repository is None) != (version_store is None):
        raise ValueError(
            "history_repository 和 version_store 必须同时提供或同时省略。"
        )
    if history_root is not None and history_repository is not None:
        raise ValueError("history_root 与自定义历史存储不能同时提供。")

    resolved_repository = history_repository
    resolved_version_store = version_store
    if history_root is not None:
        resolved_repository = SqliteWorkspaceHistoryRepository(
            history_root,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        resolved_version_store = DulwichFileVersionStore(
            history_root,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )

    mutation_service: WorkspaceMutationService | None = None
    if resolved_repository is not None and resolved_version_store is not None:
        mutation_service = WorkspaceMutationService(
            workspace,
            resolved_repository,
            resolved_version_store,
            run_id=run_id,
        )
    return RunContext(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        run_id=run_id,
        workspace=workspace,
        sandbox=sandbox,
        mutation_service=mutation_service,
    )
