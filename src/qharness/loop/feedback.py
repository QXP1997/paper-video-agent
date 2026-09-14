"""基础反馈规则；具体错误分层和粒度策略在后续批次扩展本模块。"""

from qharness.exception import LoopConfigurationError
from qharness.loop.config import LoopConfig
from qharness.loop.models import CheckStatus, FeedbackDecision, OutcomeStatus, Phase, Route, RunState, StageKind


class FeedbackRouter:
    def __init__(self, config: LoopConfig):
        self.config = config

    def decide(self, state: RunState) -> FeedbackDecision:
        if state.phase != Phase.ROUTING:
            raise LoopConfigurationError("只有已验证阶段可以生成反馈")
        attempt = state.attempts[-1]
        verdict, outcome = attempt.verdicts[-1], attempt.outcome
        def decision(route, reason):
            return FeedbackDecision(route=route, reason=reason, evidence_refs=verdict.evidence_refs,
                                    focus=verdict.remaining_gaps or attempt.plan.addresses)
        def replan():
            if attempt.plan.plan_version - 1 < self.config.max_stage_replans:
                return decision(Route.REPLAN_STAGE, "阶段前提或方法需要修订，保留当前 Todo")
            if state.todo_plan.version - 1 < self.config.max_todo_replans and verdict.evidence_refs:
                return decision(Route.REPLAN_TODO, "多次阶段修订未解决当前缺口，依据验证证据重新分解 Todo")
            return decision(Route.WAIT, "规划修订上限已到，需补充信息或调整策略")
        if verdict.todo_status == CheckStatus.PASS:
            return decision(Route.ADVANCE, "Todo 已通过，推进后续工作或整体验收")
        if outcome.status == OutcomeStatus.BLOCKED:
            return decision(Route.WAIT, "行动报告阻塞，保留当前尝试等待处理")
        if outcome.status in (OutcomeStatus.NEEDS_REPLAN, OutcomeStatus.STALLED) or outcome.reported_assumption_changes:
            return replan()
        if verdict.stage_status in (CheckStatus.ERROR, CheckStatus.INCONCLUSIVE) or verdict.todo_status == CheckStatus.ERROR:
            if len(attempt.verdicts) - 1 < self.config.max_check_retries:
                return decision(Route.RETRY_CHECK, "先补采或重试检查，不重新执行 Actor")
            return decision(Route.WAIT, "检查仍不确定或不可用，停止自动重试并保留缺口")
        if verdict.stage_status == CheckStatus.PASS:
            return decision(Route.ADVANCE, "阶段已完成，围绕同一 Todo 的剩余缺口规划下一阶段")
        if attempt.plan.kind == StageKind.INVESTIGATE:
            return replan()
        repairs = sum(a.decision is not None and a.decision.route == Route.REPAIR for a in state.attempts
                      if (a.plan.stage_id, a.plan.plan_version) == (attempt.plan.stage_id, attempt.plan.plan_version))
        if repairs < self.config.max_stage_repairs:
            return decision(Route.REPAIR, "存在确定性失败且未报告前提变化，保留原 StagePlan 修复")
        return decision(Route.INVESTIGATE, "局部修复次数已到，先调查失败原因")
