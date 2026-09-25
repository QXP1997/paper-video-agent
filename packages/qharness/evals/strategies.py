"""仅用于评测的调度对照，复用原 Actor / Planner / Router / reducer。"""

import asyncio

from qharness.loop.feedback import FeedbackRouter
from qharness.loop.models import FeedbackDecision, Phase, Route, StartStage, StagePlan
from qharness.loop.planner import Planner, select_todo
from qharness.loop.repository import digest


VARIANTS = ("react", "fixed_todo", "full_replan", "base", "layered", "dynamic", "gaps",
            "layered_dynamic", "layered_gaps", "dynamic_gaps", "all")
DESCRIPTIONS = {
    "react": "单 Todo / Stage 的受约束 ReAct：只调用原 Actor，候选声明后由独立终态检查评分；不是外部 ReAct 论文复现。",
    "fixed_todo": "固定 Todo 和阶段目标，复用 Actor、三层验证和原反馈；不调用规划模型。",
    "full_replan": "每次未完成 Todo 的行动反馈后重新分解全部 Todo；环境错误仍走检查重试，已通过 Todo 不强制重开。",
    "base": "原三层流程，三项改进关闭。",
}


def configure(config, variant):
    if variant not in VARIANTS:
        raise ValueError("未知对照策略")
    enabled = {"layered", "dynamic", "gaps"} if variant == "all" else set(variant.split("_"))
    return type(config).model_validate({**config.model_dump(), "layered_feedback": "layered" in enabled,
        "dynamic_stage_planning": "dynamic" in enabled, "track_gap_progress": "gaps" in enabled})


class FixedPlanner(Planner):
    async def stage(self, state, **kwargs):
        if state.pending_decision and state.pending_decision.route == Route.REPAIR:
            return await super().stage(state, **kwargs)
        todo = select_todo(state)
        previous = state.attempts[-1].plan if state.attempts else None
        replan = state.pending_decision and state.pending_decision.route == Route.REPLAN_STAGE
        plan = StagePlan(stage_id=previous.stage_id if replan else f"fixed-{len(state.attempts)+1}",
            plan_version=previous.plan_version+1 if replan else 1, todo_id=todo.id, todo_version=todo.version,
            kind="implement", objective=todo.objective, addresses=todo.acceptance_refs,
            expected_results=todo.done_when, stop_when=("有可检查的候选实现",),
            replan_when=("任务前提无法满足",))
        return await asyncio.to_thread(self.repository.apply, StartStage(run_id=state.run_id,
            expected_version=state.version, plan=plan, attempt_id="eval-"+digest([state.run_id,state.version])[:32]))

    async def revise(self, state, **kwargs):
        # Fixed decomposition is part of this arm; never secretly switch to model planning.
        raise ValueError("固定计划对照不能调用全局重规划")


class ComparisonRouter(FeedbackRouter):
    def __init__(self, config, *, full_replan):
        super().__init__(config)
        self.full_replan = full_replan

    def decide(self, state):
        decision = super().decide(state)
        verdict = state.attempts[-1].verdicts[-1]
        if self.full_replan and verdict.todo_status != "pass" and decision.route not in (Route.WAIT, Route.TERMINATE, Route.RETRY_CHECK):
            route = Route.REPLAN_TODO if state.todo_plan.version-1 < self.config.max_todo_replans else Route.WAIT
            return FeedbackDecision(route=route, reason="评测对照：未完成反馈后全局重规划",
                                    evidence_refs=verdict.evidence_refs)
        if not self.full_replan and decision.route == Route.REPLAN_TODO:
            return FeedbackDecision(route=Route.WAIT, reason="固定分解对照已耗尽局部恢复路径", evidence_refs=verdict.evidence_refs)
        return decision


class ReactiveExecutor:
    """RunService 仍拥有锁和输入；候选只留在 VERIFYING，不伪造 Task PASS。"""
    def __init__(self, original, task):
        self.original, self.task = original, task

    async def _wait(self, reason):
        return await self.original._wait(reason)

    async def run(self, checks, **kwargs):
        state = await self.original._state()
        if state.phase == Phase.PLANNING:
            planner = FixedPlanner(self.original.actor.model, self.original.context)
            await planner.stage(state)
        state = await self.original._state()
        if state.phase == Phase.ACTING:
            await self.original.actor.run(context_observations=(*kwargs.get("observations", ()), checks.context()))
        return await self.original._state()


def install(services, task, variant):
    if variant == "react":
        # Frozen assembly, same underlying services; only orchestration adapter changes.
        from dataclasses import replace
        return replace(services, executor=ReactiveExecutor(services.executor, task))
    if variant == "fixed_todo":
        services.executor.planner = FixedPlanner(services.model, services.tools.context, progress=services.verifier.progress)
        services.executor.router = ComparisonRouter(services.executor.config, full_replan=False)
    elif variant == "full_replan":
        services.executor.router = ComparisonRouter(services.executor.config, full_replan=True)
    return services
