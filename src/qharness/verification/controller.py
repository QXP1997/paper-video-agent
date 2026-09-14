"""把实际检查归约为三层 Verdict；路由和下一阶段规划交给 Executor。"""

import asyncio
from collections.abc import Sequence

from qharness.exception import LoopConfigurationError, LoopExecutionError, WorkspaceError
from qharness.loop.config import Role
from qharness.loop.models import (
    CheckStatus, EvidenceLink, Phase, ProgressDelta, RecordStageVerdict, RecordTaskVerdict,
    QuestionFinding, ReopenTodos, Scope, StageVerdict, TaskVerdict, TodoStatus,
)
from qharness.loop.repository import digest, encode
from qharness.verification.contracts import CheckEvidence, CheckSpec, FailureBundle
from qharness.verification.evidence import combine
from qharness.verification.runner import CheckRunner
from qharness.workspace.version import workspace_fingerprint


class VerificationController:
    def __init__(self, runner: CheckRunner, model=None):
        self.runner, self.model, self.repository = runner, model, runner.repository

    async def _collect(self, verification_id, specs, state, judge, baselines):
        specs = tuple(CheckSpec.model_validate(s) for s in specs)
        if len({s.id for s in specs}) != len(specs):
            raise LoopConfigurationError("检查 ID 不能重复")
        task = state.phase == Phase.VERIFYING_TASK
        if not task and state.phase != Phase.VERIFYING:
            raise LoopConfigurationError("当前不处于验证阶段")
        if judge and self.model is None:
            raise LoopConfigurationError("语义验证需要已有 ModelService")
        if set(baselines) - {s.id for s in specs}:
            raise LoopConfigurationError("基线引用了未知检查")
        todo = None if task else next(t.todo for t in state.todos if t.todo.id == state.attempts[-1].plan.todo_id)
        allowed = ({Scope.TASK: {c.id for c in state.contract.criteria} | set(state.contract.constraints)} if task else
                   {Scope.STAGE: set(state.attempts[-1].plan.expected_results),
                    Scope.TODO: set(todo.acceptance_refs) | set(todo.done_when)})
        for spec in specs:
            if spec.scope not in allowed or not set(spec.targets) <= allowed[spec.scope]:
                raise LoopConfigurationError("检查目标不属于当前阶段、Todo 或原始任务要求")
        relevant = {q.id for q in state.questions if todo and set(q.acceptance_refs) & set(todo.acceptance_refs)}
        conclusions = [c for spec in specs for c in spec.conclusions]
        if (any(c.question_id not in relevant for c in conclusions)
                or len({c.question_id for c in conclusions}) != len(conclusions)):
            raise LoopConfigurationError("问题结论必须引用当前 Todo 的问题，且不能重复")
        manifest = {"version": state.version, "attempt": state.active_attempt_id, "judge": judge,
                    "specs": [s.model_dump(mode="json") for s in specs], "baselines": baselines}
        await asyncio.to_thread(self.repository.put_artifact, digest(["verification", verification_id]), manifest,
                                expected_version=state.version)
        evidence = []
        for spec in specs:
            evidence.append(await self.runner.run(verification_id, spec, expected_version=state.version,
                                                  baseline_ref=baselines.get(spec.id)))
        return tuple(evidence)

    async def _statuses(self, evidence):
        statuses = {}
        for e in evidence:
            statuses[e.id] = e.status
            if not await self.runner.current(e) and e.status != CheckStatus.ERROR:
                statuses[e.id] = CheckStatus.INCONCLUSIVE
        return statuses

    @staticmethod
    def _coverage(scope, required, evidence, statuses):
        results, links = {}, {}
        for target in required:
            checks = [e for e in evidence if e.spec.scope == scope and target in e.spec.targets]
            results[target] = combine(statuses[e.id] for e in checks)
            links[target] = tuple(e.id for e in checks)
        return results, links

    @staticmethod
    def _judge_gate(checked, proposed):
        if checked != CheckStatus.PASS:
            return checked  # 模型不能覆盖硬失败，也不能补出不存在的通过证据。
        # 语义反对意见表明覆盖尚不充分；不把模型推断升级为确定的业务失败。
        return CheckStatus.INCONCLUSIVE if proposed == CheckStatus.FAIL else proposed

    async def _judge(self, verification_id, state, evidence, task):
        observations = []
        for e in evidence:
            raw = [await asyncio.to_thread(self.repository.read_artifact, o.artifact_id)
                   for o in e.observations if o.artifact_id]
            observations.append(encode({"check_evidence": e.model_dump(mode="json"), "raw_results": raw}))
        result = await self.model.call(call_id=digest([verification_id, "judge"]), role=Role.JUDGE,
            observations=observations, task_check=task, expected_version=state.version,
            cancellation_event=self.runner.tools.context.cancellation_event)
        return result.output

    async def _record(self, verification_id, state, evidence, statuses, verdict, event_type):
        failures = []
        for e in evidence:
            status = statuses[e.id]
            if status == CheckStatus.PASS:
                continue
            reason = e.reason if status == e.status else "检查后输入发生变化，旧证据失效"
            failures.append(FailureBundle(check_id=e.spec.id, status=status, reason=reason, evidence_ref=e.id,
                artifact_refs=tuple(o.artifact_id for o in e.observations if o.artifact_id), pre_existing=e.pre_existing,
                next_action={CheckStatus.FAIL: "根据断言和基线诊断业务失败",
                             CheckStatus.ERROR: "先修复检查环境或确认未知执行结果",
                             CheckStatus.INCONCLUSIVE: "补采完整证据或调查不稳定结果"}[status]))
        if isinstance(verdict, TaskVerdict):
            required = {Scope.TASK: (*[c.id for c in state.contract.criteria], *state.contract.constraints)}
        else:
            todo = next(t.todo for t in state.todos if t.todo.id == verdict.todo_id)
            required = {Scope.STAGE: state.attempts[-1].plan.expected_results,
                        Scope.TODO: (*todo.acceptance_refs, *todo.done_when)}
        for scope, targets in required.items():
            for target in targets:
                if not any(e.spec.scope == scope and target in e.spec.targets for e in evidence):
                    failures.append(FailureBundle(check_id=f"missing:{scope}:{target}",
                        status=CheckStatus.INCONCLUSIVE, reason=f"没有检查覆盖 {scope} 的要求：{target}",
                        next_action="补充受信任检查并收集证据"))
        uncertain = (verdict.status != CheckStatus.PASS if isinstance(verdict, TaskVerdict) else
                     verdict.stage_status != CheckStatus.PASS or verdict.todo_status != CheckStatus.PASS)
        if uncertain and not failures:
            failures.append(FailureBundle(check_id="semantic_review", status=CheckStatus.INCONCLUSIVE,
                reason="确定性检查已通过，语义审查仍未确认完成", next_action="围绕语义缺口补采证据"))
        payload = {"expected_version": state.version, "verdict": verdict.model_dump(mode="json"),
                   "failures": [f.model_dump(mode="json") for f in failures],
                   "evidence_refs": [e.id for e in evidence]}
        await asyncio.to_thread(self.repository.put_artifact, self.report_ref(verification_id), payload,
                                expected_version=state.version)
        def guard():
            for e in evidence:
                if statuses[e.id] in (CheckStatus.PASS, CheckStatus.FAIL):
                    current = workspace_fingerprint(self.runner.tools.context.workspace, e.spec.inputs)
                    if current != e.workspace_after:
                        raise LoopExecutionError("提交验证时文件已变化", code="stale_context")
                    if e.definition_hash != self.runner.definition_hash(e.spec):
                        raise LoopExecutionError("提交验证时检查定义已变化", code="stale_context")
        await asyncio.to_thread(self.repository.apply,
            event_type(run_id=state.run_id, expected_version=state.version, verdict=verdict), guard=guard)
        return verdict

    @staticmethod
    def report_ref(verification_id):
        return digest(["verification-report", verification_id])

    async def verify_stage(self, verification_id: str, specs: Sequence[CheckSpec], *, judge=False,
                           baselines: dict[str, str] | None = None) -> StageVerdict:
        state = (await asyncio.to_thread(self.repository.snapshot))["state"]
        if state is None or state.phase != Phase.VERIFYING:
            raise LoopConfigurationError("Stage 验证必须在 Actor 交回之后")
        evidence = await self._collect(verification_id, specs, state, judge, baselines or {})
        proposal = await self._judge(verification_id, state, evidence, False) if judge else None
        # Judge 返回后再读一次磁盘；慢模型调用不能扩大旧证据的有效期。
        statuses = await self._statuses(evidence)
        attempt = state.attempts[-1]
        todo = next(t.todo for t in state.todos if t.todo.id == attempt.plan.todo_id)
        stage_results, _ = self._coverage(Scope.STAGE, attempt.plan.expected_results, evidence, statuses)
        todo_results, links = self._coverage(Scope.TODO, (*todo.acceptance_refs, *todo.done_when), evidence, statuses)
        stage_status, todo_status = combine(stage_results.values()), combine(todo_results.values())
        if proposal:
            stage_status = self._judge_gate(stage_status, proposal.stage_status)
            todo_status = self._judge_gate(todo_status, proposal.todo_status)
        if stage_status != CheckStatus.PASS and todo_status == CheckStatus.PASS:
            todo_status = CheckStatus.INCONCLUSIVE
        satisfied = tuple(EvidenceLink(ref=r, evidence_refs=links[r]) for r in todo.acceptance_refs
                          if todo_results[r] == CheckStatus.PASS)
        regressed = tuple(EvidenceLink(ref=r, evidence_refs=tuple(e for e in links[r] if statuses[e] == CheckStatus.FAIL)) for r in todo.acceptance_refs
                          if todo_results[r] == CheckStatus.FAIL)
        findings = {kind: tuple(QuestionFinding(ref=c.question_id, finding=c.finding, evidence_refs=(e.id,))
                               for e in evidence if statuses[e.id] == CheckStatus.PASS
                               for c in e.spec.conclusions if c.kind == kind)
                    for kind in ("resolved", "narrowed", "eliminated")}
        gaps = tuple(r for r in todo.acceptance_refs if todo_results[r] != CheckStatus.PASS)
        if todo_status != CheckStatus.PASS and not gaps:
            gaps = tuple(todo.acceptance_refs)  # done_when/语义仍缺证据，不能暗示 Todo 已完成。
        verdict = StageVerdict(**attempt.identity.model_dump(), stage_status=stage_status, todo_status=todo_status,
            expected_vs_observed=encode({"stage": stage_results, "todo": todo_results}),
            progress=ProgressDelta(satisfied_criteria=satisfied, regressed_criteria=regressed,
                resolved_questions=findings["resolved"], narrowed_questions=findings["narrowed"],
                eliminated_hypotheses=findings["eliminated"]),
            remaining_gaps=gaps, evidence_refs=tuple(e.id for e in evidence),
            diagnosis_hints=(proposal.expected_vs_observed,) if proposal else ())
        return await self._record(verification_id, state, evidence, statuses, verdict, RecordStageVerdict)

    async def verify_task(self, verification_id: str, specs: Sequence[CheckSpec], *, judge=False,
                          baselines: dict[str, str] | None = None) -> TaskVerdict:
        state = (await asyncio.to_thread(self.repository.snapshot))["state"]
        if state is None or state.phase != Phase.VERIFYING_TASK:
            raise LoopConfigurationError("Task 验证必须在全部 Todo 通过之后")
        evidence = await self._collect(verification_id, specs, state, judge, baselines or {})
        proposal = await self._judge(verification_id, state, evidence, True) if judge else None
        statuses = await self._statuses(evidence)
        criteria = tuple(c.id for c in state.contract.criteria)
        results, links = self._coverage(Scope.TASK, (*criteria, *state.contract.constraints), evidence, statuses)
        status = combine(results.values())
        if proposal:
            status = self._judge_gate(status, proposal.status)
        constraint_failures = tuple(e for r in state.contract.constraints if results[r] == CheckStatus.FAIL
                                    for e in links[r] if statuses[e] == CheckStatus.FAIL)
        failed = tuple(EvidenceLink(ref=r, evidence_refs=tuple(dict.fromkeys((
                           *[e for e in links[r] if statuses[e] == CheckStatus.FAIL], *constraint_failures))))
                       for r in criteria if results[r] == CheckStatus.FAIL or constraint_failures)
        failed_ids = {e.ref for e in failed}
        verdict = TaskVerdict(contract_version=state.contract.version, status=status,
            summary=encode({"requirements": results, "semantic_review": proposal.summary if proposal else None}),
            criterion_evidence=tuple(EvidenceLink(ref=r, evidence_refs=links[r]) for r in criteria
                                     if results[r] == CheckStatus.PASS and r not in failed_ids), failed_criteria=failed)
        return await self._record(verification_id, state, evidence, statuses, verdict, RecordTaskVerdict)

    async def refresh(self, specs: Sequence[CheckSpec]) -> tuple[str, ...]:
        """规划/最终验收边界重开失效 Todo，复用 reducer 的共享验收项和依赖传播。"""
        state = (await asyncio.to_thread(self.repository.snapshot))["state"]
        if state is None or state.phase not in (Phase.PLANNING, Phase.VERIFYING_TASK):
            raise LoopConfigurationError("证据失效检查须在规划或任务验证边界进行")
        definitions = {s.id: s for s in specs}
        stale = []
        for todo in state.todos:
            if todo.status != TodoStatus.PASSED:
                continue
            valid = bool(todo.evidence_refs)
            for ref in todo.evidence_refs:
                try:
                    e = await asyncio.to_thread(self.runner.read, ref)
                    spec = definitions.get(e.spec.id)
                    valid = valid and spec is not None and await self.runner.current(e, spec)
                except (LoopExecutionError, ValueError, WorkspaceError, OSError):
                    valid = False
            if not valid:
                stale.append(todo.todo.id)
        if stale:
            await asyncio.to_thread(self.repository.apply, ReopenTodos(run_id=state.run_id,
                expected_version=state.version, todo_ids=tuple(stale), reason="完成证据的文件、检查定义或环境已失效"))
        return tuple(stale)
