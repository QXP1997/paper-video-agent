# -*- coding: utf-8 -*-
"""工具运行时使用的核心领域对象。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from contextvars import ContextVar
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from qharness.model.models import ToolDefinition


ToolHandler = Callable[..., Any | Awaitable[Any]]
ToolParameterSchema = dict[str, Any] | type[BaseModel]


class ToolParameters(BaseModel):
    """本地 Python 工具参数的公共 Pydantic 基类。"""

    # 工具只能接收模型明确声明的参数，避免未声明字段进入 handler。
    model_config = ConfigDict(extra="forbid")


class ToolErrorCode(StrEnum):
    """工具执行失败时返回给上层的稳定错误码。"""

    UNKNOWN_TOOL = "unknown_tool"
    INVALID_ARGUMENTS = "invalid_arguments"
    CONFLICT = "conflict"
    LIMIT_EXCEEDED = "limit_exceeded"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    EXECUTION_FAILED = "execution_failed"
    HOOK_FAILED = "hook_failed"
    RESULT_TOO_LARGE = "result_too_large"


class ToolEffect(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Tool:
    """一个可注册、可被模型调用的本地工具。"""

    name: str
    description: str
    parameters: ToolParameterSchema
    handler: ToolHandler
    strict: bool = False
    requires_approval: bool = False
    # 由工具实现者声明，模型不能自行把写工具标记成只读。
    effect: ToolEffect = ToolEffect.UNKNOWN
    parallel_safe: bool = False

    def to_definition(self) -> ToolDefinition:
        """转换为模型调用层可以直接使用的工具定义。"""

        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.parameter_json_schema(),
            strict=self.strict,
        )

    def parameter_model(self) -> type[BaseModel] | None:
        """返回 Pydantic 参数模型；原始 JSON Schema 工具返回 ``None``。"""

        if isinstance(self.parameters, type) and issubclass(
            self.parameters,
            BaseModel,
        ):
            return self.parameters
        return None

    def parameter_json_schema(self) -> dict[str, Any]:
        """将两种参数声明统一转换为提供给模型的 JSON Schema。"""

        if isinstance(self.parameters, dict):
            return self.parameters

        parameter_model = self.parameter_model()
        if parameter_model is not None:
            return parameter_model.model_json_schema()

        raise TypeError(
            "工具 parameters 必须是 JSON Schema 字典或 Pydantic BaseModel 子类。"
        )


@dataclass(slots=True)
class ToolExecutionRequest:
    """一次工具执行请求及其调用上下文。"""

    call_id: str
    tool_name: str
    raw_arguments: str | Mapping[str, Any]
    arguments: dict[str, Any] | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    cancellation_event: asyncio.Event | None = None
    # 仅由可信调用方注入，不属于模型工具参数或 metadata。
    operation_id: str | None = None
    # 可信控制器注入，在取得并发额度后、进入 Handler 前再次核对派发条件。
    dispatch_guard: Any = None


_CURRENT_TOOL_REQUEST: ContextVar[ToolExecutionRequest | None] = ContextVar("qharness_tool_request", default=None)


def current_operation_id() -> str | None:
    """在 Handler 内读取执行器绑定的操作身份；to_thread 同样传播此上下文。"""

    request = _CURRENT_TOOL_REQUEST.get()
    return request.operation_id if request is not None else None


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """工具执行器返回的统一结果。"""

    call_id: str
    tool_name: str
    success: bool
    content: str
    elapsed_seconds: float
    error_code: str | None = None
    truncated: bool = False
    warnings: tuple[str, ...] = ()
    # 与 content 的模型摘要分离，保留完整 JSON 业务结果。
    data: Any = None
    artifact_id: str | None = None

    def to_model_content(self) -> str:
        """生成适合作为 tool 消息回填给模型的文本。"""

        if self.success:
            return self.content

        return json.dumps(
            {
                "error": {
                    "code": self.error_code,
                    "message": self.content,
                }
            },
            ensure_ascii=False,
        )


@dataclass(frozen=True, slots=True)
class ToolRuntimePolicy:
    """单个工具最终生效的资源与审批策略。"""

    max_calls: int = 10
    timeout_seconds: float = 30.0
    max_concurrency: int | None = None
    max_argument_chars: int = 64_000
    max_result_chars: int = 200_000
    truncate_oversized_results: bool = True
    requires_approval: bool = False

    def __post_init__(self) -> None:
        """校验单工具策略中的类型、零值和负数。"""

        integer_limits: dict[str, int | None] = {
            "max_calls": self.max_calls,
            "max_concurrency": self.max_concurrency,
            "max_argument_chars": self.max_argument_chars,
            "max_result_chars": self.max_result_chars,
        }
        for name, value in integer_limits.items():
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} 必须是整数。")
            if value <= 0:
                raise ValueError(f"{name} 必须大于 0。")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds,
            (int, float),
        ):
            raise ValueError("timeout_seconds 必须是数字。")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0。")
        if not isinstance(self.truncate_oversized_results, bool):
            raise ValueError("truncate_oversized_results 必须是布尔值。")
        if not isinstance(self.requires_approval, bool):
            raise ValueError("requires_approval 必须是布尔值。")


@dataclass(frozen=True, slots=True)
class ToolPolicyOverride:
    """配置文件中针对某个工具声明的可选覆盖项。"""

    max_calls: int | None = None
    timeout_seconds: float | None = None
    max_concurrency: int | None = None
    max_argument_chars: int | None = None
    max_result_chars: int | None = None
    truncate_oversized_results: bool | None = None
    requires_approval: bool | None = None

    def apply(self, default: ToolRuntimePolicy) -> ToolRuntimePolicy:
        """将非空覆盖项合并到默认策略并返回新对象。"""

        return ToolRuntimePolicy(
            max_calls=(
                self.max_calls if self.max_calls is not None else default.max_calls
            ),
            timeout_seconds=(
                self.timeout_seconds
                if self.timeout_seconds is not None
                else default.timeout_seconds
            ),
            max_concurrency=(
                self.max_concurrency
                if self.max_concurrency is not None
                else default.max_concurrency
            ),
            max_argument_chars=(
                self.max_argument_chars
                if self.max_argument_chars is not None
                else default.max_argument_chars
            ),
            max_result_chars=(
                self.max_result_chars
                if self.max_result_chars is not None
                else default.max_result_chars
            ),
            truncate_oversized_results=(
                self.truncate_oversized_results
                if self.truncate_oversized_results is not None
                else default.truncate_oversized_results
            ),
            requires_approval=(
                self.requires_approval
                if self.requires_approval is not None
                else default.requires_approval
            ),
        )


@dataclass(frozen=True, slots=True)
class ToolExecutionPolicy:
    """全局执行限制、默认工具策略和按工具名称覆盖的集合。"""

    max_total_calls: int | None = None
    max_concurrency: int | None = None
    defaults: ToolRuntimePolicy = field(default_factory=ToolRuntimePolicy)
    tool_overrides: Mapping[str, ToolPolicyOverride] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """校验仅适用于整个 Agent Run 的全局限制。"""

        global_limits = {
            "max_total_calls": self.max_total_calls,
            "max_concurrency": self.max_concurrency,
        }
        for name, value in global_limits.items():
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} 必须是整数。")
            if value <= 0:
                raise ValueError(f"{name} 必须大于 0。")

        for tool_name, override in self.tool_overrides.items():
            if not isinstance(tool_name, str) or not tool_name:
                raise ValueError("tool_overrides 的工具名称不能为空。")
            if not isinstance(override, ToolPolicyOverride):
                raise ValueError(f"工具 {tool_name} 的覆盖配置类型不正确。")
            # 提前合并一次，确保覆盖项的值在启动阶段就完成校验。
            override.apply(self.defaults)

    def for_tool(self, tool_name: str) -> ToolRuntimePolicy:
        """按工具名称解析最终策略；未配置时返回全局默认策略。"""

        override = self.tool_overrides.get(tool_name)
        return override.apply(self.defaults) if override else self.defaults


@dataclass(slots=True)
class ToolExecutionState:
    """一次 Agent Run 内共享的工具调用计数状态。"""

    total_calls: int = 0
    calls_by_tool: dict[str, int] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def reserve(
        self,
        tool_name: str,
        *,
        max_total_calls: int | None,
        max_calls_for_tool: int,
    ) -> tuple[bool, str | None]:
        """以并发安全的方式为一次调用预留计数额度。"""

        async with self._lock:
            if (
                max_total_calls is not None
                and self.total_calls >= max_total_calls
            ):
                return False, f"工具调用总次数已达到上限 {max_total_calls}。"

            current = self.calls_by_tool.get(tool_name, 0)
            if current >= max_calls_for_tool:
                return (
                    False,
                    f"工具 {tool_name} 的调用次数已达到上限 {max_calls_for_tool}。",
                )

            # 无论参数或工具执行是否成功，本次模型发起的调用都消耗额度。
            self.total_calls += 1
            self.calls_by_tool[tool_name] = current + 1
            return True, None
