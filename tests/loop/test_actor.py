import asyncio
import json
import unittest
from dataclasses import replace

from tests.loop.test_transitions import begin
from tests.support.loop_runtime import LoopRuntime
from tests.support.scripted_backend import ScriptedModelBackend
from tests.support.workspaces import ScriptedTool
from qharness.exception import LoopExecutionError
from qharness.loop.actor import Actor
from qharness.loop.config import LoopConfig
from qharness.loop.context import ContextCompiler, validate_messages
from qharness.loop.model_service import ModelService
from qharness.loop.models import StageOutcome, Phase, Wait
from qharness.loop.tool_service import ToolService
from qharness.model.models import ChatMessage, ChatResponse, ChatStreamEvent, FunctionCall, ModelEventType, ToolCall, Usage
from qharness.tools.base import Tool, ToolParameters
from qharness.tools.builtin.read_file import create_read_file_tool
from qharness.tools.builtin.replace_text import create_replace_text_tool
from qharness.workspace import DulwichFileVersionStore, SqlAlchemyWorkspaceHistoryRepository, WorkspaceMutationService


def call_response(*names, arguments="{}"):
    # 有意在后续模型轮次重复 Provider call ID，验证历史命名隔离。
    return ChatResponse(ChatMessage("assistant", tool_calls=[ToolCall(f"c{i}", FunctionCall(name, arguments))
        for i, name in enumerate(names)], reasoning_content="provider reasoning"), "tool_calls", Usage(1, 1, 2), "scripted", "r")


def handback(state, **changes):
    data = dict(**state.attempts[-1].identity.model_dump(), status="candidate", summary="阶段候选结果已就绪",
                outputs=("fixture-output",), matched_stop_when=state.attempts[-1].plan.stop_when)
    data.update(changes)
    return ChatResponse(ChatMessage("assistant", StageOutcome(**data).model_dump_json()), "stop", Usage(1, 1, 2), "scripted", "r")


def streamed(response):
    return (ChatStreamEvent(ModelEventType.RESPONSE_STARTED),
            ChatStreamEvent(ModelEventType.RESPONSE_COMPLETED, response=response))


class ActorTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, *, config=None):
        runtime = LoopRuntime(state=begin(), config=config)
        self.addCleanup(runtime.close)
        return runtime

    def actor(self, runtime, script):
        backend = ScriptedModelBackend(script)
        actor = Actor(ModelService(backend, runtime.repository, ContextCompiler(runtime.config)),
                      ToolService(runtime.executor, runtime.context, runtime.repository))
        return actor, backend

    async def test_given_plan_starts_attempt_and_hands_back_without_verifying_todo(self):
        runtime = LoopRuntime()
        self.addCleanup(runtime.close)
        predicted = begin(runtime.state)
        actor, _ = self.actor(runtime, [handback(predicted)])
        result = await actor.run(predicted.attempts[-1].plan, attempt_id="A1")
        self.assertEqual(result.attempt_id, "A1")
        self.assertEqual(runtime.repository.snapshot()["state"].phase, "verifying")

    async def test_multiple_tool_turns_and_repeated_provider_ids_pair_correctly(self):
        runtime = self.runtime()
        tool = ScriptedTool([{"page": 1}, {"page": 2}])
        runtime.registry.register(tool.as_tool())
        actor, backend = self.actor(runtime, [call_response("fixture_check"), call_response("fixture_check"), handback(runtime.state)])
        result = await actor.run()
        self.assertEqual(result.status, "candidate")
        self.assertEqual(tool.calls, 2)
        history = backend.requests[-1].messages[2:]
        validate_messages(history)
        self.assertNotEqual(history[0].tool_calls[0].id, history[2].tool_calls[0].id)
        self.assertEqual(history[0].reasoning_content, "provider reasoning")
        state = runtime.repository.snapshot()["state"]
        self.assertEqual(state.phase, Phase.VERIFYING)
        self.assertNotEqual(state.todos[0].status, "passed")
        self.assertFalse(state.task_verdicts)
        self.assertEqual(await actor.run(), result)
        backend.assert_exhausted()

    async def test_malformed_arguments_never_enter_handler_and_can_be_corrected(self):
        runtime = self.runtime()
        tool = ScriptedTool([{"ok": True}])
        runtime.registry.register(tool.as_tool())
        actor, backend = self.actor(runtime, [call_response("fixture_check", arguments='{"unfinished":'),
            call_response("fixture_check"), handback(runtime.state)])
        await actor.run()
        self.assertEqual(tool.calls, 1)
        self.assertIn("invalid_arguments", backend.requests[1].messages[-1].content)
        self.assertEqual(runtime.repository.snapshot()["budget"]["tool_calls"], 2)

    async def test_partial_stream_is_discarded_before_any_tools_execute(self):
        runtime = self.runtime()
        tool = ScriptedTool([{"ok": True}])
        runtime.registry.register(tool.as_tool())
        partial = (ChatStreamEvent(ModelEventType.TOOL_CALL_DELTA, tool_call_id="c0", tool_name="fixture_check", tool_arguments_delta="{"),)
        actor, backend = self.actor(runtime, [partial, streamed(call_response("fixture_check")), streamed(handback(runtime.state))])
        result = await actor.run(stream=True)
        self.assertEqual(result.status, "candidate")
        self.assertEqual(tool.calls, 1)
        self.assertEqual(runtime.repository.snapshot()["budget"]["model_attempts"], 3)
        self.assertGreater(runtime.repository.snapshot()["budget"]["reserved_tokens"], 0)

    async def test_stream_post_completion_data_is_rejected(self):
        runtime = self.runtime(config=LoopConfig(max_protocol_corrections=0))
        tool = ScriptedTool([])
        runtime.registry.register(tool.as_tool())
        script = (*streamed(call_response("fixture_check")), ChatStreamEvent(ModelEventType.TEXT_DELTA, text="late"))
        actor, _ = self.actor(runtime, [script])
        result = await actor.run(stream=True)
        self.assertEqual(result.status, "stalled")
        self.assertEqual(tool.calls, 0)

    async def test_protocol_and_stop_condition_correction_does_not_lower_stage_goal(self):
        runtime = self.runtime()
        malformed = ChatResponse(ChatMessage("assistant", "not json"), "stop", None, "scripted", "r")
        bad_condition = handback(runtime.state, matched_stop_when=("伪造较低的完成条件",))
        actor, backend = self.actor(runtime, [malformed, bad_condition, handback(runtime.state)])
        result = await actor.run()
        self.assertEqual(result.status, "candidate")
        self.assertEqual(runtime.repository.snapshot()["state"].attempts[-1].plan, runtime.state.attempts[-1].plan)
        self.assertEqual(len(backend.requests), 3)

    async def test_changed_assumption_requires_replan_and_stops_tools(self):
        runtime = self.runtime()
        result = handback(runtime.state, status="needs_replan", matched_stop_when=(),
                         matched_replan_when=runtime.state.attempts[-1].plan.replan_when,
                         reported_assumption_changes=("任务输入与预期不符",))
        invalid_candidate = handback(runtime.state, reported_assumption_changes=("任务输入与预期不符",))
        actor, backend = self.actor(runtime, [invalid_candidate, result])
        self.assertEqual((await actor.run()).status, "needs_replan")
        self.assertEqual(runtime.repository.snapshot()["budget"]["tool_calls"], 0)
        backend.assert_exhausted()

    async def test_inflight_cancellation_stops_remaining_batch_and_hands_back_unknown_effect(self):
        runtime = self.runtime()
        entered = asyncio.Event()
        async def slow():
            entered.set()
            await asyncio.Event().wait()
        writing = ScriptedTool([])
        runtime.registry.register(Tool("slow", "可取消工具", ToolParameters, slow))
        runtime.registry.register(writing.as_tool("write"))
        actor, backend = self.actor(runtime, [call_response("slow", "write")])
        task = asyncio.create_task(actor.run())
        await entered.wait()
        runtime.context.cancel()
        self.assertEqual((await task).status, "blocked")
        self.assertEqual(writing.calls, 0)
        self.assertEqual(runtime.repository.snapshot()["budget"]["tool_calls"], 1)
        backend.assert_exhausted()

    async def test_oversized_tool_batch_is_rejected_before_admission(self):
        runtime = self.runtime(config=LoopConfig(max_batch_calls=1, max_protocol_corrections=0))
        tool = ScriptedTool([])
        runtime.registry.register(tool.as_tool())
        actor, _ = self.actor(runtime, [call_response("fixture_check", "fixture_check")])
        self.assertEqual((await actor.run()).status, "stalled")
        self.assertEqual(runtime.repository.snapshot()["budget"]["tool_calls"], 0)

    async def test_actor_supports_explicit_blocked_and_stalled(self):
        for status in ("blocked", "stalled"):
            runtime = self.runtime()
            actor, _ = self.actor(runtime, [handback(runtime.state, status=status, matched_stop_when=())])
            self.assertEqual((await actor.run()).status, status)

    async def test_business_failure_skips_dependent_actions_and_allows_next_turn_repair(self):
        runtime = self.runtime()
        failing = ScriptedTool([{"exit_code": 1}])
        writing = ScriptedTool([{"ok": True}])
        runtime.registry.register(failing.as_tool("check"))
        runtime.registry.register(writing.as_tool("write"))
        actor, backend = self.actor(runtime, [call_response("check", "write"), call_response("write"), handback(runtime.state)])
        await actor.run()
        self.assertEqual(writing.calls, 1)
        self.assertIn("skipped", backend.requests[1].messages[-1].content)
        validate_messages(backend.requests[1].messages[2:])

    async def test_budget_and_approval_stop_without_false_completion(self):
        runtime = self.runtime(config=LoopConfig(max_total_tokens=1))
        actor, backend = self.actor(runtime, [])
        self.assertEqual((await actor.run()).status, "blocked")
        self.assertFalse(backend.requests)
        runtime = self.runtime()
        tool = ScriptedTool([])
        runtime.registry.register(replace(tool.as_tool(), requires_approval=True))
        actor, _ = self.actor(runtime, [call_response("fixture_check")])
        self.assertEqual((await actor.run()).status, "blocked")
        self.assertEqual(tool.calls, 0)

    async def test_context_cancellation_prevents_first_call(self):
        runtime = self.runtime()
        runtime.context.cancel()
        actor, backend = self.actor(runtime, [])
        self.assertEqual((await actor.run()).status, "blocked")
        self.assertFalse(backend.requests)

    async def test_turn_limit_protects_against_unbounded_tool_loop(self):
        runtime = self.runtime(config=LoopConfig(max_actor_turns=2))
        runtime.registry.register(ScriptedTool([{}, {}]).as_tool())
        actor, backend = self.actor(runtime, [call_response("fixture_check"), call_response("fixture_check")])
        self.assertEqual((await actor.run()).status, "stalled")
        self.assertEqual(len(backend.requests), 2)

    async def test_changed_state_after_model_response_blocks_old_tools(self):
        runtime = self.runtime()
        scripted = ScriptedTool([])
        runtime.registry.register(scripted.as_tool())
        actor, backend = self.actor(runtime, [])
        async def change_state(request):
            runtime.repository.apply(Wait(run_id=runtime.state.run_id, expected_version=runtime.state.version, reason="新输入"))
            return call_response("fixture_check")
        backend.complete = change_state
        with self.assertRaises(LoopExecutionError):
            await actor.run()
        self.assertEqual(scripted.calls, 0)
        self.assertEqual(runtime.repository.snapshot()["state"].phase, "waiting")

    async def test_reentry_uses_prior_completed_work_instead_of_resetting_turns(self):
        runtime = self.runtime()
        tool = ScriptedTool([{"ok": True}])
        runtime.registry.register(tool.as_tool())
        actor, _ = self.actor(runtime, [call_response("fixture_check"), handback(runtime.state)])
        original = actor.model.call
        count = 0
        async def crash_before_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("模拟进程在下一模型请求前停止")
            return await original(*args, **kwargs)
        actor.model.call = crash_before_second
        with self.assertRaises(RuntimeError):
            await actor.run()
        rebuilt, backend = self.actor(runtime, [handback(runtime.state)])
        self.assertEqual((await rebuilt.run()).status, "candidate")
        self.assertEqual(tool.calls, 1)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(runtime.repository.snapshot()["budget"]["model_attempts"], 2)

    async def test_real_file_read_modify_and_fixed_regression_check(self):
        runtime = self.runtime()
        source = runtime.context.workspace.root / "pagination.py"
        original = "def paginate(items, page, size):\n    start = page * size\n    return items[start:start + size]\n"
        repaired = original.replace("page * size", "(page - 1) * size")
        source.write_text(original, encoding="utf-8")
        history = SqlAlchemyWorkspaceHistoryRepository(runtime.database.session_factory, tenant_id="tenant", workspace_id="workspace")
        versions = DulwichFileVersionStore(runtime.root / "history", tenant_id="tenant", workspace_id="workspace")
        mutation = WorkspaceMutationService(runtime.context.workspace, history, versions, run_id=runtime.state.run_id)
        runtime.registry.register(create_read_file_tool(runtime.context.workspace))
        runtime.registry.register(create_replace_text_tool(mutation))
        checks = []
        def fixed_check():
            code = source.read_text(encoding="utf-8")
            # 只运行本测试固定的两个可信代码版本，不能用作生产代码沙箱。
            self.assertIn(code, (original, repaired))
            namespace = {}
            exec(code, namespace)
            function = namespace["paginate"]
            passed = function(list(range(5)), 1, 2) == [0, 1] and function(list(range(5)), 3, 2) == [4]
            checks.append(passed)
            return {"exit_code": 0 if passed else 1}
        runtime.registry.register(Tool("check_fixture", "检查固定分页样例", ToolParameters, fixed_check))
        actor, _ = self.actor(runtime, [call_response("read_file", arguments='{"path":"pagination.py"}'),
            call_response("check_fixture"), call_response("replace_text", arguments=json.dumps({
                "path": "pagination.py", "old_text": "page * size", "new_text": "(page - 1) * size"})),
            call_response("check_fixture"), handback(runtime.state)])
        self.assertEqual((await actor.run()).status, "candidate")
        self.assertEqual(checks, [False, True])
        self.assertEqual(source.read_text(), repaired)
