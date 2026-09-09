# -*- coding: utf-8 -*-
"""示例五：直接运行带 Hook、参数校验和次数限制的工具执行器。"""

from __future__ import annotations

import logging

from pydantic import Field

from _common import TOOL_CONFIG_PATH, run_example
from qharness.tools import (
    Tool,
    ToolExecutionHook,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolExecutionState,
    ToolExecutor,
    ToolParameters,
    ToolRegistry,
    load_tool_policy,
)


_LOGGER = logging.getLogger("qharness.examples.tool_runtime")


class MultiplyParameters(ToolParameters):
    """乘法工具的参数模型。"""

    left: int = Field(description="左侧整数。")
    right: int = Field(description="右侧整数。")


def multiply(left: int, right: int) -> dict[str, int]:
    """返回两个整数的乘积。"""

    return {"result": left * right}


class LoggingToolHook(ToolExecutionHook):
    """使用统一日志记录工具生命周期，演示 Hook 的使用方式。"""

    async def before_execute(
        self,
        request: ToolExecutionRequest,
        tool: Tool,
    ) -> None:
        """记录即将执行的工具和已经通过校验的参数。"""

        _LOGGER.info("[before] %s 参数=%s", tool.name, request.arguments)
        return None

    async def after_execute(
        self,
        request: ToolExecutionRequest,
        tool: Tool,
        result: ToolExecutionResult,
    ) -> None:
        """记录成功结果和耗时。"""

        _LOGGER.info(
            "[after] %s 结果=%s 耗时=%.4fs",
            tool.name,
            result.content,
            result.elapsed_seconds,
        )

    async def on_error(
        self,
        request: ToolExecutionRequest,
        tool: Tool | None,
        result: ToolExecutionResult,
    ) -> None:
        """记录工具执行器生成的标准错误结果。"""

        _LOGGER.error(
            "[error] %s %s",
            request.tool_name,
            result.to_model_content(),
        )


async def main() -> None:
    """依次演示成功调用、参数错误以及次数超过上限。"""

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="multiply",
            description="计算两个整数的乘积。",
            parameters=MultiplyParameters,
            handler=multiply,
        )
    )

    executor = ToolExecutor(
        registry,
        policy=load_tool_policy(TOOL_CONFIG_PATH),
        hooks=[LoggingToolHook()],
    )
    state = ToolExecutionState()

    requests = [
        ToolExecutionRequest(
            call_id="call_001",
            tool_name="multiply",
            raw_arguments='{"left": 123, "right": 456}',
        ),
        ToolExecutionRequest(
            call_id="call_002",
            tool_name="multiply",
            raw_arguments='{"left": 123}',
        ),
        ToolExecutionRequest(
            call_id="call_003",
            tool_name="multiply",
            raw_arguments='{"left": 2, "right": 3}',
        ),
    ]

    for request in requests:
        result = await executor.execute(request, state)
        _LOGGER.info(
            "调用完成：success=%s，error_code=%s",
            result.success,
            result.error_code,
        )


if __name__ == "__main__":
    run_example(main)
