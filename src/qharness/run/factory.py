# -*- coding: utf-8 -*-
"""RunContext 的标准创建入口。"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qharness.backends.base import ModelBackend
    from qharness.loop.config import LoopConfig
    from qharness.loop.models import RunState, TaskContract
    from qharness.loop.model_service import ModelService
    from qharness.loop.repository import LoopRepository
    from qharness.loop.tool_service import ToolService
    from qharness.loop.actor import Actor
    from qharness.verification import VerificationController
    from qharness.tools.executor import ToolExecutor

from qharness.persistence import DatabaseManager
from qharness.run.context import RunContext
from qharness.runtime import RuntimeManager
from qharness.sandbox.config import SandboxConfig
from qharness.sandbox.factory import create_sandbox_backend
from qharness.workspace import (
    DulwichFileVersionStore,
    FileVersionStore,
    SqlAlchemyWorkspaceHistoryRepository,
    WorkspaceContext,
    WorkspaceHistoryConfig,
    WorkspaceHistoryRepository,
    WorkspaceMutationService,
)


@dataclass(frozen=True)
class LoopServices:
    """已有 RunContext 上装配的调用边界；Backend 生命周期仍由调用方管理。"""
    repository: LoopRepository
    model: ModelService
    tools: ToolService
    actor: Actor
    verifier: VerificationController


def create_loop_services(
    context: RunContext, *, backend: ModelBackend, executor: ToolExecutor,
    database_manager: DatabaseManager, config: LoopConfig, contract: TaskContract,
    state: RunState | None = None,
) -> LoopServices:
    """复用应用数据库和已有执行依赖；重复装配不重置状态、策略或预算。"""
    from qharness.loop.context import ContextCompiler
    from qharness.loop.model_service import ModelService
    from qharness.loop.repository import LoopRepository
    from qharness.loop.tool_service import ToolService
    from qharness.loop.actor import Actor
    from qharness.verification import CheckRunner, VerificationController

    database_manager.initialize()
    repository = LoopRepository(database_manager.session_factory, tenant_id=context.tenant_id,
                                workspace_id=context.workspace_id, run_id=context.run_id)
    repository.create(contract, config, executor.policy, state=state)
    model = ModelService(backend, repository, ContextCompiler(config))
    tools = ToolService(executor, context, repository)
    return LoopServices(repository, model, tools, Actor(model, tools), VerificationController(CheckRunner(tools), model))


def create_run_context(
    *,
    tenant_id: str,
    workspace_id: str,
    run_id: str,
    workspace_root: str | Path,
    sandbox_config: SandboxConfig,
    runtime_manager: RuntimeManager | None = None,
    database_manager: DatabaseManager | None = None,
    history_config: WorkspaceHistoryConfig | None = None,
    history_repository: WorkspaceHistoryRepository | None = None,
    version_store: FileVersionStore | None = None,
) -> RunContext:
    """为一次 Run 创建独立工作区和沙箱，并可复用进程级运行时管理器。

    ``workspace_root`` 必须由客户端或服务端在完成租户授权后传入。本函数只
    建立运行边界，不根据 tenant_id 拼接或推断目录，避免标识被当成路径。
    传入 ``history_config`` 时还会创建文件变更服务。应用级数据库由
    ``database_manager`` 统一管理，Dulwich 存储目录由历史配置提供。
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
    if history_config is not None and history_repository is not None:
        raise ValueError("history_config 与自定义历史存储不能同时提供。")
    if history_config is not None and database_manager is None:
        raise ValueError("启用工作区历史时必须提供 database_manager。")

    resolved_repository = history_repository
    resolved_version_store = version_store
    if history_config is not None:
        assert database_manager is not None
        database_manager.initialize()
        resolved_repository = SqlAlchemyWorkspaceHistoryRepository(
            database_manager.session_factory,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            local_root=database_manager.local_root,
        )
        resolved_version_store = DulwichFileVersionStore(
            history_config.storage_root,
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
