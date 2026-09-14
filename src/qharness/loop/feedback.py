"""分层反馈与基线策略；路由改变须说明保留/失效范围。"""

from qharness.exception import LoopConfigurationError
from qharness.loop.config import LoopConfig
from qharness.loop.models import CheckStatus, FailureLayer, FeedbackDecision, OutcomeStatus, Phase, Route, RunState, StageKind


class FeedbackRouter:
    def __init__(self, config: LoopConfig):
        self.config = config

    def decide(self, state: RunState) -> FeedbackDecision:
        baseline = self._baseline(state)
        if not (self.config.layered_feedback or self.config.track_gap_progress):
            return baseline
        attempt = state.attempts[-1]
        verdict, outcome, plan = attempt.verdicts[-1], attempt.outcome, attempt.plan
        report = verdict.progress_report
        route, reason, layer, change = baseline.route, baseline.reason, None, False
        if verdict.todo_status != CheckStatus.PASS and outcome.status != OutcomeStatus.BLOCKED:
            if self.config.layered_feedback:
                layers = {d.layer for d in verdict.diagnoses}
                if CheckStatus.ERROR in (verdict.stage_status, verdict.todo_status):
                    layer = FailureLayer.CHECK_ENVIRONMENT
                    route = Route.RETRY_CHECK if len(attempt.verdicts) - 1 < self.config.max_check_retries else Route.WAIT
                    reason = "检查环境没有给出完整结论，先处理检查，不修改业务方案"
                elif FailureLayer.TODO_DECOMPOSITION in layers:
                    layer = FailureLayer.TODO_DECOMPOSITION
                    route = Route.REPLAN_TODO if state.todo_plan.version - 1 < self.config.max_todo_replans else Route.WAIT
                    reason = "受信任检查证明任务分解存在问题，保留原始契约并修订 Todo"
                elif FailureLayer.STAGE_ASSUMPTION in layers:
                    layer = FailureLayer.STAGE_ASSUMPTION
                    route = Route.REPLAN_STAGE if plan.plan_version - 1 < self.config.max_stage_replans else Route.WAIT
                    reason = "阶段前提被检查否定，不能继续沿用原计划盲修"
                elif outcome.reported_assumption_changes or outcome.status == OutcomeStatus.NEEDS_REPLAN:
                    layer, route = FailureLayer.UNKNOWN, Route.INVESTIGATE
                    reason = "模型报告前提改变，但尚缺少确定诊断；先查明影响范围"
                elif layers == {FailureLayer.LOCAL_ACTION}:
                    layer = FailureLayer.LOCAL_ACTION
                    reason = "局部错误有检查证据支持；阶段未通过时沿用有效计划作有界修复"
                elif verdict.stage_status == CheckStatus.FAIL:
                    layer = FailureLayer.UNKNOWN
                    route = Route.INVESTIGATE if plan.kind != StageKind.INVESTIGATE else Route.REPLAN_STAGE
                    change = plan.kind == StageKind.INVESTIGATE
                    if change and plan.plan_version - 1 >= self.config.max_stage_replans:
                        route = Route.WAIT
                    reason = "业务失败尚未定位原因，先调查；调查本身失败则改变信息获取方式"
                elif verdict.stage_status == CheckStatus.INCONCLUSIVE:
                    layer = FailureLayer.EVIDENCE
            if (self.config.track_gap_progress and report and verdict.stage_status == CheckStatus.PASS
                    and report.stalled_investigations >= self.config.max_no_progress_investigations):
                layer, change = FailureLayer.EVIDENCE, True
                route = Route.REPLAN_STAGE if plan.plan_version - 1 < self.config.max_stage_replans else Route.WAIT
                reason = "连续调查未新增验收或问题结论，必须更换实际信息来源，不能只改写阶段目标"
        focus = baseline.focus
        if report:
            if route == Route.INVESTIGATE or change:
                focus = report.unresolved_questions or report.unresolved_criteria or plan.addresses
            else:
                focus = report.unresolved_criteria or report.unresolved_questions or plan.addresses
        task_ref = f"task:{state.contract.task_id}:v{state.contract.version}"
        todo_ref = f"todo:{plan.todo_id}:v{plan.todo_version}"
        stage_ref = f"stage:{plan.stage_id}:v{plan.plan_version}"
        approach_ref = f"approach:{plan.stage_id}:v{plan.plan_version}"
        preserve = [task_ref, todo_ref]
        invalidate = []
        if report:
            preserve.extend("evidence:" + ref for ref in report.retained_evidence)
            invalidate.extend("evidence:" + ref for ref in report.stale_evidence)
        if route == Route.REPLAN_TODO:
            preserve.remove(todo_ref)
            invalidate.extend((todo_ref, stage_ref))
        elif route == Route.REPLAN_STAGE:
            invalidate.append(stage_ref)
        elif route in (Route.REPAIR, Route.RETRY_CHECK, Route.ADVANCE, Route.WAIT) and layer not in (
                FailureLayer.STAGE_ASSUMPTION, FailureLayer.TODO_DECOMPOSITION):
            preserve.append(stage_ref)
        if change:
            invalidate.append(approach_ref)
        for diagnosis in verdict.diagnoses if layer in (FailureLayer.STAGE_ASSUMPTION, FailureLayer.TODO_DECOMPOSITION) else ():
            if diagnosis.layer == FailureLayer.STAGE_ASSUMPTION:
                invalidate.extend(f"assumption:{plan.stage_id}:v{plan.plan_version}:{plan.assumptions.index(a)}"
                                  for a in diagnosis.invalidated_assumptions)
        return FeedbackDecision(route=route, reason=reason, evidence_refs=baseline.evidence_refs, focus=focus,
            preserve=tuple(dict.fromkeys(preserve)), invalidate=tuple(dict.fromkeys(invalidate)),
            failure_layer=layer, change_strategy=change)

    def _baseline(self, state: RunState) -> FeedbackDecision:
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
        if any(d.invalidated_assumptions for d in verdict.diagnoses):
            return replan()  # 关闭实验策略也不能修复一个已证伪且仍被沿用的前提。
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
