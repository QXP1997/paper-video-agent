"""Agent Loop 推理契约与状态转换。实际角色调用由后续控制器装配。"""

from qharness.loop.models import (
    ApplyFeedback, CheckStatus, Criterion, EvidenceLink, FeedbackDecision,
    LoopEvent, OutcomeStatus, Phase, ProgressDelta, Question, QuestionFinding,
    RecordStageVerdict, RecordTaskVerdict, ReopenTodos, ReplaceTodoPlan, Resume,
    Route, RunState, Scope, StageAttempt, StageIdentity, StageKind, StageOutcome,
    StagePlan, StageVerdict, StartStage, SubmitOutcome, TaskContract, TaskVerdict,
    Terminate, Todo, TodoPlan, TodoPlanPatch, TodoState, TodoStatus, Wait,
)
from qharness.loop.transitions import create_run, reduce

__all__ = [
    "ApplyFeedback", "CheckStatus", "Criterion", "EvidenceLink", "FeedbackDecision",
    "LoopEvent", "OutcomeStatus", "Phase", "ProgressDelta", "Question", "QuestionFinding",
    "RecordStageVerdict", "RecordTaskVerdict", "ReopenTodos", "ReplaceTodoPlan", "Resume",
    "Route", "RunState", "Scope", "StageAttempt", "StageIdentity", "StageKind", "StageOutcome",
    "StagePlan", "StageVerdict", "StartStage", "SubmitOutcome", "TaskContract", "TaskVerdict",
    "Terminate", "Todo", "TodoPlan", "TodoPlanPatch", "TodoState", "TodoStatus", "Wait", "create_run", "reduce",
]
