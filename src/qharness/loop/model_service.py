"""共享角色调用边界：编译、持久准入、网络尝试、输出校验与结果记录。"""

import asyncio
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from pydantic import BaseModel, ValidationError

from qharness.backends.base import ModelBackend
from qharness.exception import LoopConfigurationError, LoopExecutionError, ModelBackendError
from qharness.loop.config import Role
from qharness.loop.context import ContextCompiler
from qharness.loop.models import Phase, StageOutcome, StagePlan, StageVerdict, TaskVerdict, TodoPlan
from qharness.loop.repository import LoopRepository, response_from_dict
from qharness.model.models import ChatMessage, ChatResponse, ToolDefinition


@dataclass(frozen=True)
class RoleResult:
    response: ChatResponse
    output: BaseModel | None  # Actor 发起工具调用时没有 StageOutcome。
    cached: bool


class ModelService:
    def __init__(self, backend: ModelBackend, repository: LoopRepository, compiler: ContextCompiler):
        if not backend.supports_request_retry_control:
            raise LoopConfigurationError("Loop Backend 必须支持请求级重试控制，以保证预算计量准确")
        if repository.snapshot()["policy"]["loop"] != compiler.config.model_dump(mode="json"):
            raise LoopConfigurationError("角色调用配置与 Run 的策略快照不一致")
        self.backend, self.repository, self.compiler = backend, repository, compiler

    async def call(
        self, call_id: str, role: Role, *, todo_id: str | None = None,
        observations: Sequence[str] = (), messages: Sequence[ChatMessage] = (),
        tools: Sequence[ToolDefinition] = (), task_check: bool = False,
        cancellation_event: asyncio.Event | None = None,
    ) -> RoleResult:
        role = Role(role)
        snapshot = await asyncio.to_thread(self.repository.snapshot)
        if snapshot["policy"]["loop"] != self.compiler.config.model_dump(mode="json"):
            raise LoopConfigurationError("角色配置与 Run 策略快照不一致")
        state = snapshot["state"]
        if task_check and role != Role.JUDGE:
            raise LoopConfigurationError("只有 Judge 可以执行 Task 验证")
        schema = {Role.TODO_PLANNER: TodoPlan, Role.STAGE_PLANNER: StagePlan,
                  Role.ACTOR: StageOutcome, Role.JUDGE: TaskVerdict if task_check else StageVerdict}[role]
        stage = None
        if role == Role.ACTOR or (role == Role.JUDGE and not task_check):
            phase = Phase.ACTING if role == Role.ACTOR else Phase.VERIFYING
            if state is None or state.phase != phase or not state.active_attempt_id:
                raise LoopConfigurationError("角色调用不处于对应阶段状态")
            stage = state.attempts[-1].plan
            todo_id = stage.todo_id
        if task_check and (state is None or state.phase != Phase.VERIFYING_TASK):
            raise LoopConfigurationError("尚未到任务整体验收阶段")
        request, input_tokens = self.compiler.compile(
            contract=snapshot["contract"], role=role, output_schema=schema, state=state,
            todo_id=todo_id, stage=stage, observations=observations, messages=messages, tools=tools,
        )
        request = self.backend.resolve_request(request)
        protected = {"model", "messages", "tools", "tool_choice", "response_format", "stream",
                     "max_tokens", "max_completion_tokens", "max_output_tokens", "n"}
        if protected & request.extra_body.keys() or request.max_tokens != self.compiler.config.max_output_tokens or request.backend_max_retries != 0:
            raise LoopConfigurationError("Backend 额外参数不能覆盖 Loop 消息、工具、输出限制或重试策略")
        input_tokens = self.compiler.measure(request)
        binding = {"run_version": snapshot["version"], "prompt_version": self.compiler.config.prompt_version,
                   "schema": schema.__name__, "todo_id": todo_id,
                   "stage_attempt": state.active_attempt_id if state else None}

        def parse(response: ChatResponse) -> BaseModel | None:
            if response.message.role != "assistant":
                raise ValueError("模型必须返回 assistant 消息")
            if response.message.tool_calls:
                if role != Role.ACTOR or response.finish_reason != "tool_calls" or not tools:
                    raise ValueError("当前角色或结束原因不允许工具调用")
                ids = [c.id for c in response.message.tool_calls]
                if any(not i for i in ids) or len(ids) != len(set(ids)):
                    raise ValueError("工具调用缺少唯一身份")
                return None  # 完整工具参数的准入仍由已有 ToolExecutor 负责。
            if response.finish_reason != "stop":
                raise ValueError("响应未正常结束，不能把截断结果当作角色输出")
            value = schema.model_validate_json(response.message.content or "")
            if isinstance(value, TodoPlan):
                value.validate_contract(snapshot["contract"])
            if isinstance(value, StagePlan) and (value.todo_id != todo_id or
                value.todo_version != next(t.todo.version for t in state.todos if t.todo.id == todo_id)):
                raise ValueError("阶段计划属于其他 Todo 或旧版本")
            if isinstance(value, StagePlan):
                todo = next(t.todo for t in state.todos if t.todo.id == todo_id)
                allowed = set(todo.acceptance_refs) | {q.id for q in state.questions
                    if set(q.acceptance_refs) & set(todo.acceptance_refs)}
                if not set(value.addresses) <= allowed:
                    raise ValueError("阶段目标未关联当前 Todo 的有效缺口")
            if isinstance(value, (StageOutcome, StageVerdict)):
                identity = state.attempts[-1].identity.model_dump()
                if any(getattr(value, k) != v for k, v in identity.items()):
                    raise ValueError("阶段输出不属于当前尝试")
            if isinstance(value, TaskVerdict) and value.contract_version != snapshot["contract"].version:
                raise ValueError("任务验证绑定旧契约")
            return value

        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                raise asyncio.CancelledError
            admission = await asyncio.to_thread(self.repository.begin_model, call_id, role.value, asdict(request), binding,
                input_tokens + self.compiler.config.max_output_tokens)
            if admission["cached"]:
                response = response_from_dict(admission["response"])
                return RoleResult(response, parse(response), True)
            attempt = admission["attempt"]
            response = None
            try:
                response = await self._complete(request, cancellation_event)
                output = parse(response)
            except ModelBackendError as error:
                await asyncio.to_thread(self.repository.finish_model, call_id, attempt, error=str(error), retryable=error.retryable)
                if not error.retryable or attempt >= self.compiler.config.max_request_attempts:
                    raise
                await self._backoff(cancellation_event)
                continue
            except (ValueError, ValidationError) as error:
                await asyncio.to_thread(self.repository.finish_model, call_id, attempt, response=response, error=str(error))
                raise LoopExecutionError(f"角色输出协议错误：{error}", code="protocol_error") from error
            except TimeoutError as error:
                # 未得到可确认结果，保留 DISPATCHED 与 Token 预留，不自动重放。
                raise LoopExecutionError("模型请求超时，结果未知", code="unknown") from error
            await asyncio.to_thread(self.repository.finish_model, call_id, attempt, response=response)
            return RoleResult(response, output, False)

    async def _complete(self, request, cancellation_event):
        work = asyncio.create_task(self.backend.complete(request))
        cancel = asyncio.create_task(cancellation_event.wait()) if cancellation_event else None
        tasks = {work, cancel} if cancel else {work}
        try:
            done, _ = await asyncio.wait(tasks, timeout=self.compiler.config.model_timeout_seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
            if cancel is not None and cancel in done:
                raise asyncio.CancelledError
            if work not in done:
                raise TimeoutError
            return await work
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _backoff(self, cancellation_event):
        delay = self.compiler.config.retry_delay_seconds
        if cancellation_event is None:
            await asyncio.sleep(delay)
        else:
            try:
                await asyncio.wait_for(cancellation_event.wait(), timeout=delay)
            except TimeoutError:
                return
            raise asyncio.CancelledError
