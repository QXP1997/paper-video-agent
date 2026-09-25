"""全局/阶段规划复用 ModelService，输出先经原 reducer 预检再提交。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from qharness.exception import LoopConfigurationError, LoopExecutionError, LoopTransitionError
from qharness.loop.config import Role
from qharness.loop.models import Phase, ReplaceTodoPlan, Route, RunState, Scope, StageKind, StartStage, Todo, TodoStatus
from qharness.loop.progress import check_addresses, source_fingerprint
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
    def __init__(self, model: ModelService, context: RunContext, *, progress=None):
        self.model, self.context, self.repository = model, context, model.repository
        self.progress = progress

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

    @staticmethod
    def validate_todo_checks(plan, checks):
        if checks is None:
            return
        for todo in plan.todos:
            required = set(todo.acceptance_refs) | set(todo.done_when)
            selected = [s for s in checks.specs if s.scope == Scope.TODO and set(s.targets) <= required]
            covered = {target for s in selected for target in s.targets}
            if required - covered:
                available = [s.targets for s in checks.specs if s.scope == Scope.TODO]
                raise ValueError("Todo 完成义务缺少 TODO 层检查覆盖：" + ", ".join(sorted(required-covered))
                    + "。可用 TODO 检查目标组：" + encode(available)
                    + "。acceptance_refs 只能引用 criterion ID；done_when 使用 TODO 层标签。"
                      "仅由 TASK 层检查覆盖的约束仍由最终 Task 检查保留，不复制成缺少 TODO 检查的完成标签。")

    async def initialize(self, *, observations=(), questions=(), checks=None) -> RunState:
        snapshot = await asyncio.to_thread(self.repository.snapshot)
        if snapshot["state"] is not None:
            return snapshot["state"]
        def validate(plan):
            if plan.version != 1 or any(t.version != 1 for t in plan.todos):
                raise ValueError("初始计划和 Todo 应从版本 1 开始")
            create_run(self.repository.run_id, snapshot["contract"], plan, questions=questions)
            self.validate_todo_checks(plan, checks)
        plan = await self._call(Role.TODO_PLANNER, None, validate, observations=observations)
        state = create_run(self.repository.run_id, snapshot["contract"], plan, questions=questions)
        await asyncio.to_thread(self.repository.install_state, state)
        return state

    async def revise(self, state: RunState, *, observations=(), checks=None) -> RunState:
        observations = (*observations, *await self.evidence_context(state))
        def event(patch):
            if patch.base_version != state.todo_plan.version:
                raise ValueError("计划修订基于旧版本")
            self.validate_todo_checks(patch.plan, checks)
            return ReplaceTodoPlan(run_id=state.run_id, expected_version=state.version, plan=patch.plan)
        patch = await self._call(Role.TODO_PLANNER, state, lambda p: reduce(state, event(p)),
                                 observations=observations, plan_patch=True)
        for ref in patch.evidence_refs:
            await asyncio.to_thread(self.repository.read_artifact, ref)
        await asyncio.to_thread(self.repository.put_artifact, digest(["todo-patch", state.version]),
                                patch.model_dump(mode="json"), expected_version=state.version)
        return await asyncio.to_thread(self.repository.apply, event(patch))

    async def stage(self, state: RunState, *, observations=(), checks=None) -> RunState:
        observations = (*observations, *await self.evidence_context(state))
        todo = select_todo(state)
        attempt_id = "attempt-" + digest([self.repository.key, state.version])[:32]
        if state.pending_decision and state.pending_decision.route == Route.REPAIR:
            plan = state.attempts[-1].plan
        else:
            config = self.model.compiler.config
            guide, report = None, None
            if config.dynamic_stage_planning or config.track_gap_progress:
                if self.progress is None or checks is None:
                    raise LoopConfigurationError("阶段策略需要现有 Verifier 和受信任检查目录")
                report = await self.progress.view(state, todo.id, specs=checks.specs)
                decision = state.pending_decision
                investigate = bool(decision and (decision.route == Route.INVESTIGATE or decision.change_strategy))
                kind = (StageKind.INVESTIGATE if investigate or report.unresolved_questions else
                        StageKind.IMPLEMENT if report.unresolved_criteria else StageKind.VALIDATE)
                focus = ((report.unresolved_questions or report.unresolved_criteria) if kind == StageKind.INVESTIGATE
                         else report.unresolved_criteria) or todo.acceptance_refs
                if not config.dynamic_stage_planning:
                    focus = (*report.unresolved_criteria, *report.unresolved_questions) or todo.acceptance_refs
                guide = {"kind": kind.value if config.dynamic_stage_planning else None,
                    "focus": focus, "progress": report.model_dump(mode="json"),
                    "enforce_kind": config.dynamic_stage_planning,
                    "change_strategy": bool(decision and decision.change_strategy),
                    "boundary": "围绕缺口设计可验证结果和交回条件；明确任务允许一个阶段完成；调查成功不等于 Todo 完成。"}
                observations = (*observations, encode({"stage_guidance": guide}))
                await asyncio.to_thread(self.repository.put_artifact, digest(["stage-guidance", state.version]),
                                        guide, expected_version=state.version)

            def validate(plan):
                projected = reduce(state, StartStage(run_id=state.run_id, expected_version=state.version,
                                                     plan=plan, attempt_id=attempt_id))
                if checks is not None:
                    covered = {target for s in checks.stage_checks(projected) if s.scope == Scope.STAGE for target in s.targets}
                    missing = set(plan.expected_results) - covered
                    if missing:
                        available = [s.targets for s in checks.specs if s.scope == Scope.STAGE]
                        raise ValueError("阶段结果缺少 STAGE 层检查覆盖：" + ", ".join(sorted(missing))
                                         + "。可用 STAGE 检查目标组：" + encode(available))
                if guide and config.dynamic_stage_planning and plan.kind.value != guide["kind"]:
                    raise ValueError("阶段类型必须对应当前关键不确定性或验收缺口：" + guide["kind"])
                if checks is not None and plan.information_sources:
                    available = {s.id for s in checks.specs if s.scope == Scope.STAGE and
                                 set(s.targets) <= set(plan.expected_results)}
                    if not set(plan.information_sources) <= available:
                        raise ValueError("information_sources 必须引用覆盖当前阶段结果的目录检查")
                if report and config.track_gap_progress:
                    open_refs = set(report.unresolved_criteria) | set(report.unresolved_questions)
                    if not report.unresolved_criteria:
                        open_refs.update(todo.acceptance_refs)  # 剩余 done_when 验证仍关联原验收项。
                    if not set(plan.addresses) <= open_refs or not set(plan.addresses) & set(guide["focus"]):
                        raise ValueError("阶段必须关联实际未解决缺口，不能反复改写已解决目标")
                    selected = [s for s in checks.stage_checks(projected) if s.scope == Scope.STAGE]
                    covered = set().union(*(check_addresses(s) for s in selected))
                    if (not selected or not set(plan.addresses) <= covered or
                            any(not check_addresses(s) or not check_addresses(s) <= set(plan.addresses) for s in selected)):
                        raise ValueError("阶段 addresses 必须有相关的受信任检查覆盖")
                    if guide["change_strategy"]:
                        previous = state.attempts[-1]
                        used = set(report.repeated_sources)
                        for ref in previous.verdicts[-1].evidence_refs:
                            # 来源比较只依赖已记录的检查定义，不读取模型对方法的自述。
                            evidence = self.progress.runner.read(ref)
                            if evidence.spec.scope == Scope.STAGE:
                                used.add(source_fingerprint(evidence.spec))
                        if not {source_fingerprint(s) for s in selected} - used:
                            raise ValueError("连续无新信息：须更换实际检查命令或输入来源，改名不算改变方法")
            plan = await self._call(Role.STAGE_PLANNER, state, validate,
                todo_id=todo.id, observations=observations)
        return await asyncio.to_thread(self.repository.apply, StartStage(run_id=state.run_id,
            expected_version=state.version, plan=plan, attempt_id=attempt_id))
