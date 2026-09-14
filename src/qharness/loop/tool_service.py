"""持久工具准入与结果读取；实际执行继续交给已有 ToolExecutor。"""

import asyncio
from dataclasses import asdict, replace
from typing import Any

from qharness.exception import LoopConfigurationError, LoopExecutionError
from qharness.loop.models import Phase
from qharness.loop.repository import LoopRepository, digest
from qharness.run.context import RunContext
from qharness.tools.base import ToolExecutionResult
from qharness.tools.executor import ToolExecutor


class ToolService:
    def __init__(self, executor: ToolExecutor, context: RunContext, repository: LoopRepository):
        if (context.tenant_id, context.workspace_id, context.run_id) != (
            repository.tenant_id, repository.workspace_id, repository.run_id
        ):
            raise LoopConfigurationError("工具运行上下文与账本身份不一致")
        if repository.snapshot()["policy"]["tools"] != asdict(executor.policy):
            raise LoopConfigurationError("工具配置与 Run 的策略快照不一致")
        self.executor, self.context, self.repository = executor, context, repository
        self._refresh_counts()

    def _refresh_counts(self):
        counts = self.repository.tool_counts()
        if sum(counts.values()) >= self.context.tool_state.total_calls:
            self.context.tool_state.calls_by_tool = counts
            self.context.tool_state.total_calls = sum(counts.values())

    async def execute(self, call_id: str, tool_name: str, arguments: Any, *, retry: bool = False,
                      expected_version: int | None = None) -> ToolExecutionResult:
        """call_id 是 Run 内稳定的逻辑调用身份；retry 只允许已证明未进 Handler 的失败。"""
        return await self._execute(call_id, tool_name, arguments, retry=retry,
                                   expected_version=expected_version)

    async def execute_check(self, call_id: str, arguments: Any, *, check_binding: dict,
                            expected_version: int) -> ToolExecutionResult:
        """仅供受信任 CheckRunner 使用，不加入模型可调用工具列表。"""
        return await self._execute(call_id, "run_command", arguments, expected_version=expected_version,
                                   check_binding=check_binding)

    async def _execute(self, call_id: str, tool_name: str, arguments: Any, *, retry: bool = False,
                       expected_version: int | None = None, check_binding: dict | None = None):
        if self.context.cancelled:
            return ToolExecutionResult(call_id, tool_name, False, "运行已取消", 0, error_code="cancelled")
        snapshot = await asyncio.to_thread(self.repository.snapshot)
        state = snapshot["state"]
        if expected_version is not None and snapshot["version"] != expected_version:
            raise LoopExecutionError("阶段状态已改变，拒绝旧工具批次", code="stale_context")
        allowed = (Phase.VERIFYING, Phase.VERIFYING_TASK) if check_binding is not None else (Phase.ACTING,)
        if state is None or state.phase not in allowed:
            raise LoopConfigurationError("工具调用与当前执行/验证阶段不匹配")
        tool = self.executor.registry.get(tool_name)
        binding = {"run_version": snapshot["version"], "stage_attempt": state.active_attempt_id,
                   "tool_definition": digest(asdict(tool.to_definition())) if tool else None}
        if check_binding is not None:
            binding["verification"] = check_binding
        if tool is not None:
            binding["effect"] = str(tool.effect)
            binding["parallel_safe"] = tool.parallel_safe
        await asyncio.to_thread(self.repository.admit_tool, call_id, tool_name, arguments, binding, self.executor.policy,
                                requires_approval=tool.requires_approval if tool else False)
        await asyncio.to_thread(self._refresh_counts)
        claim = await asyncio.to_thread(self.repository.claim_tool, call_id, retry=retry)
        if claim["status"] == "done":
            return await asyncio.to_thread(self.repository.tool_result, call_id)
        if claim["status"] == "unknown":
            return ToolExecutionResult(call_id, tool_name, False, "已派发调用尚无确定结果，禁止自动重放", 0,
                                       error_code="unknown", data={"operation_id": claim["operation_id"]})
        request = self.context.create_tool_request(call_id=call_id, tool_name=tool_name,
            raw_arguments=arguments, operation_id=claim["operation_id"], metadata={"stage_attempt": state.active_attempt_id})
        result = await self.executor.execute_admitted(request)
        if result.error_code in {"timeout", "cancelled"}:
            # 同步 Handler 可能仍在线程中运行，不能把外层超时写成已确定的失败。
            return replace(result, error_code="unknown", content="工具中止后效果尚未确定，需恢复核对")
        return await asyncio.to_thread(self.repository.finish_tool, result, claim["execution"],
            max_result_chars=self.executor.policy.for_tool(tool_name).max_result_chars)

    def read_result(self, call_id: str) -> ToolExecutionResult:
        """不重新准入、不执行工具；已推进到其他阶段也可读取旧结果。"""
        return self.repository.tool_result(call_id)
