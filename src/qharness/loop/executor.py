"""完整推理主线：每个执行边界由持久 RunState 决定，不另建循环状态。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from qharness.exception import LoopConfigurationError, LoopExecutionError, ModelBackendError
from qharness.loop.feedback import FeedbackRouter
from qharness.loop.models import ApplyFeedback, OutcomeStatus, Phase, Route, RunState, Terminate, Wait
from qharness.loop.planner import Planner
from qharness.loop.repository import digest
from qharness.verification import CheckCatalog

if TYPE_CHECKING:
    from qharness.loop.actor import Actor
    from qharness.verification import VerificationController


class Executor:
    def __init__(self, actor: Actor, verifier: VerificationController, *, router: FeedbackRouter | None = None):
        if actor.repository.key != verifier.repository.key:
            raise LoopConfigurationError("Executor 组件必须属于同一 Run")
        self.actor, self.verifier, self.repository = actor, verifier, actor.repository
        self.context, self.config = actor.tools.context, actor.config
        self.planner = Planner(actor.model, self.context, progress=verifier.progress)
        self.router = router or FeedbackRouter(self.config)
        self._lock = asyncio.Lock()

    async def _state(self):
        return (await asyncio.to_thread(self.repository.snapshot))["state"]

    async def _wait(self, reason):
        state = await self._state()
        if state is None:
            raise LoopExecutionError(reason, code="initialization_failed")
        if state.phase in (Phase.WAITING, Phase.COMPLETED, Phase.TERMINATED):
            return state
        return await asyncio.to_thread(self.repository.apply, Wait(run_id=state.run_id,
                                      expected_version=state.version, reason=reason))

    async def terminate(self, reason):
        """应用显式终止入口；正常预算耗尽/阻塞只进入 WAITING。"""
        state = await self._state()
        if state is None:
            raise LoopConfigurationError("尚未建立任务计划")
        return await asyncio.to_thread(self.repository.apply,
            Terminate(run_id=state.run_id, expected_version=state.version, reason=reason))

    async def run(self, checks: CheckCatalog, *, observations=(), questions=(), stream=False, judge=False) -> RunState:
        """一次调用运行至完成/等待/终止。WAITING 不自动恢复；调用方提交原 Resume 事件。"""
        if not isinstance(checks, CheckCatalog):
            raise LoopConfigurationError("Executor 需要应用提供 CheckCatalog")
        async with self._lock:
            try:
                # 固定本次调用的 grounding 与检查目录；新 Run 首次初始化时也可供 Planner 使用。
                notes = (*observations, checks.context())
                state = await self._state()
                if state is None:
                    if self.context.cancelled:
                        raise LoopExecutionError("规划前已取消，未建立 TodoPlan", code="initialization_failed")
                    state = await self.planner.initialize(observations=notes, questions=questions)
                while True:
                    state = await self._state()
                    if state.phase in (Phase.COMPLETED, Phase.TERMINATED, Phase.WAITING):
                        return state
                    if self.context.cancelled:
                        return await self._wait("运行已取消，停止派发新行动和检查")
                    if state.version >= self.config.max_run_events:
                        return await self._wait("Run 状态推进预算已到，不能用无限重规划代替完成")
                    unknown = await asyncio.to_thread(self.repository.unresolved_calls)
                    if unknown:
                        return await self._wait("存在未确定的已派发调用，先核对结果：" + ", ".join(unknown))
                    if state.phase == Phase.PLANNING:
                        if await self.verifier.refresh(checks.specs):
                            continue
                        if state.pending_decision and state.pending_decision.route == Route.REPLAN_TODO:
                            await self.planner.revise(state, observations=notes)
                            continue
                        if len(state.attempts) >= self.config.max_stage_attempts:
                            return await self._wait("阶段尝试预算已到，保留未完成任务")
                        await self.planner.stage(state, observations=notes, checks=checks)
                    elif state.phase == Phase.ACTING:
                        await self.actor.run(stream=stream,
                            context_observations=(*notes, *await self.planner.evidence_context(state)))
                    elif state.phase == Phase.VERIFYING:
                        if state.attempts[-1].outcome.status == OutcomeStatus.BLOCKED:
                            return await self._wait("Actor 阻塞：" + state.attempts[-1].outcome.summary)
                        await self.verifier.verify_stage(self.verification_id(state), checks.stage_checks(state),
                                                         judge=judge, current_specs=checks.specs)
                    elif state.phase == Phase.ROUTING:
                        decision = self.router.decide(state)
                        # 模型/扩展路由不得跳过原 reducer 的身份与完成语义校验。
                        await asyncio.to_thread(self.repository.apply, ApplyFeedback(
                            run_id=state.run_id, expected_version=state.version, decision=decision))
                    elif state.phase == Phase.VERIFYING_TASK:
                        # Task 使用全新检查独立覆盖原始契约；旧 Todo 证据退化不抢先掩盖集成失败原因。
                        retries = 0
                        for verdict in reversed(state.task_verdicts):
                            if verdict.status in ("pass", "fail"):
                                break
                            retries += 1
                        if retries > self.config.max_check_retries:
                            return await self._wait("最终验证仍不确定或存在环境错误，需补采证据")
                        await self.verifier.verify_task(self.verification_id(state), checks.task_checks(state), judge=judge)
                    else:
                        raise LoopConfigurationError("未知 Executor 状态")
            except asyncio.CancelledError:
                if self.context.cancelled:
                    return await self._wait("运行已取消，在途效果需按账本核对")
                self.context.cancel()
                await self._wait("Executor 被取消，保留当前恢复位置")
                raise
            except ModelBackendError:
                return await self._wait("模型服务不可用，保留当前规划或执行位置")
            except LoopConfigurationError as error:
                return await self._wait(str(error))
            except LoopExecutionError as error:
                if error.code in {"budget_exceeded", "unknown", "planning_failed", "protocol_error", "model_failed",
                                  "dependency_blocked", "stale_evidence", "check_environment"}:
                    return await self._wait(str(error))
                raise  # 存储损坏、过期 CAS、身份冲突不能被伪装成正常完成。

    @staticmethod
    def verification_id(state):
        return "verify-" + digest([state.run_id, state.version, state.active_attempt_id])[:32]
