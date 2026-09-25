import asyncio
import json
import unittest
from dataclasses import replace

from tests.loop.test_transitions import begin
from tests.support.loop_runtime import LoopRuntime
from tests.support.workspaces import ScriptedTool
from qharness.exception import LoopExecutionError
from qharness.loop.tool_service import ToolService
from qharness.loop.repository import LoopRepository
from qharness.persistence import DatabaseManager
from qharness.run import RunContext
from qharness.tools.base import Tool, ToolExecutionPolicy, ToolParameters, ToolRuntimePolicy, current_operation_id
from qharness.tools.builtin.run_command import create_run_command_tool
from qharness.tools.builtin.write_file import create_write_file_tool
from qharness.tools.builtin.replace_text import create_replace_text_tool
from qharness.tools.builtin.apply_patch import create_apply_patch_tool
from qharness.tools.builtin.file_history import create_rollback_file_change_tool
from qharness.tools.hooks import ToolExecutionHook, ToolHookDecision
from qharness.workspace import DulwichFileVersionStore, SqlAlchemyWorkspaceHistoryRepository, WorkspaceMutationService


class Approve(ToolExecutionHook):
    async def before_execute(self, request, tool):
        return ToolHookDecision.allow()


class ToolServiceTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, **kwargs):
        runtime = LoopRuntime(state=begin(), **kwargs)
        self.addCleanup(runtime.close)
        return runtime

    def attach_history(self, runtime):
        history = SqlAlchemyWorkspaceHistoryRepository(runtime.database.session_factory, tenant_id="tenant",
                                                         workspace_id="workspace", local_root=runtime.root)
        versions = DulwichFileVersionStore(runtime.root / "versions", tenant_id="tenant", workspace_id="workspace")
        service = WorkspaceMutationService(runtime.context.workspace, history, versions, run_id=runtime.context.run_id)
        runtime.context.mutation_service = service
        return service

    async def test_duplicate_execution_and_rebuilt_service_only_read_persisted_result(self):
        runtime = self.runtime()
        scripted = ScriptedTool([{"answer": 42}])
        runtime.registry.register(scripted.as_tool())
        service = ToolService(runtime.executor, runtime.context, runtime.repository)
        first = await service.execute("call", "fixture_check", {})
        second = await service.execute("call", "fixture_check", {})
        rebuilt = ToolService(runtime.executor, runtime.context, runtime.repository)
        third = rebuilt.read_result("call")
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(scripted.calls, 1)
        self.assertEqual(runtime.context.tool_state.total_calls, 1)
        self.assertEqual(runtime.repository.snapshot()["budget"]["tool_executions"], 1)

    async def test_new_database_manager_and_run_context_restore_old_counts_and_result(self):
        runtime = self.runtime()
        scripted = ScriptedTool([{"value": 1}])
        runtime.registry.register(scripted.as_tool())
        first = await ToolService(runtime.executor, runtime.context, runtime.repository).execute("call", "fixture_check", {})
        runtime.database.close()
        reopened = DatabaseManager(runtime.database.config)
        self.addCleanup(reopened.close)
        reopened.initialize()
        repository = LoopRepository(reopened.session_factory, tenant_id="tenant", workspace_id="workspace", run_id=runtime.state.run_id)
        context = RunContext("tenant", "workspace", runtime.state.run_id, runtime.context.workspace, runtime.sandbox)
        restored = ToolService(runtime.executor, context, repository)
        self.assertEqual(context.tool_state.total_calls, 1)
        self.assertEqual(await restored.execute("call", "fixture_check", {}), first)
        self.assertEqual(scripted.calls, 1)

    async def test_mutating_executor_policy_cannot_relax_persisted_admission(self):
        runtime = self.runtime(policy=ToolExecutionPolicy(max_total_calls=1))
        runtime.registry.register(ScriptedTool([{"value": 1}]).as_tool())
        service = ToolService(runtime.executor, runtime.context, runtime.repository)
        runtime.executor.policy = ToolExecutionPolicy(max_total_calls=None)
        with self.assertRaises(LoopExecutionError):
            await service.execute("call", "fixture_check", {})
        self.assertEqual(runtime.repository.snapshot()["budget"]["tool_calls"], 0)

    async def test_inflight_duplicate_is_unknown_and_does_not_run_handler_twice(self):
        runtime = self.runtime()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def handler():
            calls.append(current_operation_id())
            entered.set()
            await release.wait()
            return {"ok": True}
        runtime.registry.register(Tool("slow", "可控工具", ToolParameters, handler))
        service = ToolService(runtime.executor, runtime.context, runtime.repository)
        first = asyncio.create_task(service.execute("call", "slow", {}))
        await entered.wait()
        duplicate = await service.execute("call", "slow", {})
        self.assertEqual(duplicate.error_code, "unknown")
        release.set()
        self.assertTrue((await first).success)
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(calls[0])
        self.assertIsNone(current_operation_id())

    async def test_approval_retry_keeps_logical_count_but_counts_new_execution(self):
        runtime = self.runtime()
        scripted = ScriptedTool([{"ok": True}])
        runtime.registry.register(replace(scripted.as_tool(), requires_approval=True))
        service = ToolService(runtime.executor, runtime.context, runtime.repository)
        rejected = await service.execute("call", "fixture_check", {})
        self.assertEqual(rejected.error_code, "rejected")
        self.assertEqual(scripted.calls, 0)
        runtime.executor.hooks.append(Approve())
        accepted = await service.execute("call", "fixture_check", {}, retry=True)
        self.assertTrue(accepted.success)
        budget = runtime.repository.snapshot()["budget"]
        self.assertEqual((budget["tool_calls"], budget["tool_executions"]), (1, 2))
        with self.assertRaises(LoopExecutionError):
            await service.execute("call", "fixture_check", {}, retry=True)

    async def test_command_nonzero_exit_survives_summary_truncation_and_artifact_reload(self):
        runtime = self.runtime(policy=ToolExecutionPolicy(defaults=ToolRuntimePolicy(max_result_chars=512)))
        runtime.registry.register(create_run_command_tool(runtime.context))
        service = ToolService(runtime.executor, runtime.context, runtime.repository)
        result = await service.execute("command-1", "run_command", {"command": "fixture"})
        self.assertTrue(result.success)
        self.assertFalse(result.data["succeeded"])
        self.assertEqual(result.data["exit_code"], 1)
        self.assertEqual(len(result.data["stdout"]), 5000)
        self.assertLessEqual(len(result.content), 512)
        self.assertEqual(json.loads(result.content)["exit_code"], 1)
        self.assertEqual(runtime.repository.read_artifact(result.artifact_id)["data"]["stdout"], "x" * 5000)
        self.assertEqual(service.read_result("command-1").data, result.data)
        self.assertEqual(runtime.sandbox.requests[0].operation_id, result.data["operation_id"])

    async def test_nontruncating_output_policy_still_preserves_business_data(self):
        runtime = self.runtime(policy=ToolExecutionPolicy(defaults=ToolRuntimePolicy(
            max_result_chars=64, truncate_oversized_results=False)))
        runtime.registry.register(ScriptedTool([{"exit_code": 1, "stdout": "x" * 5000}]).as_tool())
        service = ToolService(runtime.executor, runtime.context, runtime.repository)
        result = await service.execute("call", "fixture_check", {})
        self.assertEqual(result.error_code, "result_too_large")
        self.assertEqual(service.read_result("call").data["exit_code"], 1)

    async def test_file_tools_use_trusted_identity_and_share_it_with_existing_history(self):
        runtime = self.runtime()
        mutation = self.attach_history(runtime)
        for factory in (create_write_file_tool, create_replace_text_tool, create_apply_patch_tool, create_rollback_file_change_tool):
            runtime.registry.register(factory(mutation))
        service = ToolService(runtime.executor, runtime.context, runtime.repository)
        created = await service.execute("w1", "write_file", {"path": "a.py", "content": "old\n"})
        self.assertTrue(created.success, created.content)
        operation = created.data["operation_id"]
        self.assertEqual(mutation.history_repository.get_operation(operation).tool_name, "write_file")
        replaced = await service.execute("w2", "replace_text", {"path": "a.py", "old_text": "old", "new_text": "new"})
        self.assertTrue(replaced.success, replaced.content)
        patched = await service.execute("w3", "apply_patch", {"patch": "*** Begin Patch\n*** Add File: b.py\n+hello\n*** End Patch\n"})
        self.assertTrue(patched.success, patched.content)
        rollback = await service.execute("w4", "rollback_file_change", {"operation_id": patched.data["operation_id"]})
        self.assertTrue(rollback.success, rollback.content)
        self.assertNotEqual(rollback.data["operation_id"], patched.data["operation_id"])
        self.assertFalse((runtime.context.workspace.root / "b.py").exists())
        invalid = await service.execute("w5", "write_file", {"path": "bad.py", "content": "x", "operation_id": "forged"})
        self.assertEqual(invalid.error_code, "invalid_arguments")
        self.assertFalse((runtime.context.workspace.root / "bad.py").exists())
        self.assertEqual((await service.execute("w1", "write_file", {"path": "a.py", "content": "old\n"})).data, created.data)
        self.assertEqual((runtime.context.workspace.root / "a.py").read_text(), "new\n")

    async def test_command_mutation_and_sandbox_share_exact_operation_id(self):
        runtime = self.runtime()
        mutation = self.attach_history(runtime)
        runtime.sandbox.on_execute = lambda request: (runtime.context.workspace.root / "generated.py").write_text("changed")
        runtime.registry.register(create_run_command_tool(runtime.context))
        service = ToolService(runtime.executor, runtime.context, runtime.repository)
        result = await service.execute("command", "run_command", {"command": "fixture"})
        self.assertTrue(result.success, result.content)
        self.assertEqual(result.data["operation_id"], result.data["workspace_change"]["operation_id"])
        self.assertIsNotNone(mutation.history_repository.get_operation(result.data["operation_id"]).commit_id)

    async def test_timeout_stays_unknown_on_rebuilt_service(self):
        runtime = self.runtime(policy=ToolExecutionPolicy(defaults=ToolRuntimePolicy(timeout_seconds=0.01)))
        calls = []
        async def handler():
            calls.append(1)
            await asyncio.Event().wait()
        runtime.registry.register(Tool("slow", "超时工具", ToolParameters, handler))
        service = ToolService(runtime.executor, runtime.context, runtime.repository)
        result = await service.execute("call", "slow", {})
        self.assertEqual(result.error_code, "unknown")
        rebuilt = ToolService(runtime.executor, runtime.context, runtime.repository)
        self.assertEqual((await rebuilt.execute("call", "slow", {})).error_code, "unknown")
        self.assertEqual(len(calls), 1)
