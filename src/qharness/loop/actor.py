"""给定 StagePlan 的行动循环；只提交 StageOutcome，不推进 Todo 或 Task 完成。

调用身份由阶段尝试与轮次确定。再次进入同一尝试时从首轮重建上下文，
通过已有账本读取模型与工具结果；未知的已派发调用不会重放。
"""

import asyncio
import json
from dataclasses import replace

from qharness.exception import LoopConfigurationError, LoopExecutionError, ModelBackendError
from qharness.loop.config import Role
from qharness.loop.context import validate_messages
from qharness.loop.model_service import ModelService
from qharness.loop.models import OutcomeStatus, Phase, StageOutcome, StagePlan, StartStage, SubmitOutcome
from qharness.loop.repository import digest
from qharness.loop.scheduler import ActionScheduler
from qharness.loop.tool_service import ToolService
from qharness.model.models import ChatMessage


class Actor:
    def __init__(self, model: ModelService, tools: ToolService):
        if model.repository.key != tools.repository.key:
            raise LoopConfigurationError("Actor 的模型与工具不属于同一 Run")
        self.model, self.tools = model, tools
        self.repository = model.repository
        self.config = model.compiler.config
        self.scheduler = ActionScheduler(tools, max_parallel_reads=self.config.max_parallel_reads)
        self._lock = asyncio.Lock()

    async def run(self, plan: StagePlan | None = None, *, attempt_id: str | None = None,
                  stream: bool = False, context_observations=()) -> StageOutcome:
        async with self._lock:
            state = (await asyncio.to_thread(self.repository.snapshot))["state"]
            if state is None:
                raise LoopConfigurationError("先安装 TodoPlan，再执行阶段")
            if state.phase == Phase.PLANNING:
                if plan is None or attempt_id is None:
                    raise LoopConfigurationError("启动阶段需要 StagePlan 和稳定 attempt_id")
                state = await asyncio.to_thread(self.repository.apply, StartStage(
                    run_id=state.run_id, expected_version=state.version, plan=plan, attempt_id=attempt_id))
            if not state.attempts:
                raise LoopConfigurationError("没有可执行的阶段尝试")
            attempt = state.attempts[-1]
            if (plan is not None and plan != attempt.plan) or (attempt_id is not None and attempt_id != attempt.attempt_id):
                raise LoopConfigurationError("请求不属于当前阶段尝试")
            if state.phase == Phase.VERIFYING and attempt.outcome is not None:
                return attempt.outcome
            if state.phase != Phase.ACTING:
                raise LoopConfigurationError("当前状态不允许执行阶段")
            try:
                return await self._run(state, stream=stream, context_observations=context_observations)
            except asyncio.CancelledError:
                self.tools.context.cancel()
                raise

    async def _run(self, state, *, stream, context_observations=()):
        attempt = state.attempts[-1]
        plan = attempt.plan
        identity = attempt.identity.model_dump()
        history: list[ChatMessage] = []
        observations: list[str] = []
        corrections = 0

        async def handoff(outcome):
            await asyncio.to_thread(self.repository.apply, SubmitOutcome(run_id=state.run_id,
                expected_version=state.version, outcome=outcome))
            return outcome

        def stopped(status, reason):
            return StageOutcome(**identity, status=status, summary=reason,
                                observation_refs=tuple(observations))

        for turn in range(1, self.config.max_actor_turns + 1):
            if self.tools.context.cancelled:
                return await handoff(stopped(OutcomeStatus.BLOCKED, "运行已取消，停止新行动"))
            call_id = "actor-" + digest([attempt.attempt_id, turn])
            try:
                result = await self.model.call(call_id, Role.ACTOR, messages=history, observations=context_observations,
                    tools=self.tools.executor.registry.definitions(), stream=stream,
                    cancellation_event=self.tools.context.cancellation_event, expected_version=state.version)
            except asyncio.CancelledError:
                if self.tools.context.cancelled:
                    return await handoff(stopped(OutcomeStatus.BLOCKED, "运行已取消；在途请求可能尚无确定结果"))
                self.tools.context.cancel()
                raise
            except LoopExecutionError as error:
                if error.code in {"protocol_error", "model_failed"}:
                    corrections += 1
                    if corrections > self.config.max_protocol_corrections:
                        return await handoff(stopped(OutcomeStatus.STALLED, "角色输出多次违反协议，交回外层处理"))
                    history.append(ChatMessage("user", "上次响应违反输出协议，未执行其中的工具。请严格按当前 Schema、阶段身份和完整工具协议重新输出。"))
                    continue
                if error.code in {"budget_exceeded", "unknown"}:
                    return await handoff(stopped(OutcomeStatus.BLOCKED, str(error)))
                raise
            except ModelBackendError as error:
                return await handoff(stopped(OutcomeStatus.BLOCKED, "模型服务不可用：" + str(error)))
            except LoopConfigurationError as error:
                return await handoff(stopped(OutcomeStatus.BLOCKED, "上下文或调用配置阻止继续：" + str(error)))

            if result.output is not None:
                outcome = result.output
                error = self._validate_handoff(outcome, plan, observations)
                if error:
                    corrections += 1
                    if corrections > self.config.max_protocol_corrections:
                        return await handoff(stopped(OutcomeStatus.STALLED, error))
                    history.extend([result.response.message, ChatMessage("user", error + "；请修正阶段交回或继续行动。")])
                    continue
                return await handoff(outcome)

            calls = result.response.message.tool_calls
            if len(calls) > self.config.max_batch_calls:
                corrections += 1
                if corrections > self.config.max_protocol_corrections:
                    return await handoff(stopped(OutcomeStatus.STALLED, "工具批次反复超过数量上限"))
                history.append(ChatMessage("user", f"工具批次超过 {self.config.max_batch_calls} 个，未执行；请拆分。"))
                continue
            # Provider 可在不同轮复用原始 call ID；对模型历史与逻辑执行做确定性命名隔离。
            message = replace(result.response.message, tool_calls=[replace(c,
                id="call-" + digest([call_id, c.id])[:32]) for c in calls])
            batch = await self.scheduler.execute(message.tool_calls, model_call_id=call_id, expected_version=state.version)
            history.append(message)
            for item in batch:
                receipt = {"observation_ref": item.logical_call_id if item.result.error_code != "skipped" else None,
                           "artifact_id": item.result.artifact_id,
                           "invocation_success": item.result.success, "result": item.result.to_model_content()}
                history.append(ChatMessage("tool", json.dumps(receipt, ensure_ascii=False), tool_call_id=item.provider_call_id))
                if item.result.error_code != "skipped":
                    observations.append(item.logical_call_id)
            validate_messages(history)
            if any(item.result.error_code in {"unknown", "budget_exceeded", "cancelled", "rejected"} for item in batch):
                return await handoff(stopped(OutcomeStatus.BLOCKED, "工具批次遇到未知效果、预算、取消或审批阻塞"))
            if any(item.result.error_code == "stale_context" for item in batch):
                raise LoopExecutionError("阶段状态已改变，停止旧行动循环", code="stale_context")
        return await handoff(stopped(OutcomeStatus.STALLED, "达到阶段行动轮数上限，交回外层规划"))

    @staticmethod
    def _validate_handoff(outcome, plan, observations):
        if not isinstance(outcome, StageOutcome):
            return "Actor 必须交回 StageOutcome"
        if not set(outcome.matched_stop_when) <= set(plan.stop_when):
            return "交回引用了 StagePlan 中不存在的停止条件"
        if not set(outcome.matched_replan_when) <= set(plan.replan_when):
            return "交回引用了 StagePlan 中不存在的重规划条件"
        if not set(outcome.observation_refs) <= set(observations):
            return "交回引用了当前尝试中不存在的观察"
        if outcome.status == OutcomeStatus.CANDIDATE:
            if outcome.reported_assumption_changes or outcome.matched_replan_when:
                return "关键前提改变时应交回 needs_replan"
            if not outcome.matched_stop_when or not (outcome.outputs or outcome.observation_refs):
                return "candidate 需要引用已满足的停止条件，并提供产出或观察依据"
        if outcome.status == OutcomeStatus.NEEDS_REPLAN and not (
            outcome.matched_replan_when and outcome.reported_assumption_changes
        ):
            return "needs_replan 需要引用重规划条件并说明关键前提变化"
        return None
