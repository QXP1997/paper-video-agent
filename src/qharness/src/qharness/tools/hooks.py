# -*- coding: utf-8 -*-
"""工具执行生命周期 Hook。"""

from __future__ import annotations

from dataclasses import dataclass

from qharness.tools.base import Tool, ToolExecutionRequest, ToolExecutionResult


@dataclass(frozen=True, slots=True)
class ToolHookDecision:
    """前置 Hook 对当前工具调用给出的执行决定。"""

    allowed: bool
    reason: str | None = None

    @classmethod
    def allow(cls) -> ToolHookDecision:
        """明确允许当前工具调用。"""

        return cls(allowed=True)

    @classmethod
    def reject(cls, reason: str) -> ToolHookDecision:
        """拒绝当前工具调用并说明原因。"""

        return cls(allowed=False, reason=reason)


class ToolExecutionHook:
    """工具执行生命周期扩展点；子类只需覆盖关心的方法。"""

    async def before_execute(
        self,
        request: ToolExecutionRequest,
        tool: Tool,
    ) -> ToolHookDecision | None:
        """参数校验后、工具执行前调用，可用于审批或策略拦截。"""

        return None

    async def after_execute(
        self,
        request: ToolExecutionRequest,
        tool: Tool,
        result: ToolExecutionResult,
    ) -> None:
        """工具成功执行后调用，可用于审计、日志和指标记录。"""

    async def on_error(
        self,
        request: ToolExecutionRequest,
        tool: Tool | None,
        result: ToolExecutionResult,
    ) -> None:
        """工具执行失败后调用；Hook 异常不会覆盖原始执行错误。"""
