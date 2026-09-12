# -*- coding: utf-8 -*-
"""RunContext 的标准创建入口。"""

from __future__ import annotations

from pathlib import Path

from qharness.run.context import RunContext
from qharness.runtime import RuntimeManager
from qharness.sandbox.config import SandboxConfig
from qharness.sandbox.factory import create_sandbox_backend
from qharness.workspace.context import WorkspaceContext


def create_run_context(
    *,
    tenant_id: str,
    workspace_id: str,
    run_id: str,
    workspace_root: str | Path,
    sandbox_config: SandboxConfig,
    runtime_manager: RuntimeManager | None = None,
) -> RunContext:
    """为一次 Run 创建独立工作区和沙箱，并可复用进程级运行时管理器。

    ``workspace_root`` 必须由客户端或服务端在完成租户授权后传入。本函数只
    建立运行边界，不根据 tenant_id 拼接或推断目录，避免标识被当成路径。
    """

    workspace = WorkspaceContext(workspace_root)
    sandbox = create_sandbox_backend(
        sandbox_config,
        workspace,
        runtime_manager,
    )
    return RunContext(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        run_id=run_id,
        workspace=workspace,
        sandbox=sandbox,
    )
