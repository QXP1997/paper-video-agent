# -*- coding: utf-8 -*-
"""带参数校验、资源限制和 Hook 的工具执行器。"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from qharness.exception.error import (
    ToolExecutionError,
    WorkspaceConflictError,
    WorkspacePathError,
)
from qharness.tools.base import (
    Tool,
    ToolErrorCode,
    ToolExecutionPolicy,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolExecutionState,
    ToolRuntimePolicy,
    _CURRENT_TOOL_REQUEST,
)
from qharness.tools.hooks import ToolExecutionHook
from qharness.tools.registry import ToolRegistry


class ToolExecutor:
    """统一执行注册工具，并在工具外部实施运行时控制。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        policy: ToolExecutionPolicy | None = None,
        hooks: list[ToolExecutionHook] | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy or ToolExecutionPolicy()
        self.hooks = list(hooks or [])
        self._semaphore = (
            asyncio.Semaphore(self.policy.max_concurrency)
            if self.policy.max_concurrency is not None
            else None
        )
        self._tool_semaphores: dict[str, asyncio.Semaphore] = {}

    async def execute(
        self,
        request: ToolExecutionRequest,
        state: ToolExecutionState,
    ) -> ToolExecutionResult:
        """执行一次工具调用，所有预期失败均转换为标准结果。"""

        rejected = await self.admit(request, state)
        if rejected is not None:
            return rejected
        return await self.execute_admitted(request)

    async def admit(
        self, request: ToolExecutionRequest, state: ToolExecutionState,
    ) -> ToolExecutionResult | None:
        """独立调用入口的内存额度准入；Loop 使用仓储的持久准入替代此步。"""

        started_at = time.perf_counter()
        tool = self.registry.get(request.tool_name)
        tool_policy = self.policy.for_tool(request.tool_name)
        reserved, reason = await state.reserve(
            request.tool_name,
            max_total_calls=self.policy.max_total_calls,
            max_calls_for_tool=tool_policy.max_calls,
        )
        if not reserved:
            return await self._failure(
                request,
                tool,
                ToolErrorCode.LIMIT_EXCEEDED,
                reason or "工具调用次数已达到上限。",
                started_at,
            )

        return None

    async def execute_admitted(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        """执行已经准入的调用；参数、Hook、并发、超时和取消仍全部生效。

        这是可信控制器接口，调用方必须先经 admit 或持久账本预留额度。
        """

        started_at = time.perf_counter()
        tool = self.registry.get(request.tool_name)
        tool_policy = self.policy.for_tool(request.tool_name)

        if tool is None:
            return await self._failure(
                request,
                None,
                ToolErrorCode.UNKNOWN_TOOL,
                f"未注册工具：{request.tool_name}",
                started_at,
            )

        try:
            request.arguments = self._parse_arguments(
                request.raw_arguments,
                max_argument_chars=tool_policy.max_argument_chars,
            )
            request.arguments = self._validate_arguments(tool, request.arguments)
            approved = await self._run_before_hooks(request, tool)
            requires_approval = (
                tool.requires_approval or tool_policy.requires_approval
            )
            if requires_approval and not approved:
                raise ToolExecutionError(
                    f"工具 {tool.name} 需要审批，但没有 Hook 明确允许执行。",
                    code=ToolErrorCode.REJECTED,
                )

            value = await self._run_with_controls(
                tool,
                request,
                tool_policy=tool_policy,
            )
            content = self._serialize_result(value)
            structured = json.loads(content) if not isinstance(value, str) else value
            try:
                content, truncated = self._limit_result(content, tool_policy)
            except ToolExecutionError as error:
                failure = await self._failure(request, tool, error.code, str(error), started_at)
                return replace(failure, data=structured, truncated=True)
            result = ToolExecutionResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=True,
                content=content,
                elapsed_seconds=time.perf_counter() - started_at,
                truncated=truncated,
                data=structured,
            )
            hook_warnings = await self._run_after_hooks(request, tool, result)
            if hook_warnings:
                # 工具可能已经产生外部副作用，后置 Hook 失败不能把成功调用
                # 改成失败，否则上层重试时可能重复执行同一副作用。
                result = replace(result, warnings=tuple(hook_warnings))
            return result
        except ToolExecutionError as error:
            return await self._failure(
                request,
                tool,
                error.code,
                str(error),
                started_at,
            )
        except WorkspacePathError as error:
            return await self._failure(
                request,
                tool,
                ToolErrorCode.INVALID_ARGUMENTS,
                str(error),
                started_at,
            )
        except WorkspaceConflictError as error:
            return await self._failure(
                request,
                tool,
                ToolErrorCode.CONFLICT,
                str(error),
                started_at,
            )
        except Exception as error:
            return await self._failure(
                request,
                tool,
                ToolErrorCode.EXECUTION_FAILED,
                f"工具 {tool.name} 执行失败：{error}",
                started_at,
            )

    def _parse_arguments(
        self,
        raw_arguments: str | Mapping[str, Any],
        *,
        max_argument_chars: int,
    ) -> dict[str, Any]:
        """限制参数体积，并解析模型生成的 JSON 对象。"""

        if isinstance(raw_arguments, str):
            serialized = raw_arguments
            if len(serialized) > max_argument_chars:
                raise ToolExecutionError(
                    "工具参数超过允许的最大长度。",
                    code=ToolErrorCode.INVALID_ARGUMENTS,
                )
            try:
                arguments = json.loads(serialized)
            except json.JSONDecodeError as error:
                raise ToolExecutionError(
                    f"工具参数不是合法 JSON：{error.msg}",
                    code=ToolErrorCode.INVALID_ARGUMENTS,
                ) from error
        else:
            arguments = dict(raw_arguments)
            serialized = json.dumps(arguments, ensure_ascii=False)
            if len(serialized) > max_argument_chars:
                raise ToolExecutionError(
                    "工具参数超过允许的最大长度。",
                    code=ToolErrorCode.INVALID_ARGUMENTS,
                )

        if not isinstance(arguments, dict):
            raise ToolExecutionError(
                "工具参数的顶层必须是 JSON 对象。",
                code=ToolErrorCode.INVALID_ARGUMENTS,
            )
        return arguments

    @staticmethod
    def _validate_arguments(
        tool: Tool,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """按参数声明类型校验参数，并返回提供给 handler 的标准字典。"""

        parameter_model = tool.parameter_model()
        if parameter_model is not None:
            try:
                validated = parameter_model.model_validate(arguments)
            except PydanticValidationError as error:
                details = error.errors(include_url=False)
                first_error = details[0] if details else {}
                location = ".".join(
                    str(item) for item in first_error.get("loc", ())
                )
                prefix = f"参数 {location} " if location else "工具参数 "
                message = str(first_error.get("msg", "校验失败"))
                raise ToolExecutionError(
                    f"{prefix}校验失败：{message}",
                    code=ToolErrorCode.INVALID_ARGUMENTS,
                ) from error

            # 使用 Python 模式保留 Path、Enum 等本地 handler 可能需要的对象。
            return validated.model_dump(mode="python")

        try:
            Draft202012Validator(tool.parameter_json_schema()).validate(arguments)
        except JsonSchemaValidationError as error:
            location = ".".join(str(item) for item in error.absolute_path)
            prefix = f"参数 {location} " if location else "工具参数 "
            raise ToolExecutionError(
                f"{prefix}校验失败：{error.message}",
                code=ToolErrorCode.INVALID_ARGUMENTS,
            ) from error
        return arguments

    async def _run_before_hooks(
        self,
        request: ToolExecutionRequest,
        tool: Tool,
    ) -> bool:
        """依次执行前置 Hook；任一拒绝都会终止工具执行。"""

        explicitly_approved = False
        for hook in self.hooks:
            try:
                decision = await hook.before_execute(request, tool)
            except Exception as error:
                raise ToolExecutionError(
                    f"工具前置 Hook 执行失败：{error}",
                    code=ToolErrorCode.HOOK_FAILED,
                ) from error
            if decision is None:
                continue
            if not decision.allowed:
                raise ToolExecutionError(
                    decision.reason or f"工具 {tool.name} 被前置 Hook 拒绝。",
                    code=ToolErrorCode.REJECTED,
                )
            explicitly_approved = True
        return explicitly_approved

    async def _run_with_controls(
        self,
        tool: Tool,
        request: ToolExecutionRequest,
        *,
        tool_policy: ToolRuntimePolicy,
    ) -> Any:
        """同时控制并发、超时和外部取消信号。"""

        if request.cancellation_event is not None:
            if request.cancellation_event.is_set():
                raise ToolExecutionError(
                    "工具调用已取消。",
                    code=ToolErrorCode.CANCELLED,
                )

        execution_task = asyncio.create_task(
            self._invoke_guarded(tool, request, tool_policy)
        )
        cancellation_task: asyncio.Task[bool] | None = None
        wait_tasks: set[asyncio.Task[Any]] = {execution_task}
        if request.cancellation_event is not None:
            cancellation_task = asyncio.create_task(
                request.cancellation_event.wait()
            )
            wait_tasks.add(cancellation_task)

        try:
            completed, _ = await asyncio.wait(
                wait_tasks,
                timeout=tool_policy.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not completed:
                raise ToolExecutionError(
                    f"工具 {tool.name} 执行超过 "
                    f"{tool_policy.timeout_seconds:g} 秒。",
                    code=ToolErrorCode.TIMEOUT,
                )
            if cancellation_task is not None and cancellation_task in completed:
                raise ToolExecutionError(
                    "工具调用已取消。",
                    code=ToolErrorCode.CANCELLED,
                )
            return await execution_task
        finally:
            for task in wait_tasks:
                if not task.done():
                    task.cancel()
            for task in wait_tasks:
                # 清理辅助任务；主执行异常已经在上方 await 时交给调用方处理。
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _invoke_guarded(
        self,
        tool: Tool,
        request: ToolExecutionRequest,
        tool_policy: ToolRuntimePolicy,
    ) -> Any:
        """取得全局和单工具并发额度后调用工具函数。"""

        tool_semaphore = self._get_tool_semaphore(tool.name, tool_policy)
        async with contextlib.AsyncExitStack() as stack:
            if tool_semaphore is not None:
                await stack.enter_async_context(tool_semaphore)
            if self._semaphore is not None:
                await stack.enter_async_context(self._semaphore)
            return await self._invoke(tool, request)

    def _get_tool_semaphore(
        self,
        tool_name: str,
        tool_policy: ToolRuntimePolicy,
    ) -> asyncio.Semaphore | None:
        """按需创建具名工具自己的并发信号量。"""

        if tool_policy.max_concurrency is None:
            return None
        semaphore = self._tool_semaphores.get(tool_name)
        if semaphore is None:
            semaphore = asyncio.Semaphore(tool_policy.max_concurrency)
            self._tool_semaphores[tool_name] = semaphore
        return semaphore

    @staticmethod
    async def _invoke(tool: Tool, request: ToolExecutionRequest) -> Any:
        """调用同步或异步工具，并避免同步函数阻塞事件循环。"""

        token = _CURRENT_TOOL_REQUEST.set(request)
        try:
            arguments = request.arguments or {}
            if inspect.iscoroutinefunction(tool.handler):
                return await tool.handler(**arguments)
            value = await asyncio.to_thread(tool.handler, **arguments)
            if inspect.isawaitable(value):
                return await value
            return value
        finally:
            _CURRENT_TOOL_REQUEST.reset(token)

    @staticmethod
    def _serialize_result(value: Any) -> str:
        """将工具返回值序列化为可回填给模型的 UTF-8 JSON 文本。"""

        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise ToolExecutionError(
                f"工具返回值无法序列化为 JSON：{error}",
                code=ToolErrorCode.EXECUTION_FAILED,
            ) from error

    @staticmethod
    def _limit_result(
        content: str,
        tool_policy: ToolRuntimePolicy,
    ) -> tuple[str, bool]:
        """限制写回模型的工具结果长度，避免上下文被异常结果占满。"""

        if len(content) <= tool_policy.max_result_chars:
            return content, False
        if not tool_policy.truncate_oversized_results:
            raise ToolExecutionError(
                "工具结果超过允许的最大长度。",
                code=ToolErrorCode.RESULT_TOO_LARGE,
            )

        suffix = "\n...[工具结果因超过长度上限已截断]"
        if len(suffix) >= tool_policy.max_result_chars:
            return suffix[: tool_policy.max_result_chars], True

        keep_chars = tool_policy.max_result_chars - len(suffix)
        return content[:keep_chars] + suffix, True

    async def _run_after_hooks(
        self,
        request: ToolExecutionRequest,
        tool: Tool,
        result: ToolExecutionResult,
    ) -> list[str]:
        """依次执行成功 Hook，并将 Hook 异常记录为结果警告。"""

        warnings: list[str] = []
        for hook in self.hooks:
            try:
                await hook.after_execute(request, tool, result)
            except Exception as error:
                warnings.append(
                    f"Hook {type(hook).__name__}.after_execute 执行失败：{error}"
                )
        return warnings

    async def _failure(
        self,
        request: ToolExecutionRequest,
        tool: Tool | None,
        error_code: str,
        message: str,
        started_at: float,
    ) -> ToolExecutionResult:
        """构造失败结果，并通知所有错误 Hook。"""

        result = ToolExecutionResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=False,
            content=message,
            error_code=str(error_code),
            elapsed_seconds=time.perf_counter() - started_at,
        )
        for hook in self.hooks:
            with contextlib.suppress(Exception):
                await hook.on_error(request, tool, result)
        return result
