# -*- coding: utf-8 -*-
"""通过当前 Run 的 SRT 沙箱执行完整 Shell 命令。"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import Field

from qharness.sandbox.base import SandboxExecutionRequest
from qharness.tools.base import Tool, ToolParameters

if TYPE_CHECKING:
    from qharness.run import RunContext
    from qharness.workspace import ExternalMutationToken, FileMutationResult


class RunCommandParameters(ToolParameters):
    """沙箱命令工具的模型可见参数。"""

    command: str = Field(
        min_length=1,
        description=(
            "要在沙箱 Shell 中执行的完整命令，可以使用管道、重定向、"
            "环境变量、条件和多条命令。"
        ),
    )
    cwd: str = Field(
        default=".",
        min_length=1,
        description="工作目录，必须是当前工作区内的相对路径。",
    )
    stdin: str | None = Field(
        default=None,
        description="可选的 UTF-8 标准输入文本。",
    )


def create_run_command_tool(context: RunContext) -> Tool:
    """创建绑定当前 RunContext 的完整命令执行工具。"""

    async def run_command(
        command: str,
        cwd: str = ".",
        stdin: str | None = None,
    ) -> dict[str, Any]:
        """执行命令，并把命令造成的工作区变化记录为独立 Commit。"""

        mutation_service = context.mutation_service
        token: ExternalMutationToken | None = None
        if mutation_service is not None:
            token = await asyncio.to_thread(
                mutation_service.begin_external_operation,
                "run_command",
            )

        result = None
        mutation_result: FileMutationResult | None = None
        try:
            result = await context.sandbox.execute(
                SandboxExecutionRequest(
                    command=command,
                    cwd=cwd,
                    stdin=stdin,
                    run_id=context.run_id,
                    operation_id=(
                        token.operation_id
                        if token is not None
                        else f"command-{uuid.uuid4().hex}"
                    ),
                    cancellation_event=context.cancellation_event,
                )
            )
        finally:
            if mutation_service is not None and token is not None:
                mutation_result = await asyncio.shield(
                    asyncio.to_thread(
                        mutation_service.finish_external_operation,
                        token,
                        command_succeeded=(
                            result.succeeded if result is not None else False
                        ),
                    )
                )

        if result is None:
            raise RuntimeError("沙箱执行没有返回结果。")
        return {
            "succeeded": result.succeeded,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration_seconds": result.duration_seconds,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "run_id": result.run_id,
            "operation_id": result.operation_id,
            "workspace_change": (
                mutation_result.to_dict() if mutation_result is not None else None
            ),
        }

    return Tool(
        name="run_command",
        description=(
            "在当前工作区的 SRT 沙箱 Shell 中执行完整命令，支持管道、重定向、"
            "变量和条件语法。命令产生的可跟踪文件变化会自动建立私有 Commit。"
        ),
        parameters=RunCommandParameters,
        handler=run_command,
    )
