"""全局/阶段规划复用 ModelService，输出先经原 reducer 预检再提交。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from qharness.exception import LoopConfigurationError, LoopExecutionError, LoopTransitionError
from qharness.loop.config import Role
from qharness.loop.models import Phase, ReplaceTodoPlan, Route, RunState, StartStage, Todo, TodoStatus
from qharness.loop.repository import digest, encode
from qharness.loop.transitions import create_run, reduce

if TYPE_CHECKING:
    from qharness.loop.model_service import ModelService
    from qharness.run.context import RunContext


def select_todo(state: RunState) -> Todo:
    if state.phase != Phase.PLANNING:
        raise LoopConfigurationError("只能在规划阶段选择 Todo")
    passed = {t.todo.id for t in state.todos if t.status == TodoStatus.PASSED}
    ready = [t.todo for t in state.todos if t.status != TodoStatus.PASSED and set(t.todo.dependencies) <= passed]
    if state.pending_decision and state.attempts:
        previous = state.attempts[-1].plan.todo_id
        target = next((t for t in state.todos if t.todo.id == previous), None)
        if target and target.status != TodoStatus.PASSED:
            chosen = next((t for t in ready if t.id == previous), None)
            if chosen is None:
                raise LoopExecutionError("当前 Todo 的上游依赖尚未完成", code="dependency_blocked")
            return chosen
    if not ready:
        raise LoopExecutionError("没有依赖满足的未完成 Todo，需核对状态与依赖", code="dependency_blocked")
    return ready[0]


class Planner:
    def __init__(self, model: ModelService, context: RunContext):
        self.model, self.context, self.repository = model, context, model.repository

    async def evidence_context(self, state: RunState) -> tuple[str, ...]:
        """为下一阶段和修复回填最近的实际验证输出，不只把 Evidence ID 交给模型。"""
        refs = []
        previous = next((a for a in reversed(state.attempts) if a.verdicts), None)
        if previous:
            refs.extend(previous.verdicts[-1].evidence_refs)
        if state.task_verdicts:
            last = state.task_verdicts[-1]
            refs.extend(ref for link in (*last.failed_criteria, *last.criterion_evidence) for ref in link.evidence_refs)
        notes = []
        for ref in dict.fromkeys(refs):
            try:
                evidence = await asyncio.to_thread(self.repository.read_artifact, ref)
            except LoopExecutionError as error:
                if error.code != "not_found":
                    raise
                notes.append(encode({"unavailable_evidence_ref": ref}))
                continue
            raw = []
            if isinstance(evidence, dict) and evidence.get("kind") == "check_evidence_v1":
                for observation in evidence["observations"]:
                    if observation.get("artifact_id"):
                        raw.append(await asyncio.to_thread(self.repository.read_artifact, observation["artifact_id"]))
            notes.append(encode({"historical_evidence": evidence, "raw_check_results": raw,
                                 "notice": "这是已有验证记录，当前完成仍须由 Verifier 核对有效性。"}))
        return tuple(notes)

    async def _call(self, role, state, validator, *, observations=(), **kwargs):
        version = state.version if state else 0
        problem = None
        for correction in range(self.model.compiler.config.max_protocol_corrections + 1):
            notes = (*observations, f"规划协议修正次数={correction}；输出须满足当前版本、依赖和反馈路由约束。")
            if problem:
                notes = (*notes, "上次输出未通过检查：" + problem)
            try:
                result = await self.model.call("plan-" + digest([role.value, version, correction]), role,
                    observations=notes, cancellation_event=self.context.cancellation_event,
                    expected_version=version, **kwargs)
                validator(result.output)
                return result.output
            except (LoopTransitionError, ValueError) as error:
                if isinstance(error, LoopConfigurationError):
                    raise
                problem = str(error)
            except LoopExecutionError as error:
                if error.code not in {"protocol_error", "model_failed"}:
                    raise
                # 首次错误与重入账本错误的文本可能不同；使用稳定提示保证同逻辑调用可重放读取。
                problem = "角色 JSON、版本、验收引用或证据引用不符合当前输出 Schema 与状态约束"
        raise LoopExecutionError("规划输出持续不满足契约，停止自动规划", code="planning_failed")

    async def initialize(self, *, observations=(), questions=()) -> RunState:
        snapshot = await asyncio.to_thread(self.repository.snapshot)
        if snapshot["state"] is not None:
            return snapshot["state"]
        def validate(plan):
            if plan.version != 1 or any(t.version != 1 for t in plan.todos):
                raise ValueError("初始计划和 Todo 应从版本 1 开始")
            create_run(self.repository.run_id, snapshot["contract"], plan, questions=questions)
        plan = await self._call(Role.TODO_PLANNER, None, validate, observations=observations)
        state = create_run(self.repository.run_id, snapshot["contract"], plan, questions=questions)
        await asyncio.to_thread(self.repository.install_state, state)
        return state

    async def revise(self, state: RunState, *, observations=()) -> RunState:
        observations = (*observations, *await self.evidence_context(state))
        def event(patch):
            if patch.base_version != state.todo_plan.version:
                raise ValueError("计划修订基于旧版本")
            return ReplaceTodoPlan(run_id=state.run_id, expected_version=state.version, plan=patch.plan)
        patch = await self._call(Role.TODO_PLANNER, state, lambda p: reduce(state, event(p)),
                                 observations=observations, plan_patch=True)
        for ref in patch.evidence_refs:
            await asyncio.to_thread(self.repository.read_artifact, ref)
        await asyncio.to_thread(self.repository.put_artifact, digest(["todo-patch", state.version]),
                                patch.model_dump(mode="json"), expected_version=state.version)
        return await asyncio.to_thread(self.repository.apply, event(patch))

    async def stage(self, state: RunState, *, observations=()) -> RunState:
        observations = (*observations, *await self.evidence_context(state))
        todo = select_todo(state)
        attempt_id = "attempt-" + digest([self.repository.key, state.version])[:32]
        if state.pending_decision and state.pending_decision.route == Route.REPAIR:
            plan = state.attempts[-1].plan
        else:
            plan = await self._call(Role.STAGE_PLANNER, state, lambda p: reduce(state, StartStage(
                run_id=state.run_id, expected_version=state.version, plan=p, attempt_id=attempt_id)),
                todo_id=todo.id, observations=observations)
        return await asyncio.to_thread(self.repository.apply, StartStage(run_id=state.run_id,
            expected_version=state.version, plan=plan, attempt_id=attempt_id))
