# -*- coding: utf-8 -*-
"""单次 Agent Run 的隔离上下文。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from qharness.sandbox.base import SandboxBackend
from qharness.tools.base import ToolExecutionRequest, ToolExecutionState
from qharness.workspace import WorkspaceContext, WorkspaceMutationService


@dataclass(slots=True)
class RunContext:
    """保存一次 Run 独享的工作区、沙箱、计数和取消状态。

    本对象按 Run 创建，而不是做成进程级单例。因此同一个 Harness 进程可以
    同时持有多个租户的 RunContext，并且不会共享工作区或工具调用计数。
    """

    # 租户稳定标识；只用于关联上下文，工作区授权必须由上层单独完成。
    tenant_id: str

    # 租户内部的工作空间标识，不直接作为文件系统路径使用。
    workspace_id: str

    # 本次 Agent Run 的唯一标识，用于日志、审计和持久化关联。
    run_id: str

    # 已经过上层授权并完成路径规范化的本次 Run 工作区。
    workspace: WorkspaceContext

    # 与本次工作区绑定的沙箱后端；外部进程只能通过该入口执行。
    sandbox: SandboxBackend

    # 可选的统一文件变更服务；启用后写工具自动获得 Diff、历史与回滚能力。
    mutation_service: WorkspaceMutationService | None = None

    # 本次 Run 独享的工具调用计数，不与其他租户或其他 Run 共用。
    tool_state: ToolExecutionState = field(default_factory=ToolExecutionState)

    # 整个 Run 的主动取消信号；工具请求和沙箱进程共享同一个事件。
    cancellation_event: asyncio.Event = field(default_factory=asyncio.Event)

    # 供日志、追踪或上层调度使用的非安全关键扩展信息。
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """验证标识和核心依赖，避免创建无法安全使用的 Run。"""

        _validate_identifier(self.tenant_id, "tenant_id")
        _validate_identifier(self.workspace_id, "workspace_id")
        _validate_identifier(self.run_id, "run_id")
        if not isinstance(self.workspace, WorkspaceContext):
            raise TypeError("workspace 必须是 WorkspaceContext。")
        if not isinstance(self.sandbox, SandboxBackend):
            raise TypeError("sandbox 必须实现 SandboxBackend。")
        if self.mutation_service is not None:
            if not isinstance(self.mutation_service, WorkspaceMutationService):
                raise TypeError(
                    "mutation_service 必须是 WorkspaceMutationService。"
                )
            if self.mutation_service.workspace.root != self.workspace.root:
                raise ValueError("mutation_service 与 RunContext 工作区不一致。")

    @property
    def cancelled(self) -> bool:
        """返回整个 Run 是否已经收到取消信号。"""

        return self.cancellation_event.is_set()

    def cancel(self) -> None:
        """设置 Run 级取消信号，使正在执行和后续工具调用尽快停止。"""

        self.cancellation_event.set()

    def create_tool_request(
        self,
        *,
        call_id: str,
        tool_name: str,
        raw_arguments: str | Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> ToolExecutionRequest:
        """创建自动携带 Run 标识、租户信息和取消事件的工具请求。"""

        _validate_identifier(call_id, "call_id")
        request_metadata = dict(metadata or {})
        # 租户字段由 RunContext 写入，调用方不能用 metadata 覆盖。
        request_metadata.update(
            {
                "tenant_id": self.tenant_id,
                "workspace_id": self.workspace_id,
            }
        )
        return ToolExecutionRequest(
            call_id=call_id,
            tool_name=tool_name,
            raw_arguments=raw_arguments,
            run_id=self.run_id,
            metadata=request_metadata,
            cancellation_event=self.cancellation_event,
            operation_id=operation_id,
        )


def _validate_identifier(value: str, name: str) -> None:
    """验证仅用于关联的外部标识，不允许空值、空字符或异常长度。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串。")
    if "\x00" in value:
        raise ValueError(f"{name} 不能包含空字符。")
    if len(value) > 256:
        raise ValueError(f"{name} 不能超过 256 个字符。")
