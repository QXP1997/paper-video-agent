# -*- coding: utf-8 -*-
"""通过当前 Run 的沙箱执行一个结构化 argv 进程。"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from pydantic import Field

from qharness.sandbox.base import SandboxExecutionRequest
from qharness.tools.base import Tool, ToolParameters

if TYPE_CHECKING:
    from qharness.run import RunContext


class RunProcessParameters(ToolParameters):
    """沙箱进程工具的模型可见参数。"""

    executable: str = Field(
        min_length=1,
        description=(
            "要执行的程序名称或绝对路径，例如 python、node、git 或 java。"
        ),
    )
    arguments: tuple[str, ...] = Field(
        default=(),
        description=(
            "直接传给程序的参数数组；每项都是独立 argv，不要拼接 Shell 命令。"
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


def create_run_process_tool(context: RunContext) -> Tool:
    """创建绑定到指定 RunContext 的沙箱进程工具。

    本工具不保存调用次数、并发数或工具超时。这些执行策略全部由外层
    ToolExecutor 根据工具名称应用；SandboxBackend 只保留不可绕过的进程
    超时、输出和工作区安全边界。
    """

    async def run_process(
        executable: str,
        arguments: tuple[str, ...] = (),
        cwd: str = ".",
        stdin: str | None = None,
    ) -> dict[str, Any]:
        """将结构化工具参数转换成统一沙箱请求并返回执行结果。"""

        result = await context.sandbox.execute(
            SandboxExecutionRequest(
                executable=executable,
                arguments=arguments,
                cwd=cwd,
                stdin=stdin,
                run_id=context.run_id,
                operation_id=f"process-{uuid.uuid4().hex}",
                cancellation_event=context.cancellation_event,
            )
        )
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
        }

    return Tool(
        name="run_process",
        description=(
            "在当前工作区的安全沙箱中执行一个程序。请分别提供 executable 和 "
            "arguments，不要使用管道、重定向、&& 等 Shell 拼接语法。"
        ),
        parameters=RunProcessParameters,
        handler=run_process,
    )
