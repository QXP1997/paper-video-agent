# -*- coding: utf-8 -*-
"""根据动态配置创建统一沙箱后端。"""

from __future__ import annotations

from qharness.exception import SandboxConfigurationError
from qharness.runtime import RuntimeManager
from qharness.sandbox.base import SandboxBackend
from qharness.sandbox.config import SandboxConfig
from qharness.sandbox.srt import SrtSandboxBackend
from qharness.workspace import WorkspaceContext


def create_sandbox_backend(
    config: SandboxConfig,
    workspace: WorkspaceContext,
    runtime_manager: RuntimeManager | None = None,
) -> SandboxBackend:
    """创建配置指定的后端，并共享一份可选托管运行时管理器。"""

    if config.backend == "srt":
        manager = runtime_manager or RuntimeManager(
            config.runtime_directory.parent
        )
        return SrtSandboxBackend(config, workspace, manager)
    raise SandboxConfigurationError(
        f"不支持的沙箱后端：{config.backend}"
    )
