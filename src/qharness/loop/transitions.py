"""推理状态的纯 reducer：不调用模型、工具、数据库，也不读取时间。

调用方负责事件授权和原子版本提交。这里的 CAS 校验不会替代数据库 CAS。
模型建议必须先经控制器解释成事件；只有受信任的验证事件可以更新完成状态。
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import TypeAdapter, ValidationError

from qharness.exception import LoopTransitionError
from qharness.loop.models import (
    ApplyFeedback, CheckStatus, ContractModel, EvidenceLink, LoopEvent,
    Phase, Question, RecordStageVerdict, RecordTaskVerdict, RefreshContext, ReopenTodos,
    ReplaceTodoPlan, Resume, Route, RunState, StageAttempt, StageIdentity,
    StageKind, StartStage, Steer, SubmitOutcome, TaskContract, Terminate, TodoPlan,
    TodoState, TodoStatus, Wait,
)


T = TypeVar("T", bound=ContractModel)
EVENT_ADAPTER = TypeAdapter(LoopEvent)


def _updated(model: T, **changes: object) -> T:
    # model_copy(update=...) 不执行 Pydantic 校验，不能用在状态边界。
    return type(model).model_validate({
        **{key: getattr(model, key) for key in type(model).model_fields}, **changes,
    })


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LoopTransitionError(message)


def create_run(
    run_id: str, contract: TaskContract, plan: TodoPlan, *,
    questions: tuple[Question, ...] = (),
) -> RunState:
    """固定原始任务契约；后续 Todo 重规划只能调整其实现方式与分解。"""

    return RunState(
        run_id=run_id, contract=contract, todo_plan=plan,
        todos=tuple(TodoState(todo=t) for t in plan.todos), questions=questions,
    )


def _phase(state: RunState, *allowed: Phase) -> None:
    _require(state.phase in allowed, f"{state.phase} 状态不允许此事件")


def _identity(attempt: StageAttempt, result: StageIdentity) -> None:
    _require(all(getattr(result, k) == v for k, v in attempt.identity.model_dump().items()),
             "结果的阶段、Todo 版本或 attempt_id 已过期")


def _dependents(todos: tuple[TodoState, ...], roots: set[str]) -> set[str]:
    affected = set(roots)
    while True:
        expanded = affected | {t.todo.id for t in todos if set(t.todo.dependencies) & affected}
        if expanded == affected:
            return affected
        affected = expanded


def _invalidate(
    todos: tuple[TodoState, ...], evidence: tuple[EvidenceLink, ...], roots: set[str],
) -> tuple[tuple[TodoState, ...], tuple[EvidenceLink, ...]]:
    """依赖变化保守重开下游，丢弃相应通过凭据，历史尝试仍保留。"""

    affected = set(roots)
    while True:
        affected = _dependents(todos, affected)
        stale_refs = {ref for t in todos if t.todo.id in affected for ref in t.todo.acceptance_refs}
        # 当前证据尚未细分依赖：共享失效验收项的 PASS 也必须保守重开。
        shared = {t.todo.id for t in todos if set(t.todo.acceptance_refs) & stale_refs}
        if shared <= affected:
            break
        affected |= shared
    return (
        tuple(_updated(t, status=TodoStatus.PENDING, evidence_refs=())
              if t.todo.id in affected else t for t in todos),
        tuple(e for e in evidence if e.ref not in stale_refs),
    )


def _start(state: RunState, event: StartStage) -> dict[str, object]:
    _phase(state, Phase.PLANNING)
    plan = event.plan
    target = next((t for t in state.todos if t.todo.id == plan.todo_id), None)
    _require(target is not None, "阶段引用未知 Todo")
    assert target is not None
    _require(target.status != TodoStatus.PASSED, "已完成 Todo 必须先重开")
    _require(target.todo.version == plan.todo_version, "阶段绑定的 Todo 版本已过期")
    passed = {t.todo.id for t in state.todos if t.status == TodoStatus.PASSED}
    _require(set(target.todo.dependencies) <= passed, "Todo 依赖尚未完成")
    allowed = set(target.todo.acceptance_refs) | {
        q.id for q in state.questions if set(q.acceptance_refs) & set(target.todo.acceptance_refs)
    }
    _require(set(plan.addresses) <= allowed, "阶段 addresses 不属于当前 Todo 的验收项或相关问题")
    _require(event.attempt_id not in {a.attempt_id for a in state.attempts}, "attempt_id 不能重复使用")
    previous = state.attempts[-1] if state.attempts else None
    route = state.pending_decision.route if state.pending_decision else None
    if previous and route and previous.verdicts:
        invalid = {a for d in previous.verdicts[-1].diagnoses for a in d.invalidated_assumptions}
        _require(not set(plan.assumptions) & invalid, "新阶段不能沿用已被检查否定的前提")
    _require(route != Route.REPLAN_TODO, "必须先提交 TodoPlan 修订")
    same_stage = [a for a in state.attempts if a.plan.stage_id == plan.stage_id]
    if route == Route.REPAIR:
        _require(previous is not None and plan == previous.plan, "REPAIR 必须完整保留原 StagePlan")
    elif route == Route.REPLAN_STAGE:
        assert previous is not None
        _require(plan.stage_id == previous.plan.stage_id
                 and plan.plan_version == previous.plan.plan_version + 1
                 and plan.todo_id == previous.plan.todo_id,
                 "REPLAN_STAGE 必须保留阶段和 Todo 身份，并递增计划版本")
    else:
        _require(not same_stage and plan.plan_version == 1, "新阶段必须使用新 ID 和初始版本")
        if route == Route.INVESTIGATE:
            assert previous is not None
            _require(plan.kind == StageKind.INVESTIGATE and plan.todo_id == previous.plan.todo_id,
                     "INVESTIGATE 必须针对当前 Todo 创建调查阶段")
        if previous is not None and route == Route.ADVANCE:
            old = next(t for t in state.todos if t.todo.id == previous.plan.todo_id)
            _require(old.status == TodoStatus.PASSED or plan.todo_id == old.todo.id,
                     "当前 Todo 尚未完成，应先推进其下一阶段")
    attempt = StageAttempt(plan=plan, attempt_id=event.attempt_id, attempt_number=len(same_stage) + 1)
    return dict(
        attempts=(*state.attempts, attempt), active_attempt_id=event.attempt_id,
        phase=Phase.ACTING, pending_decision=None,
        todos=tuple(_updated(t, status=TodoStatus.ACTIVE) if t.todo.id == plan.todo_id else t
                    for t in state.todos),
    )


def _stage_verdict(state: RunState, event: RecordStageVerdict) -> dict[str, object]:
    _phase(state, Phase.VERIFYING)
    attempt = state.attempts[-1]
    verdict = event.verdict
    _identity(attempt, verdict)
    todo = next(t.todo for t in state.todos if t.todo.id == verdict.todo_id)
    delta = verdict.progress
    criteria = {c.id for c in state.contract.criteria}
    _require({e.ref for e in delta.satisfied_criteria} <= set(todo.acceptance_refs),
             "不能为其他 Todo 宣布验收通过")
    _require({e.ref for e in delta.regressed_criteria} <= criteria, "退化结果引用未知验收项")
    new_ids = {q.id for q in delta.new_questions}
    _require(not new_ids & ({q.id for q in state.questions} | criteria), "新增问题 ID 已存在")
    for question in delta.new_questions:
        _require(set(question.acceptance_refs) <= set(todo.acceptance_refs), "新增问题与当前 Todo 无关")
    questions = (*state.questions, *delta.new_questions)
    relevant = {q.id for q in questions if set(q.acceptance_refs) & set(todo.acceptance_refs)}
    for finding in (*delta.resolved_questions, *delta.narrowed_questions, *delta.eliminated_hypotheses):
        _require(finding.ref in relevant, "问题进展必须引用已知且与当前 Todo 相关的问题")
    _require(set(verdict.remaining_gaps) <= (set(todo.acceptance_refs) | relevant),
             "remaining_gaps 含悬空或无关引用")
    for diagnosis in verdict.diagnoses:
        _require(CheckStatus.FAIL in (verdict.stage_status, verdict.todo_status), "失败诊断需要实际失败结果")
        _require(set(diagnosis.evidence_refs) <= set(verdict.evidence_refs), "诊断必须引用本次验证证据")
        _require(set(diagnosis.invalidated_assumptions) <= set(attempt.plan.assumptions), "诊断引用未知阶段前提")
    if verdict.progress_report:
        report = verdict.progress_report
        _require(report.todo_id == todo.id, "进展报告属于其他 Todo")
        _require(set(report.unresolved_criteria) <= set(todo.acceptance_refs) and
                 set(report.unresolved_questions) <= relevant and
                 set(report.novel_refs) <= set(todo.acceptance_refs) | relevant, "进展报告含无关缺口")
    regressed = {e.ref for e in delta.regressed_criteria}
    roots = {t.todo.id for t in state.todos if set(t.todo.acceptance_refs) & regressed}
    todos, evidence = _invalidate(state.todos, state.satisfied_criteria, roots)
    satisfied = {e.ref: e for e in evidence}
    satisfied.update((e.ref, e) for e in delta.satisfied_criteria)
    if verdict.todo_status == CheckStatus.PASS:
        _require(set(todo.acceptance_refs) <= satisfied.keys(), "Todo PASS 缺少验收项通过证据")
        passed = {t.todo.id for t in todos if t.status == TodoStatus.PASSED}
        _require(set(todo.dependencies) <= passed, "Todo PASS 的上游依赖已失效")
    status = TodoStatus.PASSED if verdict.todo_status == CheckStatus.PASS else TodoStatus.ACTIVE
    todos = tuple(_updated(t, status=status, evidence_refs=verdict.evidence_refs if status == TodoStatus.PASSED else ())
                  if t.todo.id == todo.id else t for t in todos)
    attempt = _updated(attempt, verdicts=(*attempt.verdicts, verdict))
    return dict(
        attempts=(*state.attempts[:-1], attempt), todos=todos, questions=questions,
        satisfied_criteria=tuple(satisfied.values()), progress_history=(*state.progress_history, delta),
        phase=Phase.ROUTING,
    )


def _feedback(state: RunState, event: ApplyFeedback) -> dict[str, object]:
    _phase(state, Phase.ROUTING)
    decision = event.decision
    route = decision.route
    attempt = state.attempts[-1]
    verdict = attempt.verdicts[-1]
    assert attempt.outcome is not None
    plan = attempt.plan
    todo = next(t.todo for t in state.todos if t.todo.id == plan.todo_id)
    relevant = set(todo.acceptance_refs) | {q.id for q in state.questions if set(q.acceptance_refs) & set(todo.acceptance_refs)}
    _require(set(decision.focus) <= relevant, "反馈 focus 含当前 Todo 之外的目标")
    known_evidence = {ref for a in state.attempts for v in a.verdicts for ref in v.evidence_refs}
    known_evidence |= {ref for link in state.satisfied_criteria for ref in link.evidence_refs}
    known_evidence |= {ref for v in state.task_verdicts for link in (*v.failed_criteria, *v.criterion_evidence) for ref in link.evidence_refs}
    stage_ref = f"stage:{plan.stage_id}:v{plan.plan_version}"
    todo_ref = f"todo:{plan.todo_id}:v{plan.todo_version}"
    approach_ref = f"approach:{plan.stage_id}:v{plan.plan_version}"
    assumptions = {f"assumption:{plan.stage_id}:v{plan.plan_version}:{i}" for i in range(len(plan.assumptions))}
    evidence_objects = {"evidence:" + ref for ref in known_evidence}
    mutable = {stage_ref, approach_ref} | assumptions | evidence_objects
    if route == Route.REPLAN_TODO:
        mutable.add(todo_ref)
    allowed = mutable | {todo_ref, f"task:{state.contract.task_id}:v{state.contract.version}"}
    _require(set(decision.preserve) <= allowed and set(decision.invalidate) <= mutable,
             "保留/失效范围含未知对象，或试图删除原始任务义务")
    if route in (Route.REPAIR, Route.RETRY_CHECK):
        _require(not set(decision.invalidate) & ({stage_ref, approach_ref} | assumptions),
                 "保留方案的修复/检查重试不能同时使阶段或前提失效")
    if decision.change_strategy:
        _require(route in (Route.REPLAN_STAGE, Route.INVESTIGATE, Route.WAIT), "改变调查方法只能重规划、调查或等待")
    if route == Route.ADVANCE:
        _require(verdict.stage_status == CheckStatus.PASS, "ADVANCE 要求阶段已通过")
    elif route == Route.REPAIR:
        _require(verdict.todo_status != CheckStatus.PASS
                 and CheckStatus.FAIL in (verdict.stage_status, verdict.todo_status),
                 "REPAIR 需要尚未完成且存在失败的验证结果")
        _require(not attempt.outcome.reported_assumption_changes
                 and attempt.outcome.status != "needs_replan", "前提已改变，不能沿用阶段计划修复")
        _require(not any(d.invalidated_assumptions for d in verdict.diagnoses), "检查已否定前提，不能沿用阶段计划修复")
    elif route == Route.RETRY_CHECK:
        _require(verdict.todo_status != CheckStatus.PASS
                 and any(s in (CheckStatus.ERROR, CheckStatus.INCONCLUSIVE)
                         for s in (verdict.stage_status, verdict.todo_status)),
                 "RETRY_CHECK 需要检查错误或证据不足")
    elif route in (Route.REPLAN_STAGE, Route.INVESTIGATE, Route.REPLAN_TODO):
        _require(verdict.todo_status != CheckStatus.PASS, "Todo 已通过，应推进或明确重开")
    attempt = _updated(attempt, decision=decision)
    updates: dict[str, object] = dict(attempts=(*state.attempts[:-1], attempt))
    if route == Route.RETRY_CHECK:
        return dict(updates, phase=Phase.VERIFYING)
    if route == Route.WAIT:
        return dict(updates, phase=Phase.WAITING, resume_phase=Phase.ROUTING, wait_reason=decision.reason)
    if route == Route.TERMINATE:
        return dict(updates, phase=Phase.TERMINATED)
    next_phase = Phase.VERIFYING_TASK if all(t.status == TodoStatus.PASSED for t in state.todos) else Phase.PLANNING
    return dict(updates, phase=next_phase, active_attempt_id=None, pending_decision=decision,
                todos=tuple(_updated(t, status=TodoStatus.PENDING) if t.status == TodoStatus.ACTIVE else t
                            for t in state.todos))


def _replace_plan(state: RunState, event: ReplaceTodoPlan) -> dict[str, object]:
    _phase(state, Phase.PLANNING)
    _require(state.pending_decision is not None and state.pending_decision.route == Route.REPLAN_TODO,
             "只有 REPLAN_TODO 决策后才能替换 TodoPlan")
    plan = event.plan
    _require(plan.version == state.todo_plan.version + 1, "TodoPlan 版本必须递增一次")
    plan.validate_contract(state.contract)
    old = {t.todo.id: t for t in state.todos}
    changed = set(old) - {t.id for t in plan.todos}
    for todo in plan.todos:
        if todo.id not in old:
            _require(todo.version == 1, "新 Todo 必须从初始版本开始")
            changed.add(todo.id)
            continue
        before = old[todo.id].todo
        if todo == before:
            continue
        _require(todo.version == before.version + 1, "修订 Todo 必须递增版本")
        _require((todo.objective, todo.acceptance_refs, todo.done_when)
                 == (before.objective, before.acceptance_refs, before.done_when),
                 "不能原地改写 Todo 完成义务；重新分解请创建新 Todo 并保持任务验收覆盖")
        changed.add(todo.id)
    new = tuple(old[t.id] if t.id in old and old[t.id].todo == t else TodoState(todo=t) for t in plan.todos)
    # 旧图与新图都传播失效，依赖被删除或重接时不能复用旧 PASS。
    affected = _dependents(state.todos, changed)
    removed_refs = {r for t in state.todos if t.todo.id in changed for r in t.todo.acceptance_refs}
    shared = {t.todo.id for t in new if set(t.todo.acceptance_refs) & removed_refs}
    new, evidence = _invalidate(new, state.satisfied_criteria, affected | changed | shared)
    return dict(todo_plan=plan, todos=new, satisfied_criteria=evidence, pending_decision=None)


def _task_verdict(state: RunState, event: RecordTaskVerdict) -> dict[str, object]:
    _phase(state, Phase.VERIFYING_TASK)
    verdict = event.verdict
    _require(verdict.contract_version == state.contract.version, "任务验证绑定的契约版本已过期")
    required = {c.id for c in state.contract.criteria}
    _require({e.ref for e in (*verdict.criterion_evidence, *verdict.failed_criteria)} <= required,
             "任务验证含未知验收项")
    updates: dict[str, object] = dict(task_verdicts=(*state.task_verdicts, verdict))
    if verdict.status == CheckStatus.PASS:
        _require({e.ref for e in verdict.criterion_evidence} == required, "Task PASS 必须独立覆盖全部验收项")
        return dict(updates, phase=Phase.COMPLETED, pending_decision=None)
    if verdict.failed_criteria:
        failed = {e.ref for e in verdict.failed_criteria}
        roots = {t.todo.id for t in state.todos if set(t.todo.acceptance_refs) & failed}
        todos, evidence = _invalidate(state.todos, state.satisfied_criteria, roots)
        return dict(updates, phase=Phase.PLANNING, todos=todos,
                    satisfied_criteria=evidence, pending_decision=None)
    # ERROR / INCONCLUSIVE 未证明业务失败时保留 Todo 状态，继续最终验证或等待。
    return updates


def reduce(state: RunState, event: LoopEvent) -> RunState:
    """应用一次事件并返回新状态；拒绝过期/重复提交，输入对象保持不变。"""

    try:
        state = RunState.model_validate(state)
        event = EVENT_ADAPTER.validate_python(event)
        _require(event.run_id == state.run_id, "事件属于其他 Run")
        _require(event.expected_version == state.version, "Run 版本已过期，不能重复提交或覆盖新状态")
        _require(state.phase not in (Phase.COMPLETED, Phase.TERMINATED), "终态 Run 不再接收推理事件")
        if isinstance(event, StartStage):
            updates = _start(state, event)
        elif isinstance(event, SubmitOutcome):
            _phase(state, Phase.ACTING)
            attempt = state.attempts[-1]
            _identity(attempt, event.outcome)
            updates = dict(phase=Phase.VERIFYING, attempts=(
                *state.attempts[:-1], _updated(attempt, outcome=event.outcome),
            ))
        elif isinstance(event, RecordStageVerdict):
            updates = _stage_verdict(state, event)
        elif isinstance(event, ApplyFeedback):
            updates = _feedback(state, event)
        elif isinstance(event, ReplaceTodoPlan):
            updates = _replace_plan(state, event)
        elif isinstance(event, RecordTaskVerdict):
            updates = _task_verdict(state, event)
        elif isinstance(event, ReopenTodos):
            _phase(state, Phase.PLANNING, Phase.VERIFYING_TASK)
            roots = set(event.todo_ids)
            _require(roots <= {t.todo.id for t in state.todos}, "重开引用未知 Todo")
            todos, evidence = _invalidate(state.todos, state.satisfied_criteria, roots)
            updates = dict(phase=Phase.PLANNING, todos=todos, satisfied_criteria=evidence,
                           active_attempt_id=None, pending_decision=None)
        elif isinstance(event, Wait):
            _require(state.phase != Phase.WAITING, "已经处于等待状态")
            updates = dict(phase=Phase.WAITING, resume_phase=state.phase, wait_reason=event.reason)
        elif isinstance(event, Resume):
            _phase(state, Phase.WAITING)
            updates = dict(phase=state.resume_phase, resume_phase=None, wait_reason=None)
        elif isinstance(event, Steer):
            constraints = tuple(dict.fromkeys((*state.contract.constraints, *event.constraints)))
            _require(constraints != state.contract.constraints, "追加约束没有实际变化")
            contract = _updated(state.contract, version=state.contract.version + 1, constraints=constraints)
            todos, evidence = _invalidate(state.todos, state.satisfied_criteria, {t.todo.id for t in state.todos})
            updates = dict(contract=contract, todos=todos, satisfied_criteria=evidence, phase=Phase.PLANNING,
                           active_attempt_id=None, pending_decision=None, resume_phase=None, wait_reason=None)
        elif isinstance(event, RefreshContext):
            _phase(state, Phase.WAITING)
            updates = dict(phase=Phase.PLANNING, active_attempt_id=None, pending_decision=None,
                resume_phase=None, wait_reason=None, todos=tuple(
                    _updated(t, status=TodoStatus.PENDING) if t.status == TodoStatus.ACTIVE else t for t in state.todos))
        elif isinstance(event, Terminate):
            updates = dict(phase=Phase.TERMINATED, resume_phase=None, wait_reason=None)
        else:
            raise LoopTransitionError("未知推理事件")
        return _updated(state, version=state.version + 1, **updates)
    except ValidationError as error:
        raise LoopTransitionError(str(error)) from error
    except ValueError as error:
        if isinstance(error, LoopTransitionError):
            raise
        raise LoopTransitionError(str(error)) from error
