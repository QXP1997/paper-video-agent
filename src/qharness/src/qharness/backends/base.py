# -*- coding: utf-8 -*-
"""模型 Backend 的抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from collections.abc import AsyncIterator

from qharness.model.models import ChatRequest, ChatResponse, ChatStreamEvent


class ModelBackend(ABC):
    """所有模型 Provider Adapter 必须实现的统一接口。"""

    supports_request_retry_control: bool = False

    def resolve_request(self, request: ChatRequest) -> ChatRequest:
        """返回生效请求供预算与记录使用；Adapter 可补入自己的默认参数。"""
        return deepcopy(request)

    @abstractmethod
    async def complete(self, request: ChatRequest) -> ChatResponse:
        """发起非流式调用并返回完整结果。"""

    @abstractmethod
    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        """发起流式调用并逐个返回标准事件。"""

    @abstractmethod
    async def close(self) -> None:
        """关闭底层网络资源。"""
