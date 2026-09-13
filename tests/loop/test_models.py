import unittest

from pydantic import TypeAdapter, ValidationError

from tests.support.workspaces import pagination_run, stage
from qharness.loop import (
    EvidenceLink, LoopEvent, Phase, ProgressDelta, Question, RunState,
    StagePlan, StageVerdict, StartStage, Todo, TodoPlan, create_run,
)


class ModelTests(unittest.TestCase):
    def test_todo_dependency_graph_rejects_missing_self_and_cycles(self):
        todo = pagination_run().todo_plan.todos[0].model_dump()
        for dependencies in (("missing",), ("T1",)):
            with self.subTest(dependencies=dependencies), self.assertRaises(ValidationError):
                TodoPlan(todos=(dict(todo, dependencies=dependencies),))
        with self.assertRaises(ValidationError):
            TodoPlan(todos=(dict(todo, dependencies=("T2",)),
                            dict(todo, id="T2", dependencies=("T1",))))

    def test_plan_cannot_drop_task_criterion_or_add_unknown_reference(self):
        state = pagination_run()
        for refs in (("C1",), ("C1", "C2", "C99")):
            plan = TodoPlan(todos=(Todo(id="T", objective="实现", acceptance_refs=refs, done_when=("通过",)),))
            with self.subTest(refs=refs), self.assertRaises(ValidationError):
                create_run("run", state.contract, plan)

    def test_role_output_rejects_unknown_fields_and_empty_stage_results(self):
        for change in ({"complete_run": True}, {"expected_results": []}, {"addresses": []},
                       {"plan_version": True}, {"objective": "  "}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                StagePlan.model_validate({**stage().model_dump(), **change})

    def test_success_requires_evidence_and_distinct_scope(self):
        identity = dict(stage_id="S", plan_version=1, todo_id="T", todo_version=1, attempt_id="A")
        with self.assertRaises(ValidationError):
            StageVerdict(**identity, stage_status="pass", todo_status="fail", expected_vs_observed="已定位")
        with self.assertRaises(ValidationError):
            StageVerdict(**identity, stage_status="fail", todo_status="pass", expected_vs_observed="矛盾",
                         evidence_refs=("E1",))

    def test_contradictory_progress_is_rejected(self):
        evidence = EvidenceLink(ref="C1", evidence_refs=("E",))
        with self.assertRaises(ValidationError):
            ProgressDelta(satisfied_criteria=(evidence,), regressed_criteria=(evidence,))

    def test_questions_must_link_to_task_and_not_shadow_criteria(self):
        state = pagination_run()
        for question in (Question(id="C1", description="冲突", acceptance_refs=("C1",)),
                         Question(id="Q2", description="无关", acceptance_refs=("C99",))):
            with self.subTest(question=question.id), self.assertRaises(ValidationError):
                create_run("run", state.contract, state.todo_plan, questions=(question,))

    def test_state_snapshot_and_event_roundtrip_and_immutability(self):
        state = pagination_run()
        self.assertEqual(RunState.model_validate_json(state.model_dump_json()), state)
        event = StartStage(run_id=state.run_id, expected_version=1, plan=stage(), attempt_id="A")
        self.assertEqual(TypeAdapter(LoopEvent).validate_json(event.model_dump_json()), event)
        with self.assertRaises(ValidationError):
            state.contract.objective = "降低要求"
        self.assertIsInstance(state.todos, tuple)

    def test_completed_snapshot_without_task_verification_is_rejected(self):
        state = pagination_run().model_dump()
        state["phase"] = Phase.COMPLETED
        for todo in state["todos"]:
            todo["status"] = "passed"
            todo["evidence_refs"] = ["E"]
        state["satisfied_criteria"] = [dict(ref=ref, evidence_refs=["E"]) for ref in ("C1", "C2")]
        with self.assertRaisesRegex(ValidationError, "独立 Task PASS"):
            RunState.model_validate(state)


if __name__ == "__main__":
    unittest.main()
