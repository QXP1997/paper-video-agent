import asyncio
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.loop.test_transitions import all_todos_passed, begin, outcome
from tests.support.loop_runtime import LoopRuntime
from tests.support.scripted_backend import ScriptedModelBackend
from tests.support.workspaces import stage
from qharness.backends.openai_compatible import OpenAICompatibleBackend
from qharness.exception import LoopExecutionError, ModelBackendError
from qharness.loop.config import LoopConfig, Role
from qharness.loop.context import ContextCompiler
from qharness.loop.model_service import ModelService
from qharness.loop.models import EvidenceLink, StageOutcome, StageVerdict, TaskVerdict
from qharness.model.config import ModelBackendConfig
from qharness.model.models import ChatMessage, ChatRequest, ChatResponse, FunctionCall, ToolCall, ToolDefinition, Usage


def response(value, *, finish_reason="stop", usage=True):
    content = value.model_dump_json() if hasattr(value, "model_dump_json") else value
    return ChatResponse(ChatMessage("assistant", content), finish_reason, Usage(7, 3, 10) if usage else None, "scripted", "R1")


class ModelServiceTests(unittest.IsolatedAsyncioTestCase):
    def setup_service(self, script, **kwargs):
        runtime = LoopRuntime(**kwargs)
        self.addCleanup(runtime.close)
        backend = ScriptedModelBackend(script)
        return runtime, backend, ModelService(backend, runtime.repository, ContextCompiler(runtime.config))

    async def test_planner_output_and_cached_reconstruction_record_usage_and_request(self):
        from tests.support.workspaces import pagination_run
        runtime, backend, service = self.setup_service([response(pagination_run().todo_plan)])
        first = await service.call("M1", Role.TODO_PLANNER)
        second = await service.call("M1", Role.TODO_PLANNER)
        self.assertEqual(first.output, runtime.state.todo_plan)
        self.assertTrue(second.cached)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(backend.requests[0].backend_max_retries, 0)
        budget = runtime.repository.snapshot()["budget"]
        self.assertEqual(budget["model_attempts"], 1)
        self.assertEqual(budget["used_tokens"], 10)
        self.assertEqual(budget["reserved_tokens"], 0)
        trace = runtime.repository.model_trace("M1")
        self.assertEqual(trace[0]["response"]["model"], "scripted")
        self.assertIn("output_schema", trace[0]["request"]["messages"][1]["content"])

    async def test_network_retries_are_counted_and_unknown_usage_is_not_free(self):
        runtime, backend, service = self.setup_service([
            ModelBackendError("网络失败", provider="scripted", retryable=True), response(stage()),
        ])
        result = await service.call("M1", Role.STAGE_PLANNER, todo_id="T1")
        self.assertEqual(result.output.todo_id, "T1")
        self.assertEqual(len(backend.requests), 2)
        self.assertEqual([t["status"] for t in runtime.repository.model_trace("M1")], ["failed", "done"])
        budget = runtime.repository.snapshot()["budget"]
        self.assertEqual(budget["model_attempts"], 2)
        self.assertGreater(budget["reserved_tokens"], 0)
        self.assertEqual(budget["used_tokens"], 10)

    async def test_actor_can_request_tools_then_hand_back_stage_output(self):
        state = begin()
        calls = ChatResponse(ChatMessage("assistant", tool_calls=[ToolCall("provider-id", FunctionCall("read_file", "{}"))]),
                             "tool_calls", Usage(1, 1, 2), "scripted", "R1")
        expected = StageOutcome(**state.attempts[-1].identity.model_dump(), status="candidate", summary="完成阶段")
        runtime, backend, service = self.setup_service([calls, response(expected)], state=state)
        result = await service.call("M1", Role.ACTOR, tools=[ToolDefinition("read_file", "读取", {"type": "object"})])
        self.assertIsNone(result.output)
        result = await service.call("M2", Role.ACTOR)
        self.assertEqual(result.output, expected)
        self.assertEqual(runtime.repository.snapshot()["state"], state)  # 角色响应不直接推进状态。

    async def test_stage_and_task_judge_have_distinct_schemas(self):
        state = outcome(begin())
        stage_verdict = StageVerdict(**state.attempts[-1].identity.model_dump(), stage_status="inconclusive",
                                    todo_status="inconclusive", expected_vs_observed="缺少检查结果")
        _, _, service = self.setup_service([response(stage_verdict)], state=state)
        self.assertEqual((await service.call("M1", Role.JUDGE)).output, stage_verdict)
        expected = TaskVerdict(contract_version=1, status="pass", summary="整体验收通过",
                              criterion_evidence=tuple(EvidenceLink(ref=c, evidence_refs=("E-final",)) for c in ("C1", "C2")))
        _, _, service = self.setup_service([response(expected)], state=all_todos_passed())
        self.assertEqual((await service.call("M2", Role.JUDGE, task_check=True)).output, expected)

    async def test_invalid_or_truncated_output_is_saved_but_not_accepted(self):
        for raw in (response("not json"), response(stage(), finish_reason="length")):
            runtime, backend, service = self.setup_service([raw])
            with self.assertRaises(LoopExecutionError) as error:
                await service.call("M1", Role.STAGE_PLANNER, todo_id="T1")
            self.assertEqual(error.exception.code, "protocol_error")
            self.assertIsNotNone(runtime.repository.model_trace("M1")[0]["response"])
            with self.assertRaises(LoopExecutionError):
                await service.call("M1", Role.STAGE_PLANNER, todo_id="T1")
            self.assertEqual(len(backend.requests), 1)

    async def test_cancelled_inflight_model_stays_unknown_and_is_not_replayed(self):
        runtime, backend, service = self.setup_service([])
        entered = asyncio.Event()
        async def wait_forever(request):
            entered.set()
            await asyncio.Event().wait()
        backend.complete = wait_forever
        cancellation = asyncio.Event()
        task = asyncio.create_task(service.call("M1", Role.TODO_PLANNER, cancellation_event=cancellation))
        await entered.wait()
        cancellation.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(runtime.repository.model_trace("M1")[0]["status"], "dispatched")
        with self.assertRaises(LoopExecutionError) as error:
            await service.call("M1", Role.TODO_PLANNER)
        self.assertEqual(error.exception.code, "unknown")

    async def test_budget_prevents_backend_call(self):
        runtime, backend, service = self.setup_service([], config=LoopConfig(max_total_tokens=1))
        with self.assertRaises(LoopExecutionError) as error:
            await service.call("M1", Role.TODO_PLANNER)
        self.assertEqual(error.exception.code, "budget_exceeded")
        self.assertFalse(backend.requests)

    async def test_database_wait_does_not_block_other_async_work(self):
        from tests.support.workspaces import pagination_run
        runtime, _, service = self.setup_service([response(pagination_run().todo_plan)])
        entered, release = threading.Event(), threading.Event()
        blocked_until_timeout = []
        original = runtime.repository.begin_model
        def waiting_database(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=2):
                blocked_until_timeout.append(True)
            return original(*args, **kwargs)
        with patch.object(runtime.repository, "begin_model", side_effect=waiting_database):
            task = asyncio.create_task(service.call("M1", Role.TODO_PLANNER))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                # 此协程必须能在账本仍等待时运行并释放它。
                self.assertFalse(blocked_until_timeout)
            finally:
                release.set()
                await task

    async def test_sdk_retry_override_is_per_request_and_standalone_default_survives(self):
        message = SimpleNamespace(role="assistant", content="ok", tool_calls=[], reasoning_content=None)
        raw = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                              usage=None, model="test", id="r", model_dump=lambda **kwargs: {})
        scoped = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=raw))))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=raw))),
                                 with_options=Mock(return_value=scoped))
        with patch("qharness.backends.openai_compatible.AsyncOpenAI", return_value=client) as constructor:
            backend = OpenAICompatibleBackend(ModelBackendConfig("test", "https://example.invalid", "test-key", "test", max_retries=2))
        await backend.complete(ChatRequest([ChatMessage("user", "hi")]))
        client.with_options.assert_not_called()
        await backend.complete(ChatRequest([ChatMessage("user", "hi")], backend_max_retries=0))
        client.with_options.assert_called_once_with(max_retries=0)
        self.assertEqual(constructor.call_args.kwargs["max_retries"], 2)
        self.assertEqual(backend.resolve_request(ChatRequest([])).model, "test")
