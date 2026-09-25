import itertools
from dataclasses import asdict
import unittest

from qharness.exception import LoopTransitionError
from qharness.loop import ApplyFeedback, FailureDiagnosis, FeedbackDecision, Phase, ProgressReport, ReplaceTodoPlan, Resume, Route, StageKind, Todo, TodoPlan
from qharness.loop.config import LoopConfig
from qharness.loop.feedback import FeedbackRouter
from qharness.loop.repository import digest
from qharness.verification import CheckCatalog, CheckSpec, DiagnosisRule, QuestionConclusion
from tests.loop.test_transitions import begin, outcome, send, verdict
from tests.support.task_flow import catalog, flow_runtime, guided_plan, handed_back, reasoning_catalog, write
from tests.support.workspaces import pagination_run, stage


def changed(model, **changes):
    return type(model).model_validate({**model.model_dump(), **changes})


def configured(**changes):
    return LoopConfig(layered_feedback=True, dynamic_stage_planning=True, track_gap_progress=True,
                      retry_delay_seconds=0, **changes)


def clear_state():
    return changed(pagination_run(), questions=())


class ReasoningTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, script, **kwargs):
        r = flow_runtime(script, **kwargs)
        self.addCleanup(r.close)
        return r

    async def test_guided_investigate_then_local_repair_preserves_exact_plan(self):
        r = self.runtime([guided_plan(), handed_back(), guided_plan(), write("pagination.py", "still wrong"),
            handed_back(), write("pagination.py", "fixed"), handed_back(), guided_plan(),
            write("validation.py", "validated"), handed_back()], config=configured())
        state = await r.services.executor.run(reasoning_catalog())
        r.backend.assert_exhausted()
        self.assertEqual(state.phase, Phase.COMPLETED)
        self.assertEqual([a.plan.kind for a in state.attempts], ["investigate", "implement", "implement", "implement"])
        first, failed, repaired = state.attempts[:3]
        self.assertEqual(first.verdicts[0].progress_report.novel_refs, ("Q1",))
        self.assertNotEqual(first.verdicts[0].todo_status, "pass")
        self.assertEqual(failed.decision.failure_layer, "local_action")
        self.assertEqual(failed.decision.route, Route.REPAIR)
        self.assertEqual(failed.plan, repaired.plan)
        self.assertIn("stage:S2:v1", failed.decision.preserve)
        self.assertTrue(state.task_verdicts)

    async def test_eight_flag_combinations_preserve_completion_and_local_repair(self):
        for layered, dynamic, gaps in itertools.product((False, True), repeat=3):
            with self.subTest(layered=layered, dynamic=dynamic, gaps=gaps):
                config = LoopConfig(layered_feedback=layered, dynamic_stage_planning=dynamic, track_gap_progress=gaps)
                r = self.runtime([guided_plan(), handed_back(), write("pagination.py", "fixed"), handed_back(),
                                  guided_plan(), write("validation.py", "validated"), handed_back()],
                                 config=config, state=clear_state())
                result = await r.services.executor.run(reasoning_catalog())
                r.backend.assert_exhausted()
                self.assertEqual(result.phase, Phase.COMPLETED)
                self.assertEqual(result.attempts[0].decision.route, Route.REPAIR)
                self.assertEqual(result.contract, r.state.contract)
                self.assertEqual(result.task_verdicts[-1].status, "pass")

    async def test_clear_todo_finishes_in_one_stage_without_forced_investigation(self):
        r = self.runtime([guided_plan(), write("pagination.py", "fixed"), handed_back(),
                          guided_plan(), write("validation.py", "validated"), handed_back()],
                         config=configured(), state=clear_state())
        result = await r.services.executor.run(reasoning_catalog())
        self.assertEqual(result.phase, Phase.COMPLETED)
        self.assertEqual([(a.plan.todo_id, a.plan.kind) for a in result.attempts], [("T1", "implement"), ("T2", "implement")])

    async def test_missing_final_evidence_still_waits_with_all_strategies(self):
        r = self.runtime([guided_plan(), write("pagination.py", "fixed"), handed_back(),
                          guided_plan(), write("validation.py", "validated"), handed_back()],
                         config=configured(max_check_retries=0), state=clear_state())
        checks = CheckCatalog(tuple(s for s in reasoning_catalog().specs if s.id != "task-input"))
        result = await r.services.executor.run(checks)
        self.assertEqual(result.phase, Phase.WAITING)
        self.assertEqual(result.resume_phase, Phase.VERIFYING_TASK)

    async def test_unknown_failure_creates_question_and_investigates(self):
        checks = CheckCatalog(tuple(changed(s, on_failure=None) for s in reasoning_catalog().specs))
        r = self.runtime([guided_plan(), handed_back()], config=configured(max_stage_attempts=1), state=clear_state())
        result = await r.services.executor.run(checks)
        self.assertEqual(result.attempts[0].decision.route, Route.INVESTIGATE)
        self.assertEqual(result.attempts[0].decision.failure_layer, "unknown")
        self.assertEqual(len(result.questions), 1)
        self.assertEqual(result.attempts[0].decision.focus, (result.questions[0].id,))

    async def test_actual_disproved_assumption_replans_and_rejects_preserving_it(self):
        rule = DiagnosisRule(layer="stage_assumption", summary="检查证明边界前提不成立",
                             invalidated_assumptions=("边界已经正确",))
        checks = CheckCatalog(tuple(changed(s, on_failure=rule) if s.id == "stage-boundary" else s
                                    for s in reasoning_catalog().specs))
        r = self.runtime([guided_plan(assumptions=("边界已经正确",)), handed_back(),
                          guided_plan(assumptions=("边界已经正确",))],
                         config=configured(max_protocol_corrections=0), state=clear_state())
        result = await r.services.executor.run(checks)
        self.assertEqual(result.phase, Phase.WAITING)
        decision = result.attempts[0].decision
        self.assertEqual(decision.route, Route.REPLAN_STAGE)
        self.assertIn("assumption:S1:v1:0", decision.invalidate)
        self.assertIn("task:" + result.contract.task_id + ":v1", decision.preserve)
        self.assertEqual(len(result.attempts), 1)

    async def test_dynamic_kind_and_unrelated_sources_rejected_before_actor(self):
        for proposal in (guided_plan(kind="implement"), guided_plan(source="task-boundary"),
                         guided_plan(addresses=("C1",))):
            with self.subTest(proposal=proposal):
                r = self.runtime([proposal], config=configured(max_protocol_corrections=0))
                result = await r.services.executor.run(reasoning_catalog())
                self.assertEqual(result.phase, Phase.WAITING)
                self.assertFalse(result.attempts)
                self.assertFalse(r.sandbox.requests)

    async def test_validation_stage_selected_when_only_done_when_remains(self):
        checks = []
        for spec in reasoning_catalog().specs:
            if spec.id == "todo-boundary":
                checks.append(changed(spec, targets=("C1",)))
            else:
                checks.append(spec)
        r = self.runtime([guided_plan(), write("pagination.py", "fixed"), handed_back(),
                          guided_plan(), handed_back()], config=configured(max_stage_attempts=2), state=clear_state())
        result = await r.services.executor.run(CheckCatalog(tuple(checks)))
        self.assertEqual(result.phase, Phase.WAITING)
        self.assertEqual([a.plan.kind for a in result.attempts], ["implement", "validate"])
        self.assertTrue(result.satisfied_criteria)
        self.assertNotEqual(result.todos[0].status, "passed")

    async def test_repeated_elimination_is_not_new_progress_and_requires_new_source(self):
        conclusion = QuestionConclusion(question_id="Q1", kind="eliminated", finding="排除了输入校验", fact_id="not-input")
        original = next(s for s in reasoning_catalog().specs if s.id == "investigate")
        original = changed(original, conclusions=(conclusion,))
        alias = changed(original, id="renamed", targets=("换个措辞的调查目标",))
        checks = CheckCatalog(tuple(s for s in reasoning_catalog().specs if s.id != "investigate") + (original, alias))
        script = [item for _ in range(3) for item in (guided_plan(source="investigate"), handed_back())]
        script.append(guided_plan(source="renamed", target="换个措辞的调查目标"))
        r = self.runtime(script, config=configured(max_protocol_corrections=0))
        result = await r.services.executor.run(checks)
        r.backend.assert_exhausted()
        self.assertEqual(result.phase, Phase.WAITING)
        self.assertEqual(len(result.attempts), 3)
        reports = [a.verdicts[-1].progress_report for a in result.attempts]
        self.assertEqual([v.stalled_investigations for v in reports], [0, 1, 2])
        self.assertEqual([v.novel_refs for v in reports], [("Q1",), (), ()])
        self.assertEqual(len(reports[-1].retained_findings), 1)
        self.assertTrue(result.attempts[-1].decision.change_strategy)
        self.assertEqual(result.attempts[-1].decision.route, Route.REPLAN_STAGE)

    async def test_new_source_with_new_elimination_restores_progress(self):
        original = next(s for s in reasoning_catalog().specs if s.id == "investigate")
        original = changed(original, conclusions=(QuestionConclusion(question_id="Q1", kind="eliminated",
                          finding="排除了输入校验", fact_id="not-input"),))
        alternative = changed(original, id="alternative", inputs=("pagination.py", "validation.py"),
            conclusions=(QuestionConclusion(question_id="Q1", kind="narrowed", finding="范围缩小到偏移计算", fact_id="offset"),))
        checks = CheckCatalog(tuple(s for s in reasoning_catalog().specs if s.id != "investigate") + (original, alternative))
        script = [item for _ in range(3) for item in (guided_plan(source="investigate"), handed_back())]
        script += [guided_plan(source="alternative"), handed_back()]
        r = self.runtime(script, config=configured(max_stage_attempts=4))
        result = await r.services.executor.run(checks)
        r.backend.assert_exhausted()
        self.assertEqual(len(result.attempts), 4)
        report = result.attempts[-1].verdicts[-1].progress_report
        self.assertEqual(report.stalled_investigations, 0)
        self.assertEqual(report.novel_refs, ("Q1",))
        self.assertEqual(len(report.retained_findings), 2)
        self.assertEqual(result.attempts[-1].plan.plan_version, 2)

    async def test_file_and_catalog_changes_invalidate_investigation_knowledge(self):
        r = self.runtime([guided_plan(), handed_back()], config=configured(max_stage_attempts=1))
        state = await r.services.executor.run(reasoning_catalog())
        tracker = r.services.verifier.progress
        valid = await tracker.view(state, "T1", specs=reasoning_catalog().specs)
        self.assertFalse(valid.unresolved_questions)
        removed = await tracker.view(state, "T1", specs=tuple(s for s in reasoning_catalog().specs if s.id != "investigate"))
        self.assertEqual(removed.unresolved_questions, ("Q1",))
        (r.context.workspace.root / "pagination.py").write_text("changed", encoding="utf-8")
        stale = await tracker.view(state, "T1", specs=reasoning_catalog().specs)
        self.assertEqual(stale.unresolved_questions, ("Q1",))
        self.assertFalse(stale.retained_findings)
        self.assertTrue(stale.stale_evidence)

    async def test_check_environment_error_retries_without_actor_or_stall(self):
        r = self.runtime([guided_plan(), handed_back()], config=configured(max_check_retries=1))
        def unavailable(_):
            r.sandbox.exit_code, r.sandbox.stdout = 127, "unavailable"
        r.sandbox.on_execute = unavailable
        result = await r.services.executor.run(reasoning_catalog())
        r.backend.assert_exhausted()
        self.assertEqual(result.phase, Phase.WAITING)
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(len(result.attempts[0].verdicts), 2)
        self.assertEqual(result.attempts[0].decision.failure_layer, "check_environment")
        self.assertEqual(result.attempts[0].verdicts[-1].progress_report.stalled_investigations, 0)

    async def test_knowledge_survives_replacing_todo_when_evidence_remains_valid(self):
        r = self.runtime([guided_plan(), handed_back()], config=configured(max_stage_attempts=1))
        state = await r.services.executor.run(reasoning_catalog())
        state = send(state, Resume)
        state = changed(state, pending_decision=FeedbackDecision(route="replan_todo", reason="重新分解"))
        old = state.todo_plan.todos[0]
        replacement = Todo(id="T1-new", objective=old.objective, acceptance_refs=old.acceptance_refs, done_when=old.done_when)
        state = send(state, ReplaceTodoPlan, plan=TodoPlan(version=2, todos=(replacement, state.todo_plan.todos[1])))
        report = await r.services.verifier.progress.view(state, "T1-new", specs=reasoning_catalog().specs)
        self.assertFalse(report.unresolved_questions)
        self.assertEqual(report.retained_findings[0].ref, "Q1")
        self.assertEqual(report.unresolved_criteria, ("C1",))

    async def test_environment_change_reopens_resolved_question(self):
        r = self.runtime([guided_plan(), handed_back()], config=configured(max_stage_attempts=1))
        state = await r.services.executor.run(reasoning_catalog())
        r.context.metadata["verification_environment"] = "new-runtime-version"
        report = await r.services.verifier.progress.view(state, "T1")
        self.assertEqual(report.unresolved_questions, ("Q1",))
        self.assertFalse(report.retained_evidence)

    async def test_later_failed_check_retracts_earlier_question_conclusion(self):
        r = self.runtime([guided_plan(kind="investigate"), handed_back(), guided_plan(kind="investigate"), handed_back()],
                         config=LoopConfig(layered_feedback=True, max_stage_attempts=2))
        check, count = r.sandbox.on_execute, 0
        def fail_later(request):
            nonlocal count
            check(request)
            if request.command == "investigate":
                count += 1
                if count == 2:
                    r.sandbox.exit_code, r.sandbox.stdout = 1, "FAIL: investigate\nRan 1 test in 0.01s\nFAILED (failures=1)\n"
        r.sandbox.on_execute = fail_later
        result = await r.services.executor.run(reasoning_catalog())
        report = result.attempts[-1].verdicts[-1].progress_report
        self.assertIn("Q1", report.unresolved_questions)
        self.assertFalse(report.retained_findings)

    async def test_inflight_attempt_does_not_increment_investigation_stall(self):
        original = next(s for s in reasoning_catalog().specs if s.id == "investigate")
        empty = changed(original, conclusions=(), addresses=("Q1",))
        checks = CheckCatalog(tuple(s for s in reasoning_catalog().specs if s.id != "investigate") + (empty,))
        r = self.runtime([guided_plan(), handed_back(), guided_plan()], config=configured(max_stage_attempts=1))
        state = await r.services.executor.run(checks)
        self.assertEqual(state.attempts[-1].verdicts[-1].progress_report.stalled_investigations, 1)
        state = r.repository.apply(Resume(run_id=state.run_id, expected_version=state.version))
        active = await r.services.executor.planner.stage(state, checks=checks)
        report = await r.services.verifier.progress.view(active, "T1", specs=checks.specs)
        self.assertEqual(active.phase, Phase.ACTING)
        self.assertEqual(report.stalled_investigations, 1)

    def test_old_check_definition_hash_does_not_change_for_absent_metadata(self):
        r = self.runtime([])
        spec = catalog().specs[0]
        old = spec.model_dump(mode="json")
        old.pop("addresses")
        old.pop("on_failure")
        for conclusion in old["conclusions"]:
            conclusion.pop("fact_id")
        definition = asdict(r.registry.get("run_command").to_definition())
        self.assertEqual(r.services.verifier.runner.definition_hash(spec), digest(["check-parser-v1", old, definition]))

    async def test_shared_tool_budget_remains_binding_with_all_flags(self):
        r = self.runtime([guided_plan(), write("pagination.py", "fixed"), handed_back()],
                         config=configured(max_tool_executions=1), state=clear_state())
        result = await r.services.executor.run(reasoning_catalog())
        self.assertEqual(result.phase, Phase.WAITING)
        self.assertFalse(result.task_verdicts)
        self.assertEqual(r.repository.snapshot()["budget"]["tool_executions"], 1)
        self.assertFalse(r.sandbox.requests)


class FeedbackScopeTests(unittest.TestCase):
    def failed(self, **changes):
        return verdict(outcome(begin(plan=stage(kind=StageKind.IMPLEMENT))), stage_status="fail", **changes)

    def test_trusted_decomposition_routes_only_affected_todo(self):
        state = self.failed(diagnoses=(FailureDiagnosis(layer="todo_decomposition", summary="分解与证据冲突",
                                                       evidence_refs=("E-observation",)),))
        decision = FeedbackRouter(configured()).decide(state)
        self.assertEqual(decision.route, Route.REPLAN_TODO)
        self.assertIn("todo:T1:v1", decision.invalidate)
        result = send(state, ApplyFeedback, decision=decision)
        self.assertEqual(result.contract, state.contract)

    def test_model_assumption_report_only_authorizes_investigation(self):
        state = verdict(outcome(begin(plan=stage(kind=StageKind.IMPLEMENT)),
                        reported_assumption_changes=("模型声称分解错了",)), stage_status="fail")
        decision = FeedbackRouter(configured()).decide(state)
        self.assertEqual(decision.route, Route.INVESTIGATE)
        self.assertEqual(decision.failure_layer, "unknown")
        self.assertNotIn("todo:T1:v1", decision.invalidate)

    def test_unknown_diagnosis_does_not_authorize_blind_repair(self):
        decision = FeedbackRouter(configured()).decide(self.failed())
        self.assertEqual(decision.route, Route.INVESTIGATE)
        baseline = FeedbackRouter(LoopConfig()).decide(self.failed())
        self.assertEqual(baseline.route, Route.REPAIR)

    def test_feedback_cannot_invalidate_contract_or_unrelated_todo(self):
        state = self.failed()
        for invalid in ((f"task:{state.contract.task_id}:v1",), ("todo:T2:v1",), ("evidence:invented",)):
            with self.subTest(invalid=invalid), self.assertRaises(LoopTransitionError):
                send(state, ApplyFeedback, decision=FeedbackDecision(route="replan_todo", reason="错误范围", invalidate=invalid))

    def test_diagnosis_cannot_reference_unrecorded_evidence(self):
        with self.assertRaises(LoopTransitionError):
            self.failed(diagnoses=(FailureDiagnosis(layer="local_action", summary="伪造诊断", evidence_refs=("invented",)),))

    def test_old_config_keeps_baseline_and_switches_are_independent(self):
        self.assertFalse(LoopConfig().layered_feedback)
        self.assertFalse(LoopConfig().dynamic_stage_planning)
        self.assertFalse(LoopConfig().track_gap_progress)
        self.assertEqual(FeedbackRouter(LoopConfig(dynamic_stage_planning=True)).decide(self.failed()).route, Route.REPAIR)

    def test_gap_stall_switch_changes_routing_independently(self):
        state = verdict(outcome(begin()), progress_report=ProgressReport(todo_id="T1", unresolved_criteria=("C1",),
                        unresolved_questions=("Q1",), stalled_investigations=2))
        self.assertEqual(FeedbackRouter(LoopConfig()).decide(state).route, Route.ADVANCE)
        decision = FeedbackRouter(LoopConfig(track_gap_progress=True)).decide(state)
        self.assertEqual(decision.route, Route.REPLAN_STAGE)
        self.assertTrue(decision.change_strategy)


if __name__ == "__main__":
    unittest.main()
