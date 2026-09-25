import unittest

from tests.support.workspaces import pagination_run, stage
from qharness.exception import LoopTransitionError
from qharness.loop import (
    ApplyFeedback, CheckStatus, EvidenceLink, FeedbackDecision, Phase, ProgressDelta,
    Question, QuestionFinding, RecordStageVerdict, RecordTaskVerdict, ReopenTodos,
    ReplaceTodoPlan, Resume, Route, RunState, StageKind, StageOutcome, StagePlan,
    StageVerdict, StartStage, SubmitOutcome, TaskVerdict, Terminate, Todo, TodoPlan,
    TodoStatus, Wait, create_run, reduce,
)


def send(state, event_type, **payload):
    return reduce(state, event_type(run_id=state.run_id, expected_version=state.version, **payload))


def begin(state=None, plan=None, attempt_id="A1"):
    return send(state or pagination_run(), StartStage, plan=plan or stage(), attempt_id=attempt_id)


def outcome(state, **changes):
    return send(state, SubmitOutcome, outcome=StageOutcome(**{
        **state.attempts[-1].identity.model_dump(), "status": "candidate", "summary": "阶段结果已产出", **changes,
    }))


def verdict(state, **changes):
    return send(state, RecordStageVerdict, verdict=StageVerdict(**{
        **state.attempts[-1].identity.model_dump(), "stage_status": "pass", "todo_status": "fail",
        "expected_vs_observed": "已找到原因，修复尚未完成", "evidence_refs": ("E-observation",), **changes,
    }))


def feedback(state, route):
    return send(state, ApplyFeedback, decision=FeedbackDecision(route=route, reason="依据当前验证结果"))


def complete_todo(state, todo_id, stage_id):
    todo = next(t.todo for t in state.todos if t.todo.id == todo_id)
    plan = stage(todo_id, stage_id, kind=StageKind.VALIDATE, todo_version=todo.version)
    state = begin(state, plan, attempt_id=f"A-{stage_id}")
    state = outcome(state)
    state = verdict(state, todo_status="pass", expected_vs_observed="Todo 所有检查通过",
                    progress=ProgressDelta(satisfied_criteria=tuple(
                        EvidenceLink(ref=ref, evidence_refs=(f"E-{ref}",)) for ref in todo.acceptance_refs
                    )))
    return feedback(state, Route.ADVANCE)


def all_todos_passed():
    return complete_todo(complete_todo(pagination_run(), "T1", "S1"), "T2", "S2")


class TransitionTests(unittest.TestCase):
    def test_investigation_pass_does_not_complete_todo_and_records_actual_progress(self):
        before = begin()
        serialized = before.model_dump_json()
        state = verdict(outcome(before), progress=ProgressDelta(resolved_questions=(
            QuestionFinding(ref="Q1", finding="偏移量确实多加了一页", evidence_refs=("E-repro",)),
        )), remaining_gaps=("C1",))
        state = feedback(state, Route.ADVANCE)
        self.assertEqual(state.phase, Phase.PLANNING)
        self.assertEqual(state.todos[0].status, TodoStatus.PENDING)
        self.assertFalse(state.satisfied_criteria)
        self.assertEqual(state.progress_history[0].resolved_questions[0].ref, "Q1")
        self.assertEqual(before.model_dump_json(), serialized)
        # 当前 Todo 未完成，不能悄悄跳去处理无关 Todo。
        with self.assertRaises(LoopTransitionError):
            begin(state, stage("T2", "S2", kind=StageKind.IMPLEMENT), "A2")
        continued = begin(state, stage(stage_id="S2", kind=StageKind.IMPLEMENT), "A2")
        self.assertEqual(continued.attempts[-1].plan.todo_id, "T1")

    def test_todo_pass_leaves_run_open_for_other_obligations(self):
        state = complete_todo(pagination_run(), "T1", "S1")
        self.assertEqual(state.todos[0].status, TodoStatus.PASSED)
        self.assertEqual(state.todos[1].status, TodoStatus.PENDING)
        self.assertEqual(state.phase, Phase.PLANNING)
        with self.assertRaises(LoopTransitionError):
            send(state, RecordTaskVerdict, verdict=TaskVerdict(
                contract_version=1, status="pass", summary="过早结束",
                criterion_evidence=(EvidenceLink(ref="C1", evidence_refs=("E",)),),
            ))

    def test_only_complete_task_evidence_finishes_run(self):
        state = all_todos_passed()
        self.assertEqual(state.phase, Phase.VERIFYING_TASK)
        incomplete = TaskVerdict(contract_version=1, status="pass", summary="遗漏输入校验",
                                 criterion_evidence=(EvidenceLink(ref="C1", evidence_refs=("E",)),))
        with self.assertRaises(LoopTransitionError):
            send(state, RecordTaskVerdict, verdict=incomplete)
        passed = TaskVerdict(contract_version=1, status="pass", summary="最终集成验收通过",
                             criterion_evidence=tuple(EvidenceLink(ref=ref, evidence_refs=("E-final",))
                                                      for ref in ("C1", "C2")))
        state = send(state, RecordTaskVerdict, verdict=passed)
        self.assertEqual(state.phase, Phase.COMPLETED)
        self.assertEqual(RunState.model_validate_json(state.model_dump_json()), state)
        with self.assertRaises(LoopTransitionError):
            send(state, ReopenTodos, todo_ids=("T1",), reason="终态需新运行")

    def test_repair_reuses_plan_but_has_new_attempt_and_keeps_history(self):
        state = feedback(verdict(outcome(begin()), stage_status="fail"), Route.REPAIR)
        old = state.attempts[-1]
        changed = StagePlan.model_validate({**old.plan.model_dump(), "expected_results": ("只检查第一页",)})
        with self.assertRaises(LoopTransitionError):
            begin(state, changed, "A2")
        with self.assertRaises(LoopTransitionError):
            begin(state, old.plan, "A1")
        state = begin(state, old.plan, "A2")
        self.assertEqual(state.attempts[-1].plan, old.plan)
        self.assertEqual(state.attempts[-1].attempt_number, 2)
        self.assertEqual(state.attempts[0], old)

    def test_repair_cannot_preserve_disproved_assumption(self):
        state = verdict(outcome(begin(), reported_assumption_changes=("不是偏移量问题",)), stage_status="fail")
        with self.assertRaises(LoopTransitionError):
            feedback(state, Route.REPAIR)

    def test_stage_replan_versions_plan_and_preserves_todo_contract(self):
        state = feedback(verdict(outcome(begin()), stage_status="fail"), Route.REPLAN_STAGE)
        with self.assertRaises(LoopTransitionError):
            begin(state, stage(), "A2")
        revised = stage(version=2, kind=StageKind.IMPLEMENT)
        result = begin(state, revised, "A2")
        self.assertEqual(result.todo_plan, state.todo_plan)
        self.assertEqual(result.contract, state.contract)
        self.assertEqual(result.attempts[-1].plan.plan_version, 2)

    def test_investigate_requires_new_investigation_stage_for_same_todo(self):
        state = feedback(verdict(outcome(begin()), stage_status="inconclusive"), Route.INVESTIGATE)
        with self.assertRaises(LoopTransitionError):
            begin(state, stage(stage_id="S2", kind=StageKind.IMPLEMENT), "A2")
        result = begin(state, stage(stage_id="S2"), "A2")
        self.assertEqual(len(result.attempts), 2)

    def test_retry_check_does_not_reexecute_actor_or_create_attempt(self):
        state = feedback(verdict(outcome(begin()), stage_status="error", todo_status="inconclusive"), Route.RETRY_CHECK)
        self.assertEqual(state.phase, Phase.VERIFYING)
        self.assertEqual(len(state.attempts), 1)
        with self.assertRaises(LoopTransitionError):
            outcome(state)
        state = verdict(state)
        self.assertEqual(len(state.attempts[0].verdicts), 2)
        self.assertEqual(state.attempts[0].attempt_number, 1)

    def test_failed_stage_cannot_advance_or_claim_todo_pass_without_evidence(self):
        checking = outcome(begin())
        with self.assertRaises(LoopTransitionError):
            verdict(checking, todo_status="pass")
        state = verdict(checking, stage_status="fail")
        with self.assertRaises(LoopTransitionError):
            feedback(state, Route.ADVANCE)
        with self.assertRaises(LoopTransitionError):
            feedback(state, Route.RETRY_CHECK)

    def test_todo_replan_preserves_unchanged_completed_work(self):
        state = complete_todo(pagination_run(), "T1", "S1")
        state = begin(state, stage("T2", "S2", kind=StageKind.IMPLEMENT), "A2")
        state = feedback(verdict(outcome(state), stage_status="fail"), Route.REPLAN_TODO)
        old_t1, old_t2 = state.todo_plan.todos
        revised_t2 = Todo.model_validate({**old_t2.model_dump(), "version": 2, "dependencies": ("T1",)})
        revised_plan = TodoPlan(version=2, todos=(old_t1, revised_t2))
        state = send(state, ReplaceTodoPlan, plan=revised_plan)
        self.assertEqual(state.todos[0].status, TodoStatus.PASSED)
        self.assertEqual(state.todos[1].status, TodoStatus.PENDING)
        self.assertEqual(state.contract, pagination_run().contract)
        self.assertEqual([e.ref for e in state.satisfied_criteria], ["C1"])
        state = begin(state, stage("T2", "S3", todo_version=2, kind=StageKind.IMPLEMENT), "A3")
        self.assertEqual(state.phase, Phase.ACTING)

    def test_todo_replan_rejects_weakening_obligations_and_unrouted_patch(self):
        state = feedback(verdict(outcome(begin()), stage_status="fail"), Route.REPLAN_TODO)
        todo1, todo2 = state.todo_plan.todos
        weakened = Todo.model_validate({**todo1.model_dump(), "version": 2, "done_when": ("随便运行一次",)})
        for plan in (TodoPlan(version=2, todos=(weakened, todo2)),
                     TodoPlan(version=2, todos=(todo1,)),
                     TodoPlan(version=1, todos=(todo1, todo2))):
            with self.subTest(plan=plan), self.assertRaises(LoopTransitionError):
                send(state, ReplaceTodoPlan, plan=plan)
        with self.assertRaises(LoopTransitionError):
            send(pagination_run(), ReplaceTodoPlan, plan=TodoPlan(version=2, todos=(todo1, todo2)))
        with self.assertRaises(LoopTransitionError):
            begin(state, stage(stage_id="S2"), "A2")

    def test_todo_redecomposition_can_replace_ids_but_keeps_original_task(self):
        state = feedback(verdict(outcome(begin()), stage_status="fail"), Route.REPLAN_TODO)
        old1, old2 = state.todo_plan.todos
        replacement = Todo.model_validate({**old1.model_dump(), "id": "T3"})
        state = send(state, ReplaceTodoPlan, plan=TodoPlan(version=2, todos=(replacement, old2)))
        self.assertEqual([t.todo.id for t in state.todos], ["T3", "T2"])
        self.assertEqual(state.contract, pagination_run().contract)

    def test_changed_completed_todo_dependencies_invalidate_its_old_pass(self):
        state = complete_todo(pagination_run(), "T1", "S1")
        state = feedback(verdict(outcome(begin(
            state, stage("T2", "S2", kind=StageKind.IMPLEMENT), "A2",
        )), stage_status="fail"), Route.REPLAN_TODO)
        t1, t2 = state.todo_plan.todos
        changed = Todo.model_validate({**t1.model_dump(), "version": 2, "dependencies": ("T2",)})
        state = send(state, ReplaceTodoPlan, plan=TodoPlan(version=2, todos=(changed, t2)))
        self.assertEqual(state.todos[0].status, TodoStatus.PENDING)
        self.assertFalse(state.satisfied_criteria)

    def test_shared_acceptance_evidence_is_conservatively_invalidated(self):
        original = pagination_run()
        t1, t2 = original.todo_plan.todos
        shared = Todo.model_validate({**t2.model_dump(), "acceptance_refs": ("C1", "C2")})
        state = create_run(original.run_id, original.contract, TodoPlan(todos=(t1, shared)))
        state = complete_todo(complete_todo(state, "T1", "S1"), "T2", "S2")
        state = send(state, ReopenTodos, todo_ids=("T1",), reason="共享的 C1 证据失效")
        self.assertEqual([t.status for t in state.todos], [TodoStatus.PENDING, TodoStatus.PENDING])
        self.assertFalse(state.satisfied_criteria)

    def test_reopen_invalidates_transitive_dependents_and_evidence(self):
        original = pagination_run()
        t1, t2 = original.todo_plan.todos
        dependent = Todo.model_validate({**t2.model_dump(), "dependencies": ("T1",)})
        state = create_run(original.run_id, original.contract, TodoPlan(todos=(t1, dependent)))
        with self.assertRaises(LoopTransitionError):
            begin(state, stage("T2", "S2", kind=StageKind.VALIDATE), "A2")
        state = complete_todo(complete_todo(state, "T1", "S1"), "T2", "S2")
        state = send(state, ReopenTodos, todo_ids=("T1",), reason="最终集成发现旧结论失效")
        self.assertEqual([t.status for t in state.todos], [TodoStatus.PENDING, TodoStatus.PENDING])
        self.assertFalse(state.satisfied_criteria)
        self.assertTrue(all(not t.evidence_refs for t in state.todos))
        self.assertEqual(len(state.attempts), 2)

    def test_task_failure_reopens_relevant_todos_without_erasing_unrelated_pass(self):
        state = send(all_todos_passed(), RecordTaskVerdict, verdict=TaskVerdict(
            contract_version=1, status="fail", summary="集成时非法输入仍未拒绝",
            failed_criteria=(EvidenceLink(ref="C2", evidence_refs=("E-integration",)),),
        ))
        self.assertEqual(state.phase, Phase.PLANNING)
        self.assertEqual([t.status for t in state.todos], [TodoStatus.PASSED, TodoStatus.PENDING])
        self.assertEqual([e.ref for e in state.satisfied_criteria], ["C1"])

    def test_task_check_error_keeps_todos_passed_and_allows_wait(self):
        state = send(all_todos_passed(), RecordTaskVerdict, verdict=TaskVerdict(
            contract_version=1, status="error", summary="检查环境不可用",
        ))
        self.assertEqual(state.phase, Phase.VERIFYING_TASK)
        self.assertTrue(all(t.status == TodoStatus.PASSED for t in state.todos))
        waiting = send(state, Wait, reason="等待检查环境恢复")
        resumed = send(waiting, Resume)
        self.assertEqual(resumed.phase, state.phase)
        self.assertEqual(resumed.todos, state.todos)

    def test_wait_resume_preserves_exact_attempt_and_rejects_execution_while_waiting(self):
        state = begin()
        waiting = send(state, Wait, reason="等待用户提供必要输入")
        with self.assertRaises(LoopTransitionError):
            outcome(waiting)
        resumed = send(waiting, Resume)
        self.assertEqual(resumed.phase, Phase.ACTING)
        self.assertEqual(resumed.attempts, state.attempts)
        routed = feedback(verdict(outcome(resumed)), Route.WAIT)
        self.assertEqual(send(routed, Resume).phase, Phase.ROUTING)
        terminated = send(routed, Terminate, reason="用户取消")
        self.assertEqual(terminated.phase, Phase.TERMINATED)
        with self.assertRaises(LoopTransitionError):
            send(terminated, Resume)

    def test_stale_duplicate_and_cross_run_events_rejected(self):
        initial = pagination_run()
        event = StartStage(run_id=initial.run_id, expected_version=initial.version, plan=stage(), attempt_id="A1")
        state = reduce(initial, event)
        with self.assertRaises(LoopTransitionError):
            reduce(state, event)
        with self.assertRaises(LoopTransitionError):
            reduce(state, Wait(run_id="other", expected_version=state.version, reason="不属于此运行"))
        for change in ({"attempt_id": "old"}, {"plan_version": 2}, {"todo_version": 2}):
            with self.subTest(change=change), self.assertRaises(LoopTransitionError):
                outcome(state, **change)

    def test_stage_addresses_and_progress_references_are_checked(self):
        for addresses in (("C2",), ("Q99",)):
            plan = StagePlan.model_validate({**stage().model_dump(), "addresses": addresses})
            with self.subTest(addresses=addresses), self.assertRaises(LoopTransitionError):
                begin(plan=plan)
        state = outcome(begin())
        for delta in (
            ProgressDelta(satisfied_criteria=(EvidenceLink(ref="C2", evidence_refs=("E",)),)),
            ProgressDelta(resolved_questions=(QuestionFinding(ref="Q99", finding="已解决", evidence_refs=("E",)),)),
            ProgressDelta(new_questions=(Question(id="Q2", description="无关", acceptance_refs=("C2",)),)),
        ):
            with self.subTest(delta=delta), self.assertRaises(LoopTransitionError):
                verdict(state, progress=delta)

    def test_regression_reopens_previously_completed_todo(self):
        state = complete_todo(pagination_run(), "T1", "S1")
        state = begin(state, stage("T2", "S2", kind=StageKind.IMPLEMENT), "A2")
        state = verdict(outcome(state), stage_status="fail", progress=ProgressDelta(
            regressed_criteria=(EvidenceLink(ref="C1", evidence_refs=("E-regression",)),),
        ))
        self.assertEqual(state.todos[0].status, TodoStatus.PENDING)
        self.assertFalse(state.satisfied_criteria)


if __name__ == "__main__":
    unittest.main()
