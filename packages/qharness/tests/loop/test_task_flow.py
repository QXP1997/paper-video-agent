import asyncio
import unittest

from qharness.loop import Phase, Resume, Route, TodoPlan, TodoPlanPatch
from qharness.loop.config import LoopConfig
from qharness.loop.executor import Executor
from qharness.loop.feedback import FeedbackRouter
from qharness.loop.repository import digest
from qharness.verification import CheckCatalog
from tests.support.task_flow import answer, catalog, flow_runtime, handed_back, payload, planned, successful_script, write
from tests.support.workspaces import pagination_run


class TaskFlowTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, script, **kwargs):
        runtime = flow_runtime(script, **kwargs)
        self.addCleanup(runtime.close)
        return runtime

    async def test_initial_todo_investigate_fail_repair_and_final_verify(self):
        r = self.runtime([answer(pagination_run().todo_plan), *successful_script()], initial_plan=False)
        state = await r.services.executor.run(catalog(), questions=r.state.questions)
        r.backend.assert_exhausted()
        self.assertEqual(state.phase, Phase.COMPLETED)
        self.assertEqual([a.decision.route for a in state.attempts],
                         [Route.ADVANCE, Route.REPAIR, Route.ADVANCE, Route.ADVANCE])
        self.assertEqual(state.attempts[1].plan, state.attempts[2].plan)
        self.assertNotEqual(state.attempts[1].attempt_id, state.attempts[2].attempt_id)
        self.assertEqual(state.attempts[0].verdicts[0].todo_status, "fail")
        self.assertEqual(state.attempts[0].verdicts[0].progress.resolved_questions[0].ref, "Q1")
        stage_plans = [p for p in map(payload, r.backend.requests) if p["output_schema"]["title"] == "StagePlan"]
        self.assertEqual(len(stage_plans), 3)
        self.assertIn("resolved_questions", str(stage_plans[1]["run_state"]["progress_history"]))
        self.assertIn("FAIL: test_boundary", str(stage_plans[1]["observations"]))
        repair_requests = [payload(req) for req in r.backend.requests if payload(req)["run_state"]
                           and payload(req)["run_state"]["active_attempt_id"] == state.attempts[2].attempt_id]
        self.assertIn("FAIL: test_boundary", str(repair_requests[0]["observations"]))
        self.assertEqual((r.context.workspace.root / "pagination.py").read_text(), "fixed")
        self.assertEqual((r.context.workspace.root / "validation.py").read_text(), "validated")

    async def test_task_integration_failure_reopens_todo_then_continues(self):
        r = self.runtime(successful_script(investigate=False, repair=False, integration_failure=True), integration_failure=True)
        state = await r.services.executor.run(catalog())
        r.backend.assert_exhausted()
        self.assertEqual(state.phase, Phase.COMPLETED)
        self.assertEqual([v.status for v in state.task_verdicts], ["fail", "pass"])
        self.assertEqual([a.plan.todo_id for a in state.attempts], ["T1", "T2", "T1"])
        self.assertEqual(state.task_verdicts[0].failed_criteria[0].ref, "C1")

    async def test_retry_check_does_not_repeat_actor_or_stage_planner(self):
        r = self.runtime(successful_script(investigate=False, repair=False))
        check = r.sandbox.on_execute
        first = True
        def temporarily_broken(request):
            nonlocal first
            check(request)
            if first:
                first = False
                r.sandbox.exit_code, r.sandbox.stdout = 2, "collection unavailable"
        r.sandbox.on_execute = temporarily_broken
        state = await r.services.executor.run(catalog())
        r.backend.assert_exhausted()
        self.assertEqual(state.phase, Phase.COMPLETED)
        self.assertEqual(len(state.attempts), 2)
        self.assertEqual(len(state.attempts[0].verdicts), 2)
        self.assertEqual(state.attempts[0].verdicts[0].stage_status, "error")
        self.assertEqual(r.repository.snapshot()["budget"]["tool_calls"], 10)

    async def test_persistent_check_error_waits_without_code_changes(self):
        r = self.runtime([planned(), handed_back()], config=LoopConfig(max_check_retries=1))
        def unavailable(_):
            r.sandbox.exit_code, r.sandbox.stdout = 127, "command not found"
        r.sandbox.on_execute = unavailable
        state = await r.services.executor.run(catalog())
        r.backend.assert_exhausted()
        self.assertEqual(state.phase, Phase.WAITING)
        self.assertEqual(len(state.attempts), 1)
        self.assertEqual(len(state.attempts[0].verdicts), 2)
        self.assertEqual((r.context.workspace.root / "pagination.py").read_text(), "wrong")

    async def test_missing_task_coverage_cannot_finish_and_retries_are_bounded(self):
        r = self.runtime(successful_script(investigate=False, repair=False), config=LoopConfig(max_check_retries=1))
        checks = CheckCatalog(tuple(s for s in catalog().specs if s.id != "task-input"))
        state = await r.services.executor.run(checks)
        r.backend.assert_exhausted()
        self.assertEqual(state.phase, Phase.WAITING)
        self.assertEqual(state.resume_phase, Phase.VERIFYING_TASK)
        self.assertEqual(len(state.task_verdicts), 2)
        self.assertTrue(all(t.status == "passed" for t in state.todos))

    async def test_replan_stage_preserves_id_and_increments_plan_version(self):
        r = self.runtime([planned(), handed_back(status="needs_replan", matched_replan_when=("关键前提改变",),
            reported_assumption_changes=("初始假设不成立",), matched_stop_when=()),
            *successful_script(investigate=False, repair=False)])
        state = await r.services.executor.run(catalog())
        r.backend.assert_exhausted()
        self.assertEqual(state.phase, Phase.COMPLETED)
        self.assertEqual(state.attempts[0].decision.route, Route.REPLAN_STAGE)
        self.assertEqual(state.attempts[0].plan.stage_id, state.attempts[1].plan.stage_id)
        self.assertEqual(state.attempts[1].plan.plan_version, 2)

    async def test_repair_limit_enters_investigation_instead_of_unbounded_repair(self):
        r = self.runtime([planned(), handed_back(), planned("investigate"), handed_back(),
                          *successful_script(investigate=False, repair=False)], config=LoopConfig(max_stage_repairs=0))
        state = await r.services.executor.run(catalog())
        r.backend.assert_exhausted()
        self.assertEqual(state.phase, Phase.COMPLETED)
        self.assertEqual(state.attempts[0].decision.route, Route.INVESTIGATE)
        self.assertEqual(state.attempts[1].plan.kind, "investigate")

    async def test_todo_patch_is_evidence_bound_and_preserves_obligations(self):
        def patch(request):
            data = payload(request)["run_state"]
            plan = TodoPlan.model_validate(data["todo_plan"])
            from qharness.loop import Todo
            old = plan.todos[0]
            replacement = Todo(id="T1-revised", objective="根据复现结果重新实施分页边界修复",
                acceptance_refs=old.acceptance_refs, done_when=old.done_when)
            revised = TodoPlan(version=plan.version + 1, todos=(replacement, *plan.todos[1:]))
            return answer(TodoPlanPatch(base_version=plan.version, plan=revised,
                reason="验证显示需要重新检查任务分解，保留当前完成义务",
                evidence_refs=tuple(data["pending_decision"]["evidence_refs"])))
        r = self.runtime([planned(), handed_back(status="needs_replan", matched_replan_when=("关键前提改变",),
            reported_assumption_changes=("需重新核对任务分解",), matched_stop_when=()), patch,
            *successful_script(investigate=False, repair=False)], config=LoopConfig(max_stage_replans=0))
        state = await r.services.executor.run(catalog())
        r.backend.assert_exhausted()
        self.assertEqual(state.phase, Phase.COMPLETED)
        self.assertEqual(state.todo_plan.version, 2)
        self.assertEqual(state.attempts[0].decision.route, Route.REPLAN_TODO)
        self.assertEqual(state.todo_plan.todos[0].id, "T1-revised")
        self.assertEqual(state.todo_plan.todos[1], r.state.todo_plan.todos[1])
        patches = [payload(req) for req in r.backend.requests if payload(req)["output_schema"]["title"] == "TodoPlanPatch"]
        self.assertEqual(len(patches), 1)
        stored = r.repository.read_artifact(digest(["todo-patch", patches[0]["run_state"]["version"]]))
        self.assertTrue(stored["reason"] and stored["evidence_refs"])

    async def test_blocked_actor_waits_before_running_checks(self):
        r = self.runtime([planned(), handed_back(status="blocked", summary="外部条件缺失")])
        state = await r.services.executor.run(catalog())
        r.backend.assert_exhausted()
        self.assertEqual(state.phase, Phase.WAITING)
        self.assertEqual(state.resume_phase, Phase.VERIFYING)
        self.assertFalse(r.sandbox.requests)
        self.assertFalse(state.attempts[0].verdicts)

    async def test_terminal_reentry_and_wait_reentry_do_not_consume_budget(self):
        r = self.runtime(successful_script(investigate=False, repair=False))
        first = await r.services.executor.run(catalog())
        budget = r.repository.snapshot()["budget"]
        second = await Executor(r.services.actor, r.services.verifier).run(catalog())
        self.assertEqual(first, second)
        self.assertEqual(budget, r.repository.snapshot()["budget"])
        r.backend.assert_exhausted()

    async def test_explicit_terminate_route_is_persisted(self):
        class StopRouter(FeedbackRouter):
            def decide(self, state):
                from qharness.loop import FeedbackDecision
                return FeedbackDecision(route="terminate", reason="应用明确终止")
        r = self.runtime([planned(), handed_back()])
        executor = Executor(r.services.actor, r.services.verifier, router=StopRouter(r.config))
        state = await executor.run(catalog())
        self.assertEqual(state.phase, Phase.TERMINATED)
        self.assertEqual(state.attempts[0].decision.route, Route.TERMINATE)

    async def test_stage_attempt_budget_survives_resume(self):
        r = self.runtime([planned("investigate"), handed_back()], config=LoopConfig(max_stage_attempts=1))
        state = await r.services.executor.run(catalog())
        self.assertEqual(state.phase, Phase.WAITING)
        budget = r.repository.snapshot()["budget"]
        r.repository.apply(Resume(run_id=state.run_id, expected_version=state.version))
        again = await Executor(r.services.actor, r.services.verifier).run(catalog())
        self.assertEqual(again.phase, Phase.WAITING)
        self.assertEqual(budget, r.repository.snapshot()["budget"])
        r.backend.assert_exhausted()

    async def test_forward_dependency_selects_ready_todo_instead_of_list_order(self):
        from qharness.loop import Todo, create_run
        original = pagination_run()
        t1, t2 = original.todo_plan.todos
        dependent = Todo.model_validate({**t2.model_dump(), "dependencies": ("T1",)})
        initial = create_run(original.run_id, original.contract, TodoPlan(todos=(dependent, t1)), questions=original.questions)
        r = self.runtime(successful_script(investigate=False, repair=False), state=initial)
        state = await r.services.executor.run(catalog())
        self.assertEqual(state.phase, Phase.COMPLETED)
        self.assertEqual([a.plan.todo_id for a in state.attempts], ["T1", "T2"])
        r.backend.assert_exhausted()

    async def test_cyclic_initial_plan_is_corrected_before_any_action(self):
        raw = pagination_run().todo_plan.model_dump(mode="json")
        raw["todos"][0]["dependencies"] = ["T2"]
        raw["todos"][1]["dependencies"] = ["T1"]
        r = self.runtime([answer(raw), answer(pagination_run().todo_plan),
            *successful_script(investigate=False, repair=False)], initial_plan=False)
        state = await r.services.executor.run(catalog())
        self.assertEqual(state.phase, Phase.COMPLETED)
        self.assertEqual(state.todo_plan.version, 1)
        self.assertEqual([payload(p)["output_schema"]["title"] for p in r.backend.requests[:2]], ["TodoPlan", "TodoPlan"])
        r.backend.assert_exhausted()

    async def test_forged_patch_evidence_cannot_replace_plan(self):
        def patch(request):
            raw = payload(request)["run_state"]["todo_plan"]
            raw["version"] += 1
            raw["todos"][0]["id"] = "T1-new"
            return answer({"base_version": 1, "plan": raw, "reason": "伪造引用", "evidence_refs": ["invented"]})
        r = self.runtime([planned(), handed_back(status="needs_replan", matched_replan_when=("关键前提改变",),
            reported_assumption_changes=("重新分解",), matched_stop_when=()), patch],
            config=LoopConfig(max_stage_replans=0, max_protocol_corrections=0))
        state = await r.services.executor.run(catalog())
        self.assertEqual(state.phase, Phase.WAITING)
        self.assertEqual(state.todo_plan, r.state.todo_plan)
        r.backend.assert_exhausted()

    async def test_unknown_check_blocks_remaining_checks_and_new_actor_attempts(self):
        r = self.runtime([planned(), handed_back()])
        async def slow(request):
            r.sandbox.requests.append(request)
            request.cancellation_event.set()  # 精确在实际派发后中断，不依赖磁盘准备耗时。
            await asyncio.sleep(5)
        r.sandbox.execute = slow
        state = await r.services.executor.run(catalog())
        self.assertEqual(state.phase, Phase.WAITING)
        self.assertEqual(len(r.sandbox.requests), 1)
        self.assertEqual(len(state.attempts), 1)
        self.assertTrue(r.repository.unresolved_calls())
        self.assertFalse(state.attempts[0].verdicts)
        r.backend.assert_exhausted()

    async def test_cancellation_during_planning_preserves_state_and_unknown_call(self):
        r = self.runtime([])
        started = asyncio.Event()
        async def slow(_):
            started.set()
            await asyncio.sleep(5)
        r.backend.complete = slow
        running = asyncio.create_task(r.services.executor.run(catalog()))
        await started.wait()
        r.context.cancel()
        state = await running
        self.assertEqual(state.phase, Phase.WAITING)
        self.assertEqual(state.resume_phase, Phase.PLANNING)
        self.assertFalse(state.attempts)
        self.assertTrue(r.repository.unresolved_calls())

    async def test_repair_reentry_uses_persisted_plan_without_planner_call(self):
        from qharness.loop import StagePlan
        from tests.loop.test_transitions import begin, feedback, outcome, verdict
        from tests.support.workspaces import stage
        plan = StagePlan.model_validate({**stage(kind="implement").model_dump(), "expected_results": ("分页结果正确",)})
        initial = feedback(verdict(outcome(begin(plan=plan)), stage_status="fail"), Route.REPAIR)
        r = self.runtime([write("pagination.py", "fixed"), handed_back(), planned(),
                          write("validation.py", "validated"), handed_back()], state=initial)
        state = await r.services.executor.run(catalog())
        self.assertEqual(state.phase, Phase.COMPLETED)
        self.assertEqual(state.attempts[0].plan, state.attempts[1].plan)
        self.assertEqual(payload(r.backend.requests[0])["output_schema"]["title"], "StageOutcome")
        r.backend.assert_exhausted()
