"""推理契约与可序列化状态；不持有 Backend、工作区、锁或协程。

这些对象描述角色之间交换的结果，不代表模型已被授权修改 RunState。
只有控制器提交的事件能够经 transitions.reduce 推进状态。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Version = Annotated[int, Field(strict=True, ge=1)]


class ContractModel(BaseModel):
    """JSON 输出边界拒绝未知字段；tuple 与 frozen 保证正常访问不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


def unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} 含重复 ID")


class Scope(StrEnum):
    STAGE = "stage"
    TODO = "todo"
    TASK = "task"


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


class StageKind(StrEnum):
    INVESTIGATE = "investigate"
    IMPLEMENT = "implement"
    VALIDATE = "validate"


class OutcomeStatus(StrEnum):
    CANDIDATE = "candidate"
    NEEDS_REPLAN = "needs_replan"
    BLOCKED = "blocked"
    STALLED = "stalled"


class Route(StrEnum):
    REPAIR = "repair"
    INVESTIGATE = "investigate"
    REPLAN_STAGE = "replan_stage"
    REPLAN_TODO = "replan_todo"
    ADVANCE = "advance"
    RETRY_CHECK = "retry_check"
    WAIT = "wait"
    TERMINATE = "terminate"


class Phase(StrEnum):
    PLANNING = "planning"
    ACTING = "acting"
    VERIFYING = "verifying"
    ROUTING = "routing"
    VERIFYING_TASK = "verifying_task"
    WAITING = "waiting"
    COMPLETED = "completed"
    TERMINATED = "terminated"


class TodoStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    PASSED = "passed"


class Criterion(ContractModel):
    id: Text
    description: Text


class TaskContract(ContractModel):
    task_id: Text
    version: Version = 1
    objective: Text
    criteria: tuple[Criterion, ...] = Field(min_length=1)
    constraints: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def validate_ids(self) -> Self:
        unique(tuple(c.id for c in self.criteria), "任务验收项")
        return self


class Todo(ContractModel):
    id: Text
    version: Version = 1
    objective: Text
    acceptance_refs: tuple[Text, ...] = Field(min_length=1)
    dependencies: tuple[Text, ...] = ()
    done_when: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_refs(self) -> Self:
        unique(self.acceptance_refs, "Todo 验收引用")
        unique(self.dependencies, "Todo 依赖")
        if self.id in self.dependencies:
            raise ValueError("Todo 不能依赖自身")
        return self


class TodoPlan(ContractModel):
    version: Version = 1
    todos: tuple[Todo, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        unique(tuple(t.id for t in self.todos), "Todo")
        remaining = {t.id: set(t.dependencies) for t in self.todos}
        if any(deps - remaining.keys() for deps in remaining.values()):
            raise ValueError("Todo 依赖不存在")
        # 拓扑剥离，避免长依赖链触发递归深度限制。
        while remaining:
            ready = {key for key, deps in remaining.items() if not deps}
            if not ready:
                raise ValueError("Todo 依赖存在环")
            remaining = {key: deps - ready for key, deps in remaining.items() if key not in ready}
        return self

    def validate_contract(self, contract: TaskContract) -> None:
        required = {c.id for c in contract.criteria}
        covered = {ref for todo in self.todos for ref in todo.acceptance_refs}
        if required != covered:
            raise ValueError("TodoPlan 必须覆盖全部任务验收项，且不能引用未知验收项")


class Question(ContractModel):
    id: Text
    description: Text
    acceptance_refs: tuple[Text, ...] = Field(min_length=1)


class StagePlan(ContractModel):
    stage_id: Text
    plan_version: Version = 1
    todo_id: Text
    todo_version: Version
    kind: StageKind
    objective: Text
    addresses: tuple[Text, ...] = Field(min_length=1)
    assumptions: tuple[Text, ...] = ()
    expected_results: tuple[Text, ...] = Field(min_length=1)
    approach: tuple[Text, ...] = ()
    stop_when: tuple[Text, ...] = Field(min_length=1)
    replan_when: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_addresses(self) -> Self:
        unique(self.addresses, "阶段 addresses")
        return self


class StageIdentity(ContractModel):
    stage_id: Text
    plan_version: Version
    todo_id: Text
    todo_version: Version
    attempt_id: Text


class StageOutcome(StageIdentity):
    status: OutcomeStatus
    summary: Text
    outputs: tuple[Text, ...] = ()
    observation_refs: tuple[Text, ...] = ()
    reported_assumption_changes: tuple[Text, ...] = ()
    remaining_questions: tuple[Text, ...] = ()


class EvidenceLink(ContractModel):
    """批次 1 只校验引用；证据来源与工作区有效性由后续 Verifier 实现。"""

    ref: Text
    evidence_refs: tuple[Text, ...] = Field(min_length=1)


class QuestionFinding(EvidenceLink):
    finding: Text


class ProgressDelta(ContractModel):
    satisfied_criteria: tuple[EvidenceLink, ...] = ()
    regressed_criteria: tuple[EvidenceLink, ...] = ()
    resolved_questions: tuple[QuestionFinding, ...] = ()
    narrowed_questions: tuple[QuestionFinding, ...] = ()
    eliminated_hypotheses: tuple[QuestionFinding, ...] = ()
    new_questions: tuple[Question, ...] = ()

    @model_validator(mode="after")
    def validate_changes(self) -> Self:
        satisfied = tuple(e.ref for e in self.satisfied_criteria)
        regressed = tuple(e.ref for e in self.regressed_criteria)
        unique(satisfied, "已满足验收项")
        unique(regressed, "退化验收项")
        if set(satisfied) & set(regressed):
            raise ValueError("同一验收项不能同时满足和退化")
        unique(tuple(q.id for q in self.new_questions), "新增问题")
        return self


class StageVerdict(StageIdentity):
    stage_status: CheckStatus
    todo_status: CheckStatus
    expected_vs_observed: Text
    progress: ProgressDelta = Field(default_factory=ProgressDelta)
    remaining_gaps: tuple[Text, ...] = ()
    evidence_refs: tuple[Text, ...] = ()
    diagnosis_hints: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def validate_pass(self) -> Self:
        if CheckStatus.PASS in (self.stage_status, self.todo_status) and not self.evidence_refs:
            raise ValueError("PASS 必须引用证据")
        if self.todo_status == CheckStatus.PASS and self.stage_status != CheckStatus.PASS:
            raise ValueError("Todo PASS 不能与当前阶段未通过同时提交")
        if self.todo_status == CheckStatus.PASS and self.remaining_gaps:
            raise ValueError("Todo PASS 不能仍有未完成缺口")
        return self


class FeedbackDecision(ContractModel):
    route: Route
    reason: Text
    evidence_refs: tuple[Text, ...] = ()
    focus: tuple[Text, ...] = ()


class TaskVerdict(ContractModel):
    contract_version: Version
    status: CheckStatus
    summary: Text
    criterion_evidence: tuple[EvidenceLink, ...] = ()
    failed_criteria: tuple[EvidenceLink, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        unique(tuple(e.ref for e in self.criterion_evidence), "任务通过证据")
        unique(tuple(e.ref for e in self.failed_criteria), "任务失败证据")
        if self.status == CheckStatus.PASS and (not self.criterion_evidence or self.failed_criteria):
            raise ValueError("Task PASS 必须有通过证据，且不能包含失败项")
        if self.status == CheckStatus.FAIL and not self.failed_criteria:
            raise ValueError("Task FAIL 必须指出有证据的失败验收项")
        if {e.ref for e in self.criterion_evidence} & {e.ref for e in self.failed_criteria}:
            raise ValueError("任务验收结果互相矛盾")
        return self


class TodoState(ContractModel):
    todo: Todo
    status: TodoStatus = TodoStatus.PENDING
    evidence_refs: tuple[Text, ...] = ()


class StageAttempt(ContractModel):
    plan: StagePlan
    attempt_id: Text
    attempt_number: Version
    outcome: StageOutcome | None = None
    verdicts: tuple[StageVerdict, ...] = ()
    decision: FeedbackDecision | None = None

    @property
    def identity(self) -> StageIdentity:
        return StageIdentity(
            stage_id=self.plan.stage_id, plan_version=self.plan.plan_version,
            todo_id=self.plan.todo_id, todo_version=self.plan.todo_version,
            attempt_id=self.attempt_id,
        )

    @model_validator(mode="after")
    def validate_results(self) -> Self:
        for result in (self.outcome, *self.verdicts):
            if result is not None and any(
                getattr(result, key) != value for key, value in self.identity.model_dump().items()
            ):
                raise ValueError("阶段结果不属于当前执行尝试")
        if self.verdicts and self.outcome is None:
            raise ValueError("没有阶段产出不能记录验证")
        return self


class RunState(ContractModel):
    run_id: Text
    version: Version = 1
    contract: TaskContract
    todo_plan: TodoPlan
    todos: tuple[TodoState, ...]
    phase: Phase = Phase.PLANNING
    questions: tuple[Question, ...] = ()
    satisfied_criteria: tuple[EvidenceLink, ...] = ()
    progress_history: tuple[ProgressDelta, ...] = ()
    attempts: tuple[StageAttempt, ...] = ()
    active_attempt_id: Text | None = None
    pending_decision: FeedbackDecision | None = None
    task_verdicts: tuple[TaskVerdict, ...] = ()
    resume_phase: Phase | None = None
    wait_reason: Text | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        self.todo_plan.validate_contract(self.contract)
        if tuple(t.todo for t in self.todos) != self.todo_plan.todos:
            raise ValueError("Todo 状态与当前计划不一致")
        required = {c.id for c in self.contract.criteria}
        unique(tuple(q.id for q in self.questions), "问题")
        for q in self.questions:
            if q.id in required or not set(q.acceptance_refs) <= required:
                raise ValueError("问题 ID 与验收项冲突或引用未知验收项")
        unique(tuple(e.ref for e in self.satisfied_criteria), "进度验收项")
        if not {e.ref for e in self.satisfied_criteria} <= required:
            raise ValueError("进度含未知验收项")
        passed = {t.todo.id for t in self.todos if t.status == TodoStatus.PASSED}
        satisfied = {e.ref for e in self.satisfied_criteria}
        for todo in self.todos:
            if todo.status == TodoStatus.PASSED:
                if not todo.evidence_refs or not set(todo.todo.acceptance_refs) <= satisfied:
                    raise ValueError("已通过 Todo 缺少验收证据")
                if not set(todo.todo.dependencies) <= passed:
                    raise ValueError("已通过 Todo 的依赖尚未通过")
        unique(tuple(a.attempt_id for a in self.attempts), "执行尝试")
        if self.active_attempt_id is not None:
            if not self.attempts or self.attempts[-1].attempt_id != self.active_attempt_id:
                raise ValueError("活动执行尝试必须是最后一个尝试")
        if self.phase in (Phase.ACTING, Phase.VERIFYING, Phase.ROUTING):
            if self.active_attempt_id is None:
                raise ValueError("执行与验证阶段必须绑定执行尝试")
            current = self.attempts[-1]
            if self.phase == Phase.ACTING and current.outcome is not None:
                raise ValueError("已有阶段产出不能继续 ACTING")
            if self.phase != Phase.ACTING and current.outcome is None:
                raise ValueError("验证与路由必须先有阶段产出")
            if self.phase == Phase.ROUTING and not current.verdicts:
                raise ValueError("路由必须先有验证结果")
        if self.phase == Phase.WAITING:
            if self.resume_phase not in (Phase.PLANNING, Phase.ACTING, Phase.VERIFYING,
                                         Phase.ROUTING, Phase.VERIFYING_TASK) or not self.wait_reason:
                raise ValueError("等待必须记录可恢复阶段及原因")
        elif self.resume_phase is not None or self.wait_reason is not None:
            raise ValueError("非等待状态不能携带恢复阶段")
        if self.phase in (Phase.VERIFYING_TASK, Phase.COMPLETED):
            if any(t.status != TodoStatus.PASSED for t in self.todos):
                raise ValueError("最终任务验证要求所有 Todo 已通过")
        if self.phase == Phase.COMPLETED:
            if not self.task_verdicts or self.task_verdicts[-1].status != CheckStatus.PASS:
                raise ValueError("Run 完成必须有独立 Task PASS")
            verdict = self.task_verdicts[-1]
            if verdict.contract_version != self.contract.version or {
                e.ref for e in verdict.criterion_evidence
            } != required:
                raise ValueError("Task PASS 必须覆盖当前任务契约")
        return self


class Event(ContractModel):
    """由可信控制器附加运行身份与 CAS 版本；不是模型可自由指定的授权。"""

    run_id: Text
    expected_version: Version


class StartStage(Event):
    type: Literal["start_stage"] = "start_stage"
    plan: StagePlan
    attempt_id: Text


class SubmitOutcome(Event):
    type: Literal["submit_outcome"] = "submit_outcome"
    outcome: StageOutcome


class RecordStageVerdict(Event):
    type: Literal["stage_verdict"] = "stage_verdict"
    verdict: StageVerdict


class ApplyFeedback(Event):
    type: Literal["apply_feedback"] = "apply_feedback"
    decision: FeedbackDecision


class ReplaceTodoPlan(Event):
    type: Literal["replace_todo_plan"] = "replace_todo_plan"
    plan: TodoPlan


class ReopenTodos(Event):
    type: Literal["reopen_todos"] = "reopen_todos"
    todo_ids: tuple[Text, ...] = Field(min_length=1)
    reason: Text


class RecordTaskVerdict(Event):
    type: Literal["task_verdict"] = "task_verdict"
    verdict: TaskVerdict


class Wait(Event):
    type: Literal["wait"] = "wait"
    reason: Text


class Resume(Event):
    type: Literal["resume"] = "resume"


class Terminate(Event):
    type: Literal["terminate"] = "terminate"
    reason: Text


LoopEvent = Annotated[
    StartStage | SubmitOutcome | RecordStageVerdict | ApplyFeedback | ReplaceTodoPlan
    | ReopenTodos | RecordTaskVerdict | Wait | Resume | Terminate,
    Field(discriminator="type"),
]
