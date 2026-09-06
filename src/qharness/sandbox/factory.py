# -*- coding: utf-8 -*-
"""根据动态配置创建统一沙箱后端。"""

from __future__ import annotations

from qharness.exception import SandboxConfigurationError
from qharness.sandbox.base import SandboxBackend
from qharness.sandbox.config import SandboxConfig
from qharness.sandbox.srt import SrtSandboxBackend
from qharness.workspace import WorkspaceContext


def create_sandbox_backend(
    config: SandboxConfig,
    workspace: WorkspaceContext,
) -> SandboxBackend:
    """创建配置指定的后端，调用层不需要感知操作系统差异。"""

    if config.backend == "srt":
        return SrtSandboxBackend(config, workspace)
    raise SandboxConfigurationError(
        f"不支持的沙箱后端：{config.backend}"
    )
