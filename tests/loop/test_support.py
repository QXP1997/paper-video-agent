import json
import unittest

from tests.support.scripted_backend import ScriptedModelBackend
from tests.support.workspaces import ScriptedTool, pagination_workspace
from qharness.exception import ModelBackendError
from qharness.model.models import ChatMessage, ChatRequest, ChatResponse, ChatStreamEvent, ModelEventType
from qharness.tools.base import ToolExecutionRequest, ToolExecutionState
from qharness.tools.executor import ToolExecutor
from qharness.tools.registry import ToolRegistry


class SupportTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_records_requests_and_enforces_script(self):
        response = ChatResponse(ChatMessage("assistant", "调查"), "stop", None, "scripted", "r1")
        backend = ScriptedModelBackend((response, ModelBackendError("暂时失败", provider="scripted", retryable=True)))
        request = ChatRequest(messages=[ChatMessage("user", "修复分页")])
        self.assertEqual(await backend.complete(request), response)
        request.messages.clear()
        self.assertEqual(backend.requests[0].messages[0].content, "修复分页")
        with self.assertRaisesRegex(ModelBackendError, "暂时失败"):
            await backend.complete(request)
        backend.assert_exhausted()
        with self.assertRaisesRegex(AssertionError, "耗尽"):
            await backend.complete(request)
        await backend.close()
        with self.assertRaisesRegex(AssertionError, "关闭"):
            await backend.complete(request)

    async def test_script_preserves_real_stream_completion_event(self):
        response = ChatResponse(ChatMessage("assistant", "阶段完成"), "stop", None, "scripted", "r1")
        event = ChatStreamEvent(ModelEventType.RESPONSE_COMPLETED, response=response)
        backend = ScriptedModelBackend(((event,),))
        events = [e async for e in backend.stream(ChatRequest(messages=[]))]
        self.assertEqual(events[0].response, response)
        self.assertEqual(events[0].type, ModelEventType.RESPONSE_COMPLETED)
        backend.assert_exhausted()

    async def test_partial_stream_does_not_invent_completion(self):
        delta = ChatStreamEvent(ModelEventType.TOOL_CALL_DELTA, tool_arguments_delta='{"path":')
        backend = ScriptedModelBackend(((delta,), (delta, RuntimeError("断流"))))
        request = ChatRequest(messages=[])
        events = [e async for e in backend.stream(request)]
        self.assertEqual(events, [delta])
        with self.assertRaisesRegex(RuntimeError, "断流"):
            async for _ in backend.stream(request):
                pass
        backend.assert_exhausted()

    async def test_controlled_tool_uses_existing_validation_results_and_counter(self):
        scripted = ScriptedTool(({"exit_code": 1, "stderr": "assertion failed"}, RuntimeError("检查器崩溃")))
        registry = ToolRegistry()
        registry.register(scripted.as_tool())
        executor = ToolExecutor(registry)
        state = ToolExecutionState()
        def request(call_id, arguments):
            return ToolExecutionRequest(call_id=call_id, tool_name="fixture_check", raw_arguments=arguments)
        invalid = await executor.execute(request("a0", {"unexpected": True}), state)
        self.assertFalse(invalid.success)
        self.assertEqual(scripted.calls, 0)
        failure = await executor.execute(request("a1", {}), state)
        self.assertTrue(failure.success)  # 调用成功并不表示业务检查通过。
        self.assertEqual(json.loads(failure.content)["exit_code"], 1)
        error = await executor.execute(request("a2", {}), state)
        self.assertFalse(error.success)
        self.assertEqual(state.total_calls, 3)
        self.assertEqual(scripted.calls, 2)

    async def test_fixture_uses_real_workspace_context(self):
        with pagination_workspace() as workspace:
            self.assertIn("start = page * size", workspace.resolve_file("pagination.py").read_text())
            path = workspace.root
        self.assertFalse(path.exists())
