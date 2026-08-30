# -*- coding: utf-8 -*-
"""模型 Backend 的抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from qharness.model.models import ChatRequest, ChatResponse, ChatStreamEvent


class ModelBackend(ABC):
    """所有模型 Provider Adapter 必须实现的统一接口。"""

    @abstractmethod
    async def complete(self, request: ChatRequest) -> ChatResponse:
        """发起非流式调用并返回完整结果。"""

    @abstractmethod
    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        """发起流式调用并逐个返回标准事件。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭底层网络资源。"""

