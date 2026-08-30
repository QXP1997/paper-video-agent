# -*- coding: utf-8 -*-
"""示例五：直接运行带 Hook、参数校验和次数限制的工具执行器。"""

from __future__ import annotations

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


class MultiplyParameters(ToolParameters):
    """乘法工具的参数模型。"""

    left: int = Field(description="左侧整数。")
    right: int = Field(description="右侧整数。")


def multiply(left: int, right: int) -> dict[str, int]:
    """返回两个整数的乘积。"""

    return {"result": left * right}


class PrintToolHook(ToolExecutionHook):
    """将工具生命周期输出到终端，演示 Hook 的使用方式。"""

    async def before_execute(
        self,
        request: ToolExecutionRequest,
        tool: Tool,
    ) -> None:
        """打印即将执行的工具和已经通过校验的参数。"""

        print(f"[before] {tool.name} 参数={request.arguments}")
        return None

    async def after_execute(
        self,
        request: ToolExecutionRequest,
        tool: Tool,
        result: ToolExecutionResult,
    ) -> None:
        """打印成功结果和耗时。"""

        print(
            f"[after] {tool.name} 结果={result.content} "
            f"耗时={result.elapsed_seconds:.4f}s"
        )

    async def on_error(
        self,
        request: ToolExecutionRequest,
        tool: Tool | None,
        result: ToolExecutionResult,
    ) -> None:
        """打印工具执行器生成的标准错误结果。"""

        print(f"[error] {request.tool_name} {result.to_model_content()}")


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
        hooks=[PrintToolHook()],
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
        print(
            f"调用完成：success={result.success}, "
            f"error_code={result.error_code}\n"
        )


if __name__ == "__main__":
    run_example(main)
