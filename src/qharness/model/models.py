# -*- coding: utf-8 -*-
"""模型调用层使用的统一领域对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


MessageRole = Literal["system", "developer", "user", "assistant", "tool"]


class ModelEventType(StrEnum):
    """流式模型调用对外暴露的事件类型。"""

    RESPONSE_STARTED = "response.started"
    TEXT_DELTA = "text.delta"
    REASONING_DELTA = "reasoning.delta"
    TOOL_CALL_DELTA = "tool_call.delta"
    USAGE_UPDATED = "usage.updated"
    RESPONSE_COMPLETED = "response.completed"


@dataclass(slots=True)
class FunctionCall:
    """模型请求执行的函数名称和 JSON 参数文本。"""

    name: str
    arguments: str

    def to_api_dict(self) -> dict[str, Any]:
        """转换为 OpenAI Chat Completions 函数调用结构。"""

        return {"name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class ToolCall:
    """模型产生的一次工具调用。"""

    id: str
    function: FunctionCall
    type: str = "function"

    def to_api_dict(self) -> dict[str, Any]:
        """转换为可回填给模型的工具调用结构。"""

        return {
            "id": self.id,
            "type": self.type,
            "function": self.function.to_api_dict(),
        }


@dataclass(slots=True)
class ToolDefinition:
    """提供给模型的函数工具定义。"""

    name: str
    description: str
    parameters: dict[str, Any]
    strict: bool = False

    def to_api_dict(self) -> dict[str, Any]:
        """转换为 OpenAI-compatible Tool Schema。"""

        function: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        if self.strict:
            function["strict"] = True

        return {"type": "function", "function": function}


@dataclass(slots=True)
class ChatMessage:
    """QHarness 模型调用层的统一消息。"""

    role: MessageRole
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning_content: str | None = None

    def to_api_dict(self) -> dict[str, Any]:
        """转换为 Chat Completions 消息，并保留 DeepSeek 推理内容。"""

        message: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            message["name"] = self.name
        if self.tool_call_id:
            message["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            message["tool_calls"] = [item.to_api_dict() for item in self.tool_calls]
        if self.reasoning_content is not None:
            message["reasoning_content"] = self.reasoning_content
        return message


@dataclass(slots=True)
class ChatRequest:
    """一次 OpenAI-compatible 对话请求。"""

    messages: list[ChatMessage]
    tools: list[ToolDefinition] = field(default_factory=list)
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    thinking_mode: str | None = None
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Usage:
    """模型调用的 Token 使用量和 Provider 原始计量信息。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatResponse:
    """一次非流式调用或完整流式调用的统一结果。"""

    message: ChatMessage
    finish_reason: str | None
    usage: Usage | None
    model: str | None
    response_id: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatStreamEvent:
    """模型流式输出事件。"""

    type: ModelEventType
    text: str | None = None
    reasoning: str | None = None
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments_delta: str | None = None
    usage: Usage | None = None
    response: ChatResponse | None = None
    raw: dict[str, Any] = field(default_factory=dict)

