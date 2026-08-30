# -*- coding: utf-8 -*-
"""基于 OpenAI Python SDK 的 OpenAI-compatible 模型 Backend。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import openai
from openai import AsyncOpenAI

from qharness.backends.base import ModelBackend
from qharness.model.config import ModelBackendConfig
from qharness.exception.error import ModelBackendError
from qharness.model.models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    FunctionCall,
    ModelEventType,
    ToolCall,
    Usage,
)


class OpenAICompatibleBackend(ModelBackend):
    """调用 OpenAI-compatible Chat Completions API。

    首版重点兼容 DeepSeek，同时保留通用 OpenAI-compatible 接口所需的
    消息、流式输出、工具调用和 Provider 专属 extra_body。
    """

    def __init__(self, config: ModelBackendConfig) -> None:
        self.config = config
        self.client = AsyncOpenAI(
            api_key=config.require_api_key(),
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )

    async def complete(self, request: ChatRequest) -> ChatResponse:
        """执行一次非流式 Chat Completions 调用。"""

        try:
            response = await self.client.chat.completions.create(
                **self._build_request_arguments(request),
                stream=False,
            )
        except openai.OpenAIError as error:
            raise self._map_error(error) from error

        if not response.choices:
            raise ModelBackendError(
                "模型响应中没有 choices。",
                provider=self.config.provider,
                retryable=False,
            )

        choice = response.choices[0]
        message = self._map_message(choice.message)
        usage = self._map_usage(response.usage)
        return ChatResponse(
            message=message,
            finish_reason=choice.finish_reason,
            usage=usage,
            model=response.model,
            response_id=response.id,
            raw=response.model_dump(mode="json"),
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        """执行一次流式调用，并聚合出最终的完整响应事件。"""

        arguments = self._build_request_arguments(request)
        arguments["stream"] = True
        if self.config.stream_include_usage:
            arguments["stream_options"] = {"include_usage": True}

        try:
            stream = await self.client.chat.completions.create(**arguments)
        except openai.OpenAIError as error:
            raise self._map_error(error) from error

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_parts: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        usage: Usage | None = None
        response_id: str | None = None
        response_model: str | None = None

        yield ChatStreamEvent(type=ModelEventType.RESPONSE_STARTED)

        try:
            async for chunk in stream:
                response_id = chunk.id or response_id
                response_model = chunk.model or response_model

                if chunk.usage is not None:
                    usage = self._map_usage(chunk.usage)
                    yield ChatStreamEvent(
                        type=ModelEventType.USAGE_UPDATED,
                        usage=usage,
                        raw=chunk.model_dump(mode="json"),
                    )

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta

                if delta.content:
                    text_parts.append(delta.content)
                    yield ChatStreamEvent(
                        type=ModelEventType.TEXT_DELTA,
                        text=delta.content,
                        raw=chunk.model_dump(mode="json"),
                    )

                # DeepSeek 在思考模式下通过 reasoning_content 返回推理增量。
                reasoning_delta = getattr(delta, "reasoning_content", None)
                if reasoning_delta:
                    reasoning_parts.append(reasoning_delta)
                    yield ChatStreamEvent(
                        type=ModelEventType.REASONING_DELTA,
                        reasoning=reasoning_delta,
                        raw=chunk.model_dump(mode="json"),
                    )

                for tool_delta in delta.tool_calls or []:
                    index = tool_delta.index
                    current = tool_call_parts.setdefault(
                        index,
                        {"id": "", "type": "function", "name": "", "arguments": ""},
                    )
                    if tool_delta.id:
                        current["id"] += tool_delta.id
                    if tool_delta.type:
                        current["type"] = tool_delta.type
                    if tool_delta.function is not None:
                        if tool_delta.function.name:
                            current["name"] += tool_delta.function.name
                        if tool_delta.function.arguments:
                            current["arguments"] += tool_delta.function.arguments

                    yield ChatStreamEvent(
                        type=ModelEventType.TOOL_CALL_DELTA,
                        tool_call_index=index,
                        tool_call_id=tool_delta.id,
                        tool_name=(
                            tool_delta.function.name
                            if tool_delta.function is not None
                            else None
                        ),
                        tool_arguments_delta=(
                            tool_delta.function.arguments
                            if tool_delta.function is not None
                            else None
                        ),
                        raw=chunk.model_dump(mode="json"),
                    )
        except openai.OpenAIError as error:
            raise self._map_error(error) from error

        tool_calls = [
            ToolCall(
                id=item["id"],
                type=item["type"],
                function=FunctionCall(
                    name=item["name"],
                    arguments=item["arguments"],
                ),
            )
            for _, item in sorted(tool_call_parts.items())
        ]
        final_message = ChatMessage(
            role="assistant",
            content="".join(text_parts) or None,
            reasoning_content="".join(reasoning_parts) or None,
            tool_calls=tool_calls,
        )
        final_response = ChatResponse(
            message=final_message,
            finish_reason=finish_reason,
            usage=usage,
            model=response_model,
            response_id=response_id,
        )
        yield ChatStreamEvent(
            type=ModelEventType.RESPONSE_COMPLETED,
            response=final_response,
            usage=usage,
        )

    async def close(self) -> None:
        """关闭 OpenAI SDK 内部维护的异步 HTTP 客户端。"""

        await self.client.close()

    def _build_request_arguments(self, request: ChatRequest) -> dict[str, Any]:
        """将 QHarness 请求转换为 Chat Completions 参数。"""

        arguments: dict[str, Any] = {
            "model": request.model or self.config.model,
            "messages": [message.to_api_dict() for message in request.messages],
        }

        temperature = (
            request.temperature
            if request.temperature is not None
            else self.config.temperature
        )
        max_tokens = (
            request.max_tokens
            if request.max_tokens is not None
            else self.config.max_tokens
        )
        if temperature is not None:
            arguments["temperature"] = temperature
        if max_tokens is not None:
            arguments["max_tokens"] = max_tokens

        if request.tools:
            arguments["tools"] = [tool.to_api_dict() for tool in request.tools]
            arguments["tool_choice"] = request.tool_choice or "auto"
        elif request.tool_choice is not None:
            arguments["tool_choice"] = request.tool_choice

        if request.response_format is not None:
            arguments["response_format"] = request.response_format

        reasoning_effort = request.reasoning_effort or self.config.reasoning_effort
        if reasoning_effort:
            arguments["reasoning_effort"] = reasoning_effort

        # 配置级参数先加载，请求级参数可以按调用覆盖配置。
        extra_body = dict(self.config.extra_body)
        extra_body.update(request.extra_body)
        thinking_mode = request.thinking_mode or self.config.thinking_mode
        if thinking_mode:
            extra_body["thinking"] = {"type": thinking_mode}
        if extra_body:
            arguments["extra_body"] = extra_body

        return arguments

    @staticmethod
    def _map_message(message: Any) -> ChatMessage:
        """保留内容、DeepSeek reasoning_content 和工具调用。"""

        tool_calls = [
            ToolCall(
                id=tool.id,
                type=tool.type,
                function=FunctionCall(
                    name=tool.function.name,
                    arguments=tool.function.arguments,
                ),
            )
            for tool in (message.tool_calls or [])
        ]
        return ChatMessage(
            role="assistant",
            content=message.content,
            reasoning_content=getattr(message, "reasoning_content", None),
            tool_calls=tool_calls,
        )

    @staticmethod
    def _map_usage(usage: Any) -> Usage | None:
        """转换 Token 用量，同时保存 Provider 返回的完整原始字段。"""

        if usage is None:
            return None

        raw = usage.model_dump(mode="json")
        return Usage(
            prompt_tokens=int(raw.get("prompt_tokens", 0) or 0),
            completion_tokens=int(raw.get("completion_tokens", 0) or 0),
            total_tokens=int(raw.get("total_tokens", 0) or 0),
            raw=raw,
        )

    def _map_error(self, error: openai.OpenAIError) -> ModelBackendError:
        """将 OpenAI SDK 异常转换为 Kernel 可判断是否重试的错误。"""

        status_code = getattr(error, "status_code", None)
        retryable = isinstance(
            error,
            (
                openai.APIConnectionError,
                openai.APITimeoutError,
                openai.RateLimitError,
                openai.InternalServerError,
            ),
        )
        return ModelBackendError(
            f"{self.config.provider} 模型调用失败：{error}",
            provider=self.config.provider,
            retryable=retryable,
            status_code=status_code,
        )
