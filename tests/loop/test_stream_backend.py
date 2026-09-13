import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from qharness.backends.openai_compatible import OpenAICompatibleBackend
from qharness.model.config import ModelBackendConfig
from qharness.model.models import ChatRequest, ModelEventType


class SdkStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def close(self):
        self.closed = True


def chunk(*, arguments, first=False, finish=None):
    tool = SimpleNamespace(index=0, id="call-id" if first else None, type="function" if first else None,
        function=SimpleNamespace(name="read_file" if first else None, arguments=arguments))
    delta = SimpleNamespace(content=None, reasoning_content="reason" if first else None, tool_calls=[tool])
    return SimpleNamespace(id="r", model="test", usage=None,
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)], model_dump=lambda **kwargs: {})


class StreamBackendTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, stream):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=stream))))
        with patch("qharness.backends.openai_compatible.AsyncOpenAI", return_value=client):
            return OpenAICompatibleBackend(ModelBackendConfig("test", "https://example.invalid", "key", "test"))

    async def test_existing_backend_assembles_fragments_and_closes_stream(self):
        stream = SdkStream([chunk(arguments='{"pa', first=True), chunk(arguments='th":"a.py"}', finish="tool_calls")])
        events = [event async for event in self.backend(stream).stream(ChatRequest([]))]
        response = events[-1].response
        self.assertEqual(events[-1].type, ModelEventType.RESPONSE_COMPLETED)
        self.assertEqual(response.message.tool_calls[0].function.arguments, '{"path":"a.py"}')
        self.assertEqual(response.message.reasoning_content, "reason")
        self.assertTrue(stream.closed)

    async def test_early_generator_close_releases_sdk_stream(self):
        stream = SdkStream([])
        generator = self.backend(stream).stream(ChatRequest([]))
        self.assertEqual((await anext(generator)).type, ModelEventType.RESPONSE_STARTED)
        await generator.aclose()
        self.assertTrue(stream.closed)
