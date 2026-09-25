"""现有 ModelBackend 的离线替身；不会补全故意截断的流。"""

from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from copy import deepcopy

from qharness.backends.base import ModelBackend
from qharness.model.models import ChatRequest, ChatResponse, ChatStreamEvent


StreamScript = tuple[ChatStreamEvent | Exception, ...]


class ScriptedModelBackend(ModelBackend):
    supports_request_retry_control = True

    def __init__(self, script: Iterable[ChatResponse | StreamScript | Exception | Callable[[ChatRequest], ChatResponse]]) -> None:
        # 带 keyword-only 构造参数的生产异常不一定支持 deepcopy。
        self._script = deque(script)
        self.requests: list[ChatRequest] = []
        self.closed = False

    def _next(self, request: ChatRequest) -> ChatResponse | StreamScript:
        if self.closed:
            raise AssertionError("Backend 已关闭")
        self.requests.append(deepcopy(request))
        if not self._script:
            raise AssertionError("模型脚本已耗尽：发生了非预期的额外调用")
        step = self._script.popleft()
        if callable(step):
            step = step(deepcopy(request))  # 测试可根据控制器分配的 Attempt 身份生成固定角色输出。
        if isinstance(step, Exception):
            raise step
        return deepcopy(step) if isinstance(step, ChatResponse) else step

    async def complete(self, request: ChatRequest) -> ChatResponse:
        step = self._next(request)
        if not isinstance(step, ChatResponse):
            raise AssertionError("此脚本步骤要求 stream 调用")
        return step

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        step = self._next(request)
        if not isinstance(step, tuple):
            raise AssertionError("此脚本步骤要求 complete 调用")
        for event in step:
            if isinstance(event, Exception):
                raise event
            yield deepcopy(event)

    async def close(self) -> None:
        self.closed = True

    def assert_exhausted(self) -> None:
        if self._script:
            raise AssertionError(f"尚有 {len(self._script)} 个模型脚本步骤未执行")
