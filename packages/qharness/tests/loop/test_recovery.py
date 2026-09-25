import asyncio
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from qharness.exception import LoopExecutionError
from qharness.loop import Phase
from qharness.loop.config import LoopConfig
from qharness.loop.repository import LoopRepository, digest
from qharness.run import RunService
from qharness.run.ownership import RunOwnership
from qharness.tools.base import Tool, ToolParameters, ToolExecutionPolicy, ToolPolicyOverride, ToolRuntimePolicy, ToolExecutionResult
from tests.loop.test_actor import call_response
from tests.support.task_flow import catalog, flow_runtime, handed_back, planned, successful_script, write


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, script=None, **kwargs):
        r = flow_runtime(script if script is not None else successful_script(investigate=False, repair=False), **kwargs)
        self.addCleanup(r.close)
        r.lifecycle = RunService(r.services)
        return r

    async def test_same_instance_reentry_does_not_release_existing_ownership(self):
        r = self.runtime()
        lock = r.lifecycle.ownership
        lock.acquire()
        try:
            with self.assertRaises(LoopExecutionError):
                lock.acquire()
            self.assertEqual(len(lock.handles), 2)
            other = RunOwnership(r.repository, r.context.workspace.root / ".")
            with self.assertRaises(LoopExecutionError):
                other.acquire()
        finally:
            lock.release()

    async def test_physical_workspace_lock_is_enforced_in_another_process(self):
        r = self.runtime()
        r.lifecycle.ownership.acquire()
        script = """
import sys
from qharness.persistence import DatabaseConfig, DatabaseManager
from qharness.loop.repository import LoopRepository
from qharness.run.ownership import RunOwnership
from qharness.exception import LoopExecutionError
db = DatabaseManager(DatabaseConfig(url=sys.argv[1]))
repo = LoopRepository(db.session_factory, tenant_id='tenant', workspace_id='workspace', run_id='other-run')
lock = RunOwnership(repo, sys.argv[2])
try:
    lock.acquire()
except LoopExecutionError as error:
    print(error.code)
else:
    lock.release()
    raise AssertionError('second owner entered')
finally:
    db.close()
"""
        try:
            child = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", script,
                str(r.database.engine.url), str(r.context.workspace.root)], capture_output=True, text=True, timeout=15)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertIn("ownership_busy", child.stdout)
        finally:
            r.lifecycle.ownership.release()

    async def test_timeout_retains_lock_until_sync_handler_really_finishes(self):
        entered, release = threading.Event(), threading.Event()
        policy = ToolExecutionPolicy(defaults=ToolRuntimePolicy(max_calls=100),
                                     tool_overrides={"slow": ToolPolicyOverride(timeout_seconds=0.05)})
        r = self.runtime([planned(), call_response("slow")], policy=policy)
        calls = []
        def slow():
            entered.set()
            release.wait(5)
            calls.append("finished")
            return {"written": True}
        r.registry.register(Tool("slow", "同步执行故障注入", ToolParameters, slow))
        task = asyncio.create_task(r.lifecycle.run(catalog()))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            for _ in range(100):
                if r.repository.snapshot()["state"].phase == Phase.WAITING:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(r.repository.snapshot()["state"].phase, Phase.WAITING)
            self.assertFalse(task.done())
            self.assertTrue(r.executor._background)
            with self.assertRaises(LoopExecutionError):
                RunOwnership(r.repository, r.context.workspace.root).acquire()
            unknown = next(i for i in r.lifecycle.recovery() if i["kind"] == "tool" and i["status"] == "dispatched")
            with self.assertRaises(LoopExecutionError):
                await r.lifecycle.recovery_controller.observe("tool", unknown["call_id"], resolution="completed",
                    description="执行还没有结束", quiescent=True,
                    result=ToolExecutionResult(unknown["call_id"], "slow", True, "guess", 0))
        finally:
            release.set()
            await task
        self.assertEqual(calls, ["finished"])
        proof = await r.lifecycle.recovery_controller.observe("tool", unknown["call_id"], resolution="completed",
            description="后台线程已结束并检查实际结果", quiescent=True,
            result=ToolExecutionResult(unknown["call_id"], "slow", True, "finished", 0, data={"written": True}))
        await r.lifecycle.recovery_controller.reconcile(proof)
        self.assertFalse(r.repository.unresolved_calls())

    async def test_unknown_model_can_be_abandoned_without_refunding_tokens(self):
        r = self.runtime([planned(), TimeoutError(), write("pagination.py", "fixed"), handed_back(),
                          planned(), write("validation.py", "validated"), handed_back()])
        waiting = await r.lifecycle.run(catalog())
        self.assertEqual(waiting.resume_phase, Phase.ACTING)
        before = r.repository.snapshot()["budget"]
        unknown = next(i for i in r.lifecycle.recovery() if i["status"] == "dispatched")
        proof = await r.lifecycle.recovery_controller.observe("model", unknown["call_id"], resolution="abandoned",
            description="请求已断开，应用明确放弃丢失响应并保留费用预留", quiescent=True)
        await r.lifecycle.recovery_controller.reconcile(proof)
        self.assertEqual(before, r.repository.snapshot()["budget"])
        await r.lifecycle.submit("resume", "resume")
        result = await r.lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.COMPLETED)
        self.assertGreater(r.repository.snapshot()["budget"]["reserved_tokens"], 0)
        r.backend.assert_exhausted()

    async def test_crash_after_file_write_before_commit_records_partial_effect_without_replay(self):
        class ProcessLost(BaseException):
            pass
        r = self.runtime([planned(), write("pagination.py", "partial"), write("pagination.py", "fixed"), handed_back(),
                          planned(), write("validation.py", "validated"), handed_back()])
        with patch.object(r.context.mutation_service.version_store, "commit_changes", side_effect=ProcessLost()):
            with self.assertRaises(ProcessLost):
                await r.lifecycle.run(catalog())
        self.assertEqual((r.context.workspace.root / "pagination.py").read_text(), "partial")
        unknown = next(i for i in r.lifecycle.recovery() if i["kind"] == "tool" and i["status"] == "dispatched")
        self.assertEqual(r.context.mutation_service.inspect(unknown["operation_id"]).status, "pending")
        result = ToolExecutionResult(unknown["call_id"], "write_file", False, "文件已写入但 Commit 未确认", 0,
                                     error_code="recovered_partial", data={"path": "pagination.py", "partial": True})
        proof = await r.lifecycle.recovery_controller.observe("tool", unknown["call_id"], resolution="partial",
            description="已确认执行停止，保留部分文件效果，下一动作必须检查/修复", quiescent=True, result=result)
        await r.lifecycle.recovery_controller.reconcile(proof)
        # 沿用原历史仓储登记这次未完成操作，保留真实文件，不伪造 APPLIED Commit。
        r.context.mutation_service.history_repository.fail_operation(unknown["operation_id"], "恢复核对：部分效果")
        finished = await r.lifecycle.run(catalog())
        self.assertEqual(finished.phase, Phase.COMPLETED)
        self.assertEqual((r.context.workspace.root / "pagination.py").read_text(), "fixed")
        self.assertEqual(r.repository.snapshot()["budget"]["tool_executions"], 9)

    async def test_recovery_proof_is_invalid_after_workspace_change(self):
        r = self.runtime()
        with patch.object(r.services.repository, "finish_tool", side_effect=LoopExecutionError("injected", code="storage_error")):
            with self.assertRaises(LoopExecutionError):
                await r.lifecycle.run(catalog())
        unknown = next(i for i in r.lifecycle.recovery() if i["kind"] == "tool" and i["status"] == "dispatched")
        proof = await r.lifecycle.recovery_controller.observe("tool", unknown["call_id"], resolution="completed",
            description="已检查执行结果", quiescent=True,
            result=ToolExecutionResult(unknown["call_id"], "write_file", True, "fixed", 0))
        (r.context.workspace.root / "pagination.py").write_text("edited-again")
        with self.assertRaises(LoopExecutionError) as error:
            await r.lifecycle.recovery_controller.reconcile(proof)
        self.assertEqual(error.exception.code, "stale_evidence")
        self.assertTrue(r.repository.unresolved_calls())

    async def test_context_artifact_read_is_run_scoped_and_paged(self):
        r = self.runtime([planned(), call_response("read_run_artifact", arguments=json.dumps({
            "artifact_ref": digest(["large"]), "start": 0, "max_chars": 10})), handed_back(status="blocked")])
        r.repository.put_artifact(digest(["large"]), {"data": "source" * 100})
        result = await r.lifecycle.run(catalog())
        self.assertEqual(result.phase, Phase.WAITING)
        call = next(i for i in r.lifecycle.recovery() if i["kind"] == "tool")
        output = r.repository.tool_result(call["call_id"])
        self.assertTrue(output.data["has_more"])
        self.assertEqual(output.data["next_start"], 10)
        other = LoopRepository(r.database.session_factory, tenant_id="other", workspace_id="workspace", run_id=r.state.run_id)
        other.create(r.state.contract, r.config, r.policy, state=r.state)
        with self.assertRaises(LoopExecutionError):
            other.read_artifact(digest(["large"]))


class SandboxCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_taskkill_failure_falls_back_to_parent_kill(self):
        from qharness.sandbox.srt import _terminate_process_tree
        process = Mock(pid=123, returncode=None)
        killer = Mock(returncode=1)
        killer.wait = AsyncMock(return_value=1)
        with patch("qharness.sandbox.srt.os.name", "nt"), patch("qharness.sandbox.srt.asyncio.create_subprocess_exec", AsyncMock(return_value=killer)) as start:
            await _terminate_process_tree(process)
        self.assertEqual(start.call_args.args[:5], ("taskkill.exe", "/PID", "123", "/T", "/F"))
        process.kill.assert_called_once()
