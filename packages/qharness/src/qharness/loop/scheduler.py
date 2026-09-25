"""工具批次调度：显式安全的只读段可并行，写入与未知效果形成屏障。"""

import asyncio
from dataclasses import dataclass

from qharness.exception import LoopExecutionError
from qharness.loop.repository import digest
from qharness.loop.tool_service import ToolService
from qharness.model.models import ToolCall
from qharness.tools.base import ToolEffect, ToolExecutionResult


@dataclass(frozen=True)
class ScheduledResult:
    provider_call_id: str
    logical_call_id: str
    result: ToolExecutionResult


def business_failed(result: ToolExecutionResult) -> bool:
    if not result.success:
        return True
    data = result.data
    return isinstance(data, dict) and (data.get("succeeded") is False or
                                      ("exit_code" in data and data["exit_code"] != 0))


class ActionScheduler:
    def __init__(self, tools: ToolService, *, max_parallel_reads: int):
        self.tools = tools
        self.max_parallel_reads = max_parallel_reads

    def _parallel(self, call: ToolCall) -> bool:
        tool = self.tools.executor.registry.get(call.function.name)
        return tool is not None and tool.effect == ToolEffect.READ_ONLY and tool.parallel_safe

    async def execute(self, calls: list[ToolCall], *, model_call_id: str, expected_version: int) -> list[ScheduledResult]:
        results: list[ScheduledResult] = []
        failed = False
        index = 0

        async def run(call):
            logical_id = "tool-" + digest([model_call_id, call.id])
            try:
                result = await self.tools.execute(logical_id, call.function.name, call.function.arguments,
                                                  expected_version=expected_version)
            except LoopExecutionError as error:
                if error.code not in {"budget_exceeded", "unknown", "stale_context"}:
                    raise
                result = ToolExecutionResult(logical_id, call.function.name, False, str(error), 0,
                                             error_code=error.code)
            return ScheduledResult(call.id, logical_id, result)

        while index < len(calls):
            if failed:
                call = calls[index]
                logical_id = "tool-" + digest([model_call_id, call.id])
                results.append(ScheduledResult(call.id, logical_id, ToolExecutionResult(
                    logical_id, call.function.name, False,
                    "前序行动失败或效果未知，无法证明此调用可独立继续；请根据结果重新决定行动", 0, error_code="skipped")))
                index += 1
                continue
            end = index + 1
            if self._parallel(calls[index]):
                while end < len(calls) and end - index < self.max_parallel_reads and self._parallel(calls[end]):
                    end += 1
            # return_exceptions 确保已派发的同段调用全部结束，才允许异常交回。
            group = await asyncio.gather(*(run(call) for call in calls[index:end]), return_exceptions=True)
            for item in group:
                if isinstance(item, BaseException):
                    raise item
            results.extend(group)
            failed = any(business_failed(item.result) for item in group)
            index = end
        return results
