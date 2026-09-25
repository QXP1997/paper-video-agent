import asyncio
import json
import unittest
from dataclasses import replace

from qharness.exception import LoopConfigurationError, LoopExecutionError
from qharness.loop import ApplyFeedback, CheckStatus, FeedbackDecision, Phase, Route, TodoStatus
from qharness.loop.repository import LoopRepository
from qharness.loop.tool_service import ToolService
from qharness.tools.base import ToolExecutionPolicy, ToolExecutionResult, ToolRuntimePolicy
from qharness.tools.builtin.run_command import create_run_command_tool
from qharness.verification import CheckRunner, CheckSpec, QuestionConclusion, VerificationController
from qharness.verification.evidence import interpret
from tests.loop.test_transitions import all_todos_passed, begin, outcome
from tests.support.loop_runtime import LoopRuntime


PASS_LOG = "Ran 2 tests in 0.01s\n\nOK\n"
FAIL_LOG = "FAIL: test_offset (test_pagination.PaginationTest)\nRan 2 tests in 0.01s\n\nFAILED (failures=1)\n"


class InterpretationTests(unittest.TestCase):
    def test_exit_status_test_counts_errors_and_incomplete_logs(self):
        cases = [
            ("unittest", 0, PASS_LOG, "pass"),
            ("unittest", 1, FAIL_LOG, "fail"),
            ("unittest", 0, "Ran 0 tests in 0s\nOK\n", "inconclusive"),
            ("unittest", 0, "Ran 2 tests in 0s\nOK (skipped=2)\n", "inconclusive"),
            ("unittest", 1, "Ran 2 tests in 0s\nFAILED (errors=1)\n", "error"),
            ("unittest", 1, PASS_LOG, "error"),
            ("unittest", 127, "command not found", "error"),
            ("pytest", 5, "no tests ran in 0.01s", "inconclusive"),
            ("pytest", 2, "ImportError", "error"),
            ("pytest", 0, "================ 2 passed in 0.01s ================\n", "pass"),
            ("pytest", 1, "FAILED test_x.py::test_x - AssertionError\n1 failed, 1 passed in 0.01s\n", "fail"),
            ("pytest", 0, "2 skipped in 0.01s\n", "inconclusive"),
            ("pytest", 0, "the tool printed 2 passed", "inconclusive"),
            ("command", 127, "not found", "error"),
        ]
        for kind, code, log, status in cases:
            with self.subTest(kind=kind, code=code, log=log):
                spec = CheckSpec(id="test", scope="task", targets=("C1",), command="test", kind=kind)
                result = ToolExecutionResult("call", "run_command", True, "display only", 0,
                    data={"exit_code": code, "stdout": log, "stderr": ""})
                self.assertEqual(interpret(spec, result).status, status)
                self.assertEqual(interpret(spec, replace(result, data={**result.data, "stdout_truncated": True})).status,
                                 CheckStatus.INCONCLUSIVE)


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, *, state=None, policy=None):
        runtime = LoopRuntime(state=state or outcome(begin()), policy=policy)
        self.addCleanup(runtime.close)
        runtime.context.metadata["verification_environment"] = "fixture-image:1/deps:1"
        (runtime.context.workspace.root / "subject.py").write_text("original", encoding="utf-8")
        runtime.registry.register(create_run_command_tool(runtime.context))
        runtime.sandbox.exit_code, runtime.sandbox.stdout = 0, PASS_LOG
        runtime.checks = CheckRunner(ToolService(runtime.executor, runtime.context, runtime.repository))
        runtime.verifier = VerificationController(runtime.checks)
        return runtime

    def spec(self, runtime, scope="stage", *, id=None, targets=None, **changes):
        state = runtime.repository.snapshot()["state"]
        if targets is None:
            if scope == "stage":
                targets = state.attempts[-1].plan.expected_results
            elif scope == "todo":
                todo = next(t.todo for t in state.todos if t.todo.id == state.attempts[-1].plan.todo_id)
                targets = (*todo.acceptance_refs, *todo.done_when)
            else:
                targets = tuple(c.id for c in state.contract.criteria) + state.contract.constraints
        return CheckSpec(**dict(id=id or scope, scope=scope, targets=targets, command="python -m unittest",
                                kind="unittest", inputs=("subject.py",)) | changes)

    async def test_investigation_closes_question_without_claiming_fix(self):
        r = self.runtime()
        spec = self.spec(r, conclusions=(QuestionConclusion(question_id="Q1", finding="复现确认偏移量错误"),))
        verdict = await r.verifier.verify_stage("investigation", [spec])
        self.assertEqual(verdict.stage_status, CheckStatus.PASS)
        self.assertEqual(verdict.todo_status, CheckStatus.INCONCLUSIVE)
        self.assertEqual(verdict.progress.resolved_questions[0].ref, "Q1")
        self.assertFalse(verdict.progress.satisfied_criteria)
        self.assertEqual(r.repository.snapshot()["state"].todos[0].status, TodoStatus.ACTIVE)

    async def test_todo_requires_its_own_checks_and_done_when(self):
        r = self.runtime()
        verdict = await r.verifier.verify_stage("stage", [self.spec(r), self.spec(r, "todo", targets=("C1",))])
        self.assertEqual(verdict.stage_status, CheckStatus.PASS)
        self.assertEqual(verdict.todo_status, CheckStatus.INCONCLUSIVE)
        self.assertEqual(verdict.progress.satisfied_criteria[0].ref, "C1")

    async def test_stage_and_todo_pass_leave_task_open(self):
        r = self.runtime()
        verdict = await r.verifier.verify_stage("stage", [self.spec(r), self.spec(r, "todo")])
        self.assertEqual(verdict.todo_status, CheckStatus.PASS)
        state = r.repository.snapshot()["state"]
        self.assertEqual(state.phase, Phase.ROUTING)
        self.assertEqual(state.todos[0].status, TodoStatus.PASSED)
        self.assertFalse(state.task_verdicts)

    async def test_missing_task_requirement_stays_inconclusive_even_when_todos_passed(self):
        r = self.runtime(state=all_todos_passed())
        verdict = await r.verifier.verify_task("task", [self.spec(r, "task", targets=("C1",))])
        self.assertEqual(verdict.status, CheckStatus.INCONCLUSIVE)
        self.assertEqual(r.repository.snapshot()["state"].phase, Phase.VERIFYING_TASK)
        report = r.repository.read_artifact(r.verifier.report_ref("task"))
        self.assertTrue(any(f["check_id"] == "missing:task:C2" and f["evidence_ref"] is None for f in report["failures"]))

    async def test_independent_task_checks_complete_run(self):
        r = self.runtime(state=all_todos_passed())
        verdict = await r.verifier.verify_task("task", [self.spec(r, "task")])
        self.assertEqual(verdict.status, CheckStatus.PASS)
        self.assertEqual(r.repository.snapshot()["state"].phase, Phase.COMPLETED)
        self.assertEqual(len(r.sandbox.requests), 1)

    async def test_integration_failure_reopens_affected_todo(self):
        r = self.runtime(state=all_todos_passed())
        r.sandbox.exit_code, r.sandbox.stdout = 1, FAIL_LOG
        verdict = await r.verifier.verify_task("task", [self.spec(r, "task", targets=("C1",))])
        self.assertEqual(verdict.status, CheckStatus.FAIL)
        state = r.repository.snapshot()["state"]
        self.assertEqual(state.phase, Phase.PLANNING)
        self.assertEqual(state.todos[0].status, TodoStatus.PENDING)
        self.assertEqual(state.todos[1].status, TodoStatus.PASSED)

    async def test_environment_error_produces_failure_bundle_without_code_regression(self):
        r = self.runtime()
        r.sandbox.exit_code, r.sandbox.stdout = 2, "ImportError during collection"
        verdict = await r.verifier.verify_stage("env", [self.spec(r, kind="pytest"), self.spec(r, "todo", kind="pytest")])
        self.assertEqual(verdict.stage_status, CheckStatus.ERROR)
        self.assertFalse(verdict.progress.regressed_criteria)
        report = r.repository.read_artifact(r.verifier.report_ref("env"))
        self.assertEqual(report["failures"][0]["status"], "error")
        self.assertIn("环境", report["failures"][0]["next_action"])

    async def test_unavailable_sandbox_is_check_error_and_never_dispatches(self):
        from qharness.sandbox.base import SandboxStatus
        r = self.runtime()
        async def unavailable():
            return SandboxStatus("fixture", False, "unavailable")
        r.sandbox.check_status = unavailable
        verdict = await r.verifier.verify_stage("env", [self.spec(r)])
        self.assertEqual(verdict.stage_status, CheckStatus.ERROR)
        self.assertFalse(r.sandbox.requests)

    async def test_run_identity_prevents_reexecution_after_service_rebuild(self):
        r = self.runtime()
        spec = self.spec(r)
        first = await r.checks.run("round", spec, expected_version=r.state.version)
        rebuilt = CheckRunner(ToolService(r.executor, r.context, r.repository))
        second = await rebuilt.run("round", spec, expected_version=r.state.version)
        self.assertEqual(first, second)
        self.assertEqual(len(r.sandbox.requests), 1)
        self.assertEqual(r.repository.snapshot()["budget"]["tool_executions"], 1)

    async def test_file_definition_environment_and_unknown_dependencies_invalidate(self):
        r = self.runtime()
        spec = self.spec(r)
        evidence = await r.checks.run("round", spec, expected_version=r.state.version)
        (r.context.workspace.root / "unrelated.txt").write_text("new")
        self.assertTrue(await r.checks.current(evidence))
        changed = CheckSpec.model_validate({**spec.model_dump(), "command": "another command"})
        self.assertFalse(await r.checks.current(evidence, changed))
        r.context.metadata["verification_environment"] = "fixture-image:2"
        self.assertFalse(await r.checks.current(evidence))
        r.context.metadata["verification_environment"] = "fixture-image:1/deps:1"
        (r.context.workspace.root / "subject.py").write_text("edited")
        with self.assertRaises(LoopExecutionError) as raised:
            await r.checks.run("round", spec, expected_version=r.state.version)
        self.assertEqual(raised.exception.code, "stale_evidence")
        unknown = await r.checks.run("unknown", self.spec(r, inputs=None), expected_version=r.state.version)
        (r.context.workspace.root / ".gitignore").write_text("hidden.py\n")
        (r.context.workspace.root / "hidden.py").write_text("new ignored dependency")
        self.assertFalse(await r.checks.current(unknown))

    async def test_changes_during_check_cannot_create_pass(self):
        r = self.runtime()
        r.sandbox.on_execute = lambda _: (r.context.workspace.root / "subject.py").write_text("changed")
        verdict = await r.verifier.verify_stage("mutating", [self.spec(r)])
        self.assertEqual(verdict.stage_status, CheckStatus.INCONCLUSIVE)

    async def test_later_check_invalidates_earlier_check_in_same_round(self):
        r = self.runtime()
        def mutate(_):
            if len(r.sandbox.requests) == 2:
                (r.context.workspace.root / "subject.py").write_text("changed")
        r.sandbox.on_execute = mutate
        verdict = await r.verifier.verify_stage("mutating", [self.spec(r), self.spec(r, "todo")])
        self.assertEqual(verdict.stage_status, CheckStatus.INCONCLUSIVE)
        self.assertNotEqual(verdict.todo_status, CheckStatus.PASS)

    async def test_database_commit_guard_rejects_last_moment_file_change(self):
        r = self.runtime()
        original = r.repository.put_artifact
        def put(ref, value, **kwargs):
            result = original(ref, value, **kwargs)
            if ref == r.verifier.report_ref("race"):
                (r.context.workspace.root / "subject.py").write_text("changed at commit")
            return result
        r.repository.put_artifact = put
        with self.assertRaises(LoopExecutionError) as raised:
            await r.verifier.verify_stage("race", [self.spec(r), self.spec(r, "todo")])
        self.assertEqual(raised.exception.code, "stale_context")
        self.assertEqual(r.repository.snapshot()["state"].phase, Phase.VERIFYING)

    async def test_flaky_pass_fail_does_not_become_last_pass(self):
        r = self.runtime()
        def fluctuate(_):
            r.sandbox.exit_code = 1 if len(r.sandbox.requests) == 1 else 0
            r.sandbox.stdout = FAIL_LOG if r.sandbox.exit_code else PASS_LOG
        r.sandbox.on_execute = fluctuate
        verdict = await r.verifier.verify_stage("flaky", [self.spec(r, repetitions=2)])
        self.assertEqual(verdict.stage_status, CheckStatus.INCONCLUSIVE)
        self.assertEqual(len(r.sandbox.requests), 2)

    async def test_pre_existing_failure_is_labelled_and_never_waived(self):
        r = self.runtime()
        r.sandbox.exit_code, r.sandbox.stdout = 1, FAIL_LOG
        spec = self.spec(r)
        baseline = await r.checks.run("baseline", spec, expected_version=r.state.version)
        (r.context.workspace.root / "subject.py").write_text("modified implementation")
        evidence = await r.checks.run("after", spec, expected_version=r.state.version, baseline_ref=baseline.id)
        self.assertTrue(evidence.pre_existing)
        self.assertEqual(evidence.status, CheckStatus.FAIL)

    async def test_display_truncation_uses_full_artifact_but_sandbox_truncation_does_not(self):
        r = self.runtime(policy=ToolExecutionPolicy(defaults=ToolRuntimePolicy(max_result_chars=40)))
        evidence = await r.checks.run("full", self.spec(r), expected_version=r.state.version)
        self.assertEqual(evidence.status, CheckStatus.PASS)
        self.assertIn("Ran 2 tests", r.repository.read_artifact(evidence.observations[0].artifact_id)["data"]["stdout"])
        original = r.sandbox.execute
        async def truncated(request):
            return replace(await original(request), stdout_truncated=True)
        r.sandbox.execute = truncated
        evidence = await r.checks.run("truncated", self.spec(r), expected_version=r.state.version)
        self.assertEqual(evidence.status, CheckStatus.INCONCLUSIVE)

    async def test_refresh_reopens_passed_todo_and_clears_its_evidence(self):
        r = self.runtime()
        specs = [self.spec(r), self.spec(r, "todo")]
        await r.verifier.verify_stage("stage", specs)
        state = r.repository.snapshot()["state"]
        r.repository.apply(ApplyFeedback(run_id=state.run_id, expected_version=state.version,
            decision=FeedbackDecision(route=Route.ADVANCE, reason="已验证")))
        self.assertFalse(await r.verifier.refresh(specs))
        (r.context.workspace.root / "subject.py").write_text("new implementation")
        self.assertEqual(await r.verifier.refresh(specs), ("T1",))
        state = r.repository.snapshot()["state"]
        self.assertEqual(state.todos[0].status, TodoStatus.PENDING)
        self.assertFalse(state.satisfied_criteria)

    async def test_checks_share_tool_budget(self):
        r = self.runtime(policy=ToolExecutionPolicy(max_total_calls=1))
        with self.assertRaises(LoopExecutionError) as raised:
            await r.verifier.verify_stage("budget", [self.spec(r), self.spec(r, "todo")])
        self.assertEqual(raised.exception.code, "budget_exceeded")
        self.assertEqual(len(r.sandbox.requests), 1)
        self.assertEqual(r.repository.snapshot()["state"].phase, Phase.VERIFYING)

    async def test_cancelled_check_does_not_run_or_create_pass(self):
        r = self.runtime()
        r.context.cancellation_event.set()
        verdict = await r.verifier.verify_stage("cancel", [self.spec(r)])
        self.assertEqual(verdict.stage_status, CheckStatus.ERROR)
        self.assertFalse(r.sandbox.requests)

    async def test_actor_cannot_use_check_entry_via_normal_execute(self):
        r = self.runtime()
        with self.assertRaises(LoopConfigurationError):
            await r.checks.tools.execute("actor-call", "run_command", {"command": "echo pass"})
        with self.assertRaises(LoopConfigurationError):
            await r.verifier.verify_stage("bad", [self.spec(r, targets=("invented result",))])
        self.assertFalse(r.sandbox.requests)

    async def test_evidence_cannot_be_replaced_by_tool_artifact_or_other_tenant(self):
        r = self.runtime()
        evidence = await r.checks.run("round", self.spec(r), expected_version=r.state.version)
        with self.assertRaises(LoopExecutionError):
            r.checks.read(evidence.observations[0].artifact_id)
        other = LoopRepository(r.database.session_factory, tenant_id="other", workspace_id="workspace", run_id=r.state.run_id)
        with self.assertRaises(LoopExecutionError):
            other.read_artifact(evidence.id)

    def attach_judge(self, runtime, value):
        from qharness.loop.context import ContextCompiler
        from qharness.loop.model_service import ModelService
        from tests.loop.test_model_service import response
        from tests.support.scripted_backend import ScriptedModelBackend
        backend = ScriptedModelBackend([response(json.dumps(value))])
        service = ModelService(backend, runtime.repository, ContextCompiler(runtime.config))
        runtime.verifier = VerificationController(runtime.checks, service)
        return backend

    async def test_judge_pass_cannot_override_deterministic_failure_or_invent_progress(self):
        from qharness.loop import EvidenceLink, ProgressDelta, StageVerdict
        r = self.runtime()
        r.sandbox.exit_code, r.sandbox.stdout = 1, FAIL_LOG
        proposal = StageVerdict(**r.state.attempts[-1].identity.model_dump(), stage_status="pass", todo_status="pass",
            expected_vs_observed="模型宣称完成", evidence_refs=("invented",),
            progress=ProgressDelta(satisfied_criteria=(EvidenceLink(ref="C1", evidence_refs=("invented",)),)))
        backend = self.attach_judge(r, proposal.model_dump(mode="json"))
        verdict = await r.verifier.verify_stage("judge", [self.spec(r), self.spec(r, "todo")], judge=True)
        self.assertEqual(verdict.stage_status, CheckStatus.FAIL)
        self.assertEqual(verdict.todo_status, CheckStatus.FAIL)
        self.assertFalse(verdict.progress.satisfied_criteria)
        self.assertNotIn("invented", verdict.evidence_refs)
        self.assertEqual(r.repository.snapshot()["budget"]["model_attempts"], 1)
        self.assertIn("raw_results", str(backend.requests[0].messages))

    async def test_judge_unknown_preserves_task_uncertainty(self):
        from qharness.loop import TaskVerdict
        r = self.runtime(state=all_todos_passed())
        proposal = TaskVerdict(contract_version=1, status="inconclusive", summary="未能确认需求语义覆盖")
        self.attach_judge(r, proposal.model_dump(mode="json"))
        verdict = await r.verifier.verify_task("judge", [self.spec(r, "task")], judge=True)
        self.assertEqual(verdict.status, CheckStatus.INCONCLUSIVE)
        self.assertEqual(r.repository.snapshot()["state"].phase, Phase.VERIFYING_TASK)

    async def test_judge_pass_does_not_fill_missing_task_coverage(self):
        from qharness.loop import EvidenceLink, TaskVerdict
        r = self.runtime(state=all_todos_passed())
        proposal = TaskVerdict(contract_version=1, status="pass", summary="全部通过",
            criterion_evidence=tuple(EvidenceLink(ref=c, evidence_refs=("invented",)) for c in ("C1", "C2")))
        self.attach_judge(r, proposal.model_dump(mode="json"))
        verdict = await r.verifier.verify_task("judge", [self.spec(r, "task", targets=("C1",))], judge=True)
        self.assertEqual(verdict.status, CheckStatus.INCONCLUSIVE)
        self.assertEqual([e.ref for e in verdict.criterion_evidence], ["C1"])

    async def test_check_identity_does_not_rebind_old_tool_results_after_crash(self):
        r = self.runtime()
        spec = self.spec(r)
        original = r.repository.put_artifact
        def crash(ref, value, **kwargs):
            if value.get("kind") == "check_evidence_v1":
                raise RuntimeError("simulated process loss after tool finished")
            return original(ref, value, **kwargs)
        r.repository.put_artifact = crash
        with self.assertRaises(RuntimeError):
            await r.checks.run("crash", spec, expected_version=r.state.version)
        r.repository.put_artifact = original
        (r.context.workspace.root / "subject.py").write_text("changed during restart")
        with self.assertRaises(LoopExecutionError):
            await r.checks.run("crash", spec, expected_version=r.state.version)
        self.assertEqual(len(r.sandbox.requests), 1)

    async def test_unknown_dispatch_stops_repeated_checks(self):
        r = self.runtime(policy=ToolExecutionPolicy(defaults=ToolRuntimePolicy(timeout_seconds=0.01)))
        async def slow(request):
            r.sandbox.requests.append(request)
            await asyncio.sleep(5)
        r.sandbox.execute = slow
        evidence = await r.checks.run("unknown", self.spec(r, repetitions=3), expected_version=r.state.version)
        self.assertEqual(evidence.status, CheckStatus.ERROR)
        self.assertEqual(len(r.sandbox.requests), 1)
        self.assertEqual(len(evidence.observations), 1)

    async def test_task_constraint_violation_blocks_completion_and_reopens_todos(self):
        from qharness.loop import RunState
        raw = all_todos_passed().model_dump()
        raw["contract"]["constraints"] = ("不得修改原始测试",)
        r = self.runtime(state=RunState.model_validate(raw))
        def constraint_failure(_):
            if len(r.sandbox.requests) == 2:
                r.sandbox.exit_code, r.sandbox.stdout = 1, FAIL_LOG
        r.sandbox.on_execute = constraint_failure
        verdict = await r.verifier.verify_task("constraint", [
            self.spec(r, "task", targets=("C1", "C2")),
            self.spec(r, "task", id="constraint", targets=("不得修改原始测试",)),
        ])
        self.assertEqual(verdict.status, CheckStatus.FAIL)
        self.assertEqual({e.ref for e in verdict.failed_criteria}, {"C1", "C2"})
        self.assertFalse(verdict.criterion_evidence)
        self.assertEqual(r.repository.snapshot()["state"].phase, Phase.PLANNING)

    async def test_environment_change_during_judge_invalidates_check(self):
        from qharness.loop import StageVerdict
        r = self.runtime()
        proposal = StageVerdict(**r.state.attempts[-1].identity.model_dump(), stage_status="pass", todo_status="pass",
                               expected_vs_observed="通过", evidence_refs=("model-reference",))
        self.attach_judge(r, proposal.model_dump(mode="json"))
        original = r.verifier.model.call
        async def change_after_judge(*args, **kwargs):
            result = await original(*args, **kwargs)
            r.context.metadata["verification_environment"] = "changed-image"
            return result
        r.verifier.model.call = change_after_judge
        verdict = await r.verifier.verify_stage("changed-env", [self.spec(r), self.spec(r, "todo")], judge=True)
        self.assertEqual(verdict.stage_status, CheckStatus.INCONCLUSIVE)
        self.assertEqual(verdict.todo_status, CheckStatus.INCONCLUSIVE)
