import asyncio
import json
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from qharness.exception import LoopExecutionError
from qharness.loop import Phase
from qharness.loop.config import LoopConfig
from qharness.loop.repository import digest
from qharness.run import RunService
from qharness.tools.base import ToolExecutionPolicy, ToolPolicyOverride, ToolRuntimePolicy, ToolExecutionResult
from qharness.tools.hooks import ToolExecutionHook
from qharness.verification import CheckCatalog, CheckSpec
from tests.support.task_flow import answer, catalog, flow_runtime, handed_back, planned, successful_script, write
from tests.support.workspaces import pagination_run


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, script=None, **kwargs):
        r = flow_runtime(script if script is not None else successful_script(investigate=False, repair=False), **kwargs)
        self.addCleanup(r.close)
        r.lifecycle = RunService(r.services)
        return r

    async def test_full_task_and_terminal_reentry_preserve_budget(self):
        r = self.runtime()
        result = await r.lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.COMPLETED)
        before = r.repository.snapshot()["budget"]
        self.assertEqual(await r.lifecycle.run(catalog()), result)
        self.assertEqual(r.repository.snapshot()["budget"], before)
        r.backend.assert_exhausted()

    async def test_pause_after_write_rebuild_and_resume_do_not_repeat_completed_action(self):
        r = self.runtime()
        writes = []
        class PauseAfterWrite(ToolExecutionHook):
            async def after_execute(self, request, tool, result):
                if tool.name == "write_file":
                    writes.append(request.operation_id)
                    if len(writes) == 1:
                        await r.lifecycle.submit("pause-1", "pause")
        r.executor.hooks.append(PauseAfterWrite())
        waiting = await r.lifecycle.run(catalog())
        self.assertEqual(waiting.phase, Phase.WAITING)
        self.assertEqual(waiting.resume_phase, Phase.ACTING)
        self.assertEqual((r.context.workspace.root / "pagination.py").read_text(), "fixed")
        attempt = waiting.active_attempt_id
        r.lifecycle = RunService(r.services)
        await r.lifecycle.submit("resume-1", "resume")
        result = await r.lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.COMPLETED)
        self.assertEqual(result.attempts[0].attempt_id, attempt)
        self.assertEqual(len(writes), 2)
        self.assertEqual(len(set(writes)), 2)
        self.assertEqual(r.repository.snapshot()["budget"]["tool_executions"], 8)
        r.backend.assert_exhausted()

    async def test_partial_batch_resumes_remaining_call_only(self):
        first, second = write("pagination.py", "fixed"), write("integration.py", "wired")
        second.message.tool_calls[0].id = "second"
        batch = replace(first, message=replace(first.message, tool_calls=first.message.tool_calls + second.message.tool_calls))
        r = self.runtime([planned(), batch, handed_back(), planned(), write("validation.py", "validated"), handed_back()])
        writes = []
        class Pause(ToolExecutionHook):
            async def after_execute(self, request, tool, result):
                if tool.name == "write_file":
                    writes.append(request.arguments["path"])
                    if len(writes) == 1:
                        await r.lifecycle.submit("pause", "pause")
        r.executor.hooks.append(Pause())
        waiting = await r.lifecycle.run(catalog())
        self.assertEqual(waiting.phase, Phase.WAITING)
        self.assertEqual(writes, ["pagination.py"])
        await r.lifecycle.submit("resume", "resume")
        self.assertEqual((await r.lifecycle.run(catalog())).phase, Phase.COMPLETED)
        self.assertEqual(writes, ["pagination.py", "integration.py", "validation.py"])

    async def test_approval_is_bound_to_call_and_duplicate_answers_do_not_execute_twice(self):
        policy = ToolExecutionPolicy(defaults=ToolRuntimePolicy(max_calls=100),
                                     tool_overrides={"write_file": ToolPolicyOverride(requires_approval=True)})
        r = self.runtime(policy=policy)
        first = await r.lifecycle.run(catalog())
        self.assertEqual(first.resume_phase, Phase.ACTING)
        self.assertEqual(r.repository.snapshot()["budget"]["tool_executions"], 0)
        for i in range(2):
            refs = [entry["payload"]["ref"] for entry in r.repository.inputs("applied") if entry["kind"] == "approval_request"]
            payload = {"approval_ref": refs[-1], "allow": True}
            await r.lifecycle.submit(f"answer-{i}", "approval", payload)
            await r.lifecycle.submit(f"answer-{i}", "approval", payload)
            result = await r.lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.COMPLETED)
        self.assertEqual(r.repository.snapshot()["budget"]["tool_executions"], 8)
        r.backend.assert_exhausted()

    async def test_old_approval_cannot_authorize_changed_workspace(self):
        policy = ToolExecutionPolicy(defaults=ToolRuntimePolicy(max_calls=100),
                                     tool_overrides={"write_file": ToolPolicyOverride(requires_approval=True)})
        r = self.runtime(policy=policy)
        await r.lifecycle.run(catalog())
        old = next(i["payload"]["ref"] for i in r.repository.inputs("applied") if i["kind"] == "approval_request")
        (r.context.workspace.root / "integration.py").write_text("changed")
        await r.lifecycle.submit("old-approval", "approval", {"approval_ref": old, "allow": True})
        result = await r.lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.WAITING)
        self.assertEqual(r.repository.snapshot()["budget"]["tool_executions"], 0)
        refs = [i["payload"]["ref"] for i in r.repository.inputs("applied") if i["kind"] == "approval_request"]
        self.assertEqual(len(set(refs)), 2)

    async def test_steering_arriving_after_model_response_blocks_old_action(self):
        r = None
        def old_action(_):
            r.repository.enqueue("steer", "steer", {"constraints": ["保留接口 SECRET_TOKEN_123"]})
            return write("pagination.py", "forbidden-old-action")
        r = self.runtime([planned(), old_action, *successful_script(investigate=False, repair=False)],
                         config=LoopConfig(max_check_retries=0))
        result = await r.lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.WAITING)
        self.assertEqual(result.resume_phase, Phase.VERIFYING_TASK)
        self.assertEqual(result.contract.version, 2)
        self.assertIn("保留接口 SECRET_TOKEN_123", result.contract.constraints)
        calls = [r.repository.call_record("tool", i["call_id"]) for i in r.lifecycle.recovery() if i["kind"] == "tool"]
        self.assertNotIn("forbidden-old-action", str(calls))
        self.assertNotIn("SECRET_TOKEN_123", json.dumps(r.lifecycle.events(), ensure_ascii=False))
        self.assertEqual((await r.lifecycle.submit("steer", "steer", {"constraints": ["保留接口 SECRET_TOKEN_123"]})), "applied")
        self.assertEqual(r.repository.snapshot()["contract"].version, 2)
        with self.assertRaises(LoopExecutionError):
            await r.lifecycle.submit("steer", "steer", {"constraints": ["different"]})

    async def test_new_constraint_needs_fresh_independent_coverage(self):
        r = self.runtime()
        await r.lifecycle.submit("steer", "steer", {"constraints": ["保留接口"]})
        checks = CheckCatalog((*catalog().specs, CheckSpec(id="api", scope="task", targets=("保留接口",),
            command="integration", kind="unittest", inputs=("pagination.py", "integration.py"))))
        result = await r.lifecycle.run(checks)
        self.assertEqual(result.phase, Phase.COMPLETED)
        self.assertEqual(result.task_verdicts[-1].contract_version, 2)

    async def test_preplan_pause_and_resume_and_steering(self):
        r = self.runtime([answer(pagination_run().todo_plan), *successful_script(investigate=False, repair=False)],
                         initial_plan=False, config=LoopConfig(max_check_retries=0))
        await r.lifecycle.submit("pause", "pause")
        self.assertIsNone(await r.lifecycle.run(catalog()))
        self.assertFalse(r.backend.requests)
        await r.lifecycle.submit("steer", "steer", {"constraints": ["新要求"]})
        await r.lifecycle.submit("resume", "resume")
        result = await r.lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.WAITING)
        self.assertEqual(result.contract.constraints, ("新要求",))

    async def test_disconnect_does_not_cancel_service_and_events_can_be_read_again(self):
        r = self.runtime()
        r.lifecycle.start(catalog())
        waiter = asyncio.create_task(r.lifecycle.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        result = await r.lifecycle.wait()
        self.assertEqual(result.phase, Phase.COMPLETED)
        first = r.lifecycle.events(limit=2)
        second = r.lifecycle.events(after=first[-1]["sequence"])
        self.assertEqual(first, r.lifecycle.events(limit=2))
        self.assertGreater(second[0]["sequence"], first[-1]["sequence"])
        self.assertTrue(any(e["kind"] == "tool_finished" for e in second))

    async def test_unknown_effect_is_not_cleared_by_resume(self):
        r = self.runtime()
        def disk_full(*args, **kwargs):
            raise LoopExecutionError("故障注入：结果未落盘", code="storage_error")
        with patch.object(r.services.repository, "finish_tool", disk_full):
            with self.assertRaises(LoopExecutionError):
                await r.lifecycle.run(catalog())
        unknown = [i for i in r.lifecycle.recovery() if i["kind"] == "tool" and i["status"] == "dispatched"]
        self.assertEqual(len(unknown), 1)
        await r.lifecycle.submit("resume-without-proof", "resume")
        waiting = await r.lifecycle.run(catalog())
        self.assertEqual(waiting.phase, Phase.WAITING)
        self.assertTrue(r.repository.unresolved_calls())
        call = unknown[0]
        mutation = r.context.mutation_service.inspect(call["operation_id"])
        self.assertEqual(mutation.status, "applied")
        recovered = ToolExecutionResult(call["call_id"], "write_file", True, "文件写入已由操作历史核对", 0,
                                        data=mutation.to_dict())
        proof = await r.lifecycle.recovery_controller.observe("tool", call["call_id"], resolution="completed",
            description="已检查实际文件和原 operation 的 Commit；执行者已经停止", quiescent=True, result=recovered)
        self.assertEqual(await r.lifecycle.recovery_controller.reconcile(proof), "reconciled")
        self.assertEqual(await r.lifecycle.recovery_controller.reconcile(proof), "done")
        await r.lifecycle.submit("resume-after-proof", "resume")
        result = await r.lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.COMPLETED)
        self.assertEqual(r.repository.snapshot()["budget"]["tool_executions"], 8)
        r.backend.assert_exhausted()

    async def test_external_workspace_change_on_resume_replans_before_old_action(self):
        r = None
        def pause_before_action(_):
            r.repository.enqueue("pause", "pause", {})
            return write("pagination.py", "old-action")
        r = self.runtime([planned(), pause_before_action, *successful_script(investigate=False, repair=False)])
        waiting = await r.lifecycle.run(catalog())
        (r.context.workspace.root / "pagination.py").write_text("user-edit")
        await r.lifecycle.submit("resume", "resume")
        result = await r.lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.COMPLETED)
        self.assertNotEqual(result.attempts[1].attempt_id, waiting.active_attempt_id)
        self.assertEqual(len(result.attempts), 3)
        calls = [r.repository.call_record("tool", i["call_id"]) for i in r.lifecycle.recovery() if i["kind"] == "tool"]
        self.assertNotIn("old-action", str(calls))
        self.assertTrue(any(e["kind"] == "input_applied" for e in r.lifecycle.events()))

    async def test_cancel_from_another_repository_interrupts_inflight_model(self):
        r = self.runtime([planned()])
        started = asyncio.Event()
        complete = r.backend.complete
        async def delayed(request):
            if json.loads(request.messages[1].content)["output_schema"]["title"] == "StagePlan":
                return await complete(request)
            started.set()
            await asyncio.Event().wait()
        r.backend.complete = delayed
        task = asyncio.create_task(r.lifecycle.run(catalog()))
        await asyncio.wait_for(started.wait(), 3)
        # 独立 repository 实例写入，不触碰执行者的内存 cancellation_event。
        r.repository.enqueue("remote-cancel", "cancel", {})
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result.phase, Phase.WAITING)
        self.assertTrue(r.context.cancelled)
        self.assertTrue(r.repository.unresolved_calls())

    async def test_input_consumption_and_steering_state_are_atomic(self):
        from sqlalchemy.exc import OperationalError
        r = self.runtime(config=LoopConfig(max_check_retries=0))
        await r.lifecycle.submit("steer", "steer", {"constraints": ["atomic requirement"]})
        before = r.repository.snapshot()["state"]
        with patch.object(r.services.repository, "_journal", side_effect=OperationalError("hidden SQL", {}, Exception("disk full"))):
            with self.assertRaises(LoopExecutionError) as error:
                await r.lifecycle.run(catalog())
        self.assertEqual(error.exception.code, "storage_error")
        self.assertNotIn("hidden SQL", str(error.exception))
        self.assertEqual(r.repository.snapshot()["state"], before)
        self.assertEqual(r.repository.inputs()[0]["id"], "steer")
        result = await r.lifecycle.run(catalog())
        self.assertEqual(result.contract.version, 2)
        self.assertEqual(len(result.contract.constraints), 1)

    async def test_failure_before_dispatch_leaves_admitted_call_resumable(self):
        r = self.runtime()
        with patch.object(r.services.repository, "claim_tool", side_effect=LoopExecutionError("dispatch interruption", code="storage_error")):
            with self.assertRaises(LoopExecutionError):
                await r.lifecycle.run(catalog())
        calls = [i for i in r.lifecycle.recovery() if i["kind"] == "tool"]
        self.assertEqual([i["status"] for i in calls], ["admitted"])
        self.assertEqual((r.context.workspace.root / "pagination.py").read_text(), "wrong")
        result = await r.lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.COMPLETED)
        self.assertEqual(r.repository.snapshot()["budget"]["tool_executions"], 8)
        r.backend.assert_exhausted()

    async def test_rebuild_database_context_tools_and_executor_after_pause(self):
        from qharness.persistence import DatabaseManager
        from qharness.run import RunContext, create_loop_services
        from qharness.tools.executor import ToolExecutor
        from qharness.tools.registry import ToolRegistry
        from qharness.tools.builtin.write_file import create_write_file_tool
        from qharness.tools.builtin.run_command import create_run_command_tool
        from qharness.workspace import DulwichFileVersionStore, SqlAlchemyWorkspaceHistoryRepository, WorkspaceMutationService
        from tests.support.scripted_backend import ScriptedModelBackend
        r = self.runtime([planned(), write("pagination.py", "fixed")])
        class Pause(ToolExecutionHook):
            async def after_execute(self, request, tool, result):
                if tool.name == "write_file":
                    await r.lifecycle.submit("pause", "pause")
        r.executor.hooks.append(Pause())
        waiting = await r.lifecycle.run(catalog())
        before = r.repository.snapshot()["budget"]
        r.database.close()
        db = DatabaseManager(r.database.config)
        self.addCleanup(db.close)
        db.initialize()
        context = RunContext("tenant", "workspace", waiting.run_id, r.context.workspace, r.sandbox)
        context.metadata["verification_environment"] = "offline-task-checks-v1"
        history = SqlAlchemyWorkspaceHistoryRepository(db.session_factory, tenant_id="tenant", workspace_id="workspace", local_root=r.root)
        versions = DulwichFileVersionStore(r.root / "versions", tenant_id="tenant", workspace_id="workspace")
        context.mutation_service = WorkspaceMutationService(context.workspace, history, versions, run_id=waiting.run_id)
        registry = ToolRegistry()
        registry.register(create_run_command_tool(context))
        registry.register(create_write_file_tool(context.mutation_service))
        backend = ScriptedModelBackend([handed_back(), planned(), write("validation.py", "validated"), handed_back()])
        services = create_loop_services(context, backend=backend, executor=ToolExecutor(registry, policy=r.policy),
            database_manager=db, config=r.config, contract=waiting.contract)
        lifecycle = RunService(services)
        self.assertEqual(services.repository.snapshot()["budget"], before)
        await lifecycle.submit("resume", "resume")
        result = await lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.COMPLETED)
        self.assertEqual(result.attempts[0].attempt_id, waiting.active_attempt_id)
        self.assertEqual(services.repository.snapshot()["budget"]["tool_executions"], 8)
        backend.assert_exhausted()


if __name__ == "__main__":
    unittest.main()
