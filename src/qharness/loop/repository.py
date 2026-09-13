"""复用应用 SessionFactory 的 Loop 账本，所有读写均绑定租户、工作区和 Run。

同一 Run 的准入事务先更新其 ledger_version 取得数据库写锁，再读取计数；
不依赖进程内锁。事务结束后才调用模型/工具，外部调用不占用数据库事务。
"""

import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, ForeignKeyConstraint, Integer, String, Text, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.exc import SQLAlchemyError

from qharness.exception import LoopExecutionError
from qharness.loop.config import LoopConfig
from qharness.loop.models import LoopEvent, RunState, TaskContract
from qharness.loop.transitions import reduce
from qharness.model.models import ChatMessage, ChatResponse, FunctionCall, ToolCall, Usage
from qharness.persistence import OrmBase
from qharness.tools.base import ToolExecutionPolicy, ToolExecutionResult


_PAYLOAD = Text().with_variant(LONGTEXT(), "mysql")


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def fail(message: str, code="conflict"):
    raise LoopExecutionError(message, code=code)


def validate_call_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or "\x00" in value:
        fail("调用 ID 必须为 1—128 字符且不含空字符", "invalid_identity")


def response_from_dict(value: dict) -> ChatResponse:
    """恢复已有领域对象，包括 Provider 返回的 reasoning_content。"""
    raw = dict(value)
    message = dict(raw["message"])
    message["tool_calls"] = [ToolCall(id=c["id"], type=c["type"], function=FunctionCall(**c["function"]))
                             for c in message.get("tool_calls", [])]
    raw["message"] = ChatMessage(**message)
    raw["usage"] = Usage(**raw["usage"]) if raw.get("usage") is not None else None
    return ChatResponse(**raw)


class RunRecord(OrmBase):
    __tablename__ = "loop_runs"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    workspace_id: Mapped[str] = mapped_column(String(128))
    run_id: Mapped[str] = mapped_column(String(128))
    contract: Mapped[str] = mapped_column(_PAYLOAD)
    state: Mapped[str | None] = mapped_column(_PAYLOAD)
    policy: Mapped[str] = mapped_column(_PAYLOAD)
    version: Mapped[int] = mapped_column(Integer, default=0)
    ledger_version: Mapped[int] = mapped_column(Integer, default=0)
    model_attempts: Mapped[int] = mapped_column(Integer, default=0)
    used_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_executions: Mapped[int] = mapped_column(Integer, default=0)


class ModelCallRecord(OrmBase):
    __tablename__ = "loop_model_calls"
    run_key: Mapped[str] = mapped_column(ForeignKey("loop_runs.key"), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    binding: Mapped[str] = mapped_column(_PAYLOAD)
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    response: Mapped[str | None] = mapped_column(_PAYLOAD)
    error: Mapped[str | None] = mapped_column(_PAYLOAD)


class ModelAttemptRecord(OrmBase):
    __tablename__ = "loop_model_attempts"
    run_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    request: Mapped[str] = mapped_column(_PAYLOAD)
    status: Mapped[str] = mapped_column(String(32))
    reserved_tokens: Mapped[int] = mapped_column(Integer)
    usage_known: Mapped[bool] = mapped_column(Boolean, default=False)
    response: Mapped[str | None] = mapped_column(_PAYLOAD)
    error: Mapped[str | None] = mapped_column(_PAYLOAD)
    started_at: Mapped[str] = mapped_column(String(40))
    finished_at: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (ForeignKeyConstraint(["run_key", "call_id"],
                                          ["loop_model_calls.run_key", "loop_model_calls.call_id"]),)


class MessageRecord(OrmBase):
    __tablename__ = "loop_messages"
    run_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(_PAYLOAD)
    __table_args__ = (ForeignKeyConstraint(["run_key", "call_id", "attempt"],
        ["loop_model_attempts.run_key", "loop_model_attempts.call_id", "loop_model_attempts.attempt"]),)


class ToolCallRecord(OrmBase):
    __tablename__ = "loop_tool_calls"
    run_key: Mapped[str] = mapped_column(ForeignKey("loop_runs.key"), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    tool_name: Mapped[str] = mapped_column(String(128))
    arguments: Mapped[str] = mapped_column(_PAYLOAD)
    binding: Mapped[str] = mapped_column(_PAYLOAD)
    policy: Mapped[str] = mapped_column(_PAYLOAD)
    operation_id: Mapped[str] = mapped_column(String(32), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    executions: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[str | None] = mapped_column(_PAYLOAD)


class ToolExecutionRecord(OrmBase):
    __tablename__ = "loop_tool_executions"
    run_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    execution: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[str] = mapped_column(String(40))
    finished_at: Mapped[str | None] = mapped_column(String(40))
    result: Mapped[str | None] = mapped_column(_PAYLOAD)
    __table_args__ = (ForeignKeyConstraint(["run_key", "call_id"],
                                          ["loop_tool_calls.run_key", "loop_tool_calls.call_id"]),)


class ArtifactRecord(OrmBase):
    __tablename__ = "loop_artifacts"
    run_key: Mapped[str] = mapped_column(ForeignKey("loop_runs.key"), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(_PAYLOAD)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    media_type: Mapped[str] = mapped_column(String(64))


class LoopRepository:
    def __init__(self, sessions: sessionmaker[Session], *, tenant_id: str, workspace_id: str, run_id: str):
        for value in (tenant_id, workspace_id, run_id):
            if not value or len(value) > 128 or "\x00" in value:
                raise ValueError("Loop 标识必须是 1—128 字符且不含空字符")
        self.sessions = sessions
        self.tenant_id, self.workspace_id, self.run_id = tenant_id, workspace_id, run_id
        self.key = digest([tenant_id, workspace_id, run_id])

    @contextmanager
    def _session(self, *, transaction=False):
        try:
            with self.sessions() as session:
                if transaction:
                    with session.begin():
                        yield session
                else:
                    yield session
        except SQLAlchemyError as error:
            # 不把 SQL 参数中的用户内容、工具输出或配置展开到业务错误文本。
            raise LoopExecutionError("Loop 账本数据库操作失败", code="storage_error") from error

    def create(self, contract: TaskContract, config: LoopConfig, tools: ToolExecutionPolicy,
               *, state: RunState | None = None) -> None:
        if state is not None and (state.run_id != self.run_id or state.contract != contract):
            fail("RunState 与仓储身份或契约不一致")
        with self._session(transaction=True) as session:
            existing = session.get(RunRecord, self.key)
            if existing is not None:
                policy = json.loads(existing.policy)
                policy["loop"] = LoopConfig.model_validate(policy["loop"]).model_dump(mode="json")
                if existing.contract != contract.model_dump_json() or policy != config.snapshot(tools):
                    fail("已存在 Run 的任务契约或策略不同")
                return  # 重新装配不能覆盖已有状态或清零预算。
            session.add(RunRecord(key=self.key, tenant_id=self.tenant_id, workspace_id=self.workspace_id,
                run_id=self.run_id, contract=contract.model_dump_json(), state=state.model_dump_json() if state else None,
                version=state.version if state else 0, policy=encode(config.snapshot(tools))))

    @contextmanager
    def _write(self) -> Iterator[tuple[Session, RunRecord]]:
        with self._session(transaction=True) as session:
            result = session.execute(update(RunRecord).where(RunRecord.key == self.key).values(
                ledger_version=RunRecord.ledger_version + 1))
            if result.rowcount != 1:
                fail("Run 不存在或不属于当前租户", "not_found")
            yield session, session.get(RunRecord, self.key)

    def snapshot(self) -> dict:
        with self._session() as session:
            row = session.get(RunRecord, self.key)
            if row is None:
                fail("Run 不存在或不属于当前租户", "not_found")
            return {"contract": TaskContract.model_validate_json(row.contract),
                    "state": RunState.model_validate_json(row.state) if row.state else None,
                    "version": row.version, "policy": {**json.loads(row.policy),
                        "loop": LoopConfig.model_validate(json.loads(row.policy)["loop"]).model_dump(mode="json")},
                    "budget": {name: getattr(row, name) for name in
                               ("model_attempts", "used_tokens", "reserved_tokens", "tool_calls", "tool_executions")}}

    def install_state(self, state: RunState) -> None:
        with self._write() as (_, row):
            if row.state is not None or state.run_id != self.run_id or state.contract != TaskContract.model_validate_json(row.contract):
                fail("只允许为同一契约安装初始状态")
            row.state, row.version = state.model_dump_json(), state.version

    def apply(self, event: LoopEvent) -> RunState:
        with self._write() as (_, row):
            if row.state is None:
                fail("尚未安装 TodoPlan")
            state = reduce(RunState.model_validate_json(row.state), event)
            row.state, row.version = state.model_dump_json(), state.version
            return state

    def begin_model(self, call_id: str, role: str, request: dict, binding: dict, reservation: int) -> dict:
        validate_call_id(call_id)
        if isinstance(reservation, bool) or not isinstance(reservation, int) or reservation <= 0:
            fail("Token 预留必须为正整数", "invalid_budget")
        fingerprint = digest([role, request, binding])
        with self._write() as (session, run):
            call = session.get(ModelCallRecord, (self.key, call_id))
            if call:
                if call.fingerprint != fingerprint:
                    fail("模型逻辑调用 ID 被用于不同请求")
                if call.status == "done":
                    return {"response": json.loads(call.response), "cached": True}
                if call.status == "dispatched":
                    fail("模型请求仍在执行或结果未知，不自动重放", "unknown")
                if not call.retryable:
                    fail(call.error or "模型调用失败", "model_failed")
            if binding["run_version"] != run.version:
                fail("模型调用基于过期 RunState")
            policy = json.loads(run.policy)["loop"]
            if run.model_attempts >= policy["max_model_attempts"] or (
                call is not None and call.attempts >= policy["max_request_attempts"]
            ):
                fail("模型尝试次数预算耗尽", "budget_exceeded")
            if policy["max_total_tokens"] is not None and run.used_tokens + run.reserved_tokens + reservation > policy["max_total_tokens"]:
                fail("模型 Token 预算不足", "budget_exceeded")
            if call is None:
                call = ModelCallRecord(run_key=self.key, call_id=call_id, fingerprint=fingerprint,
                                       binding=encode(binding), role=role, status="dispatched", attempts=0)
                session.add(call)
                session.flush()
            call.attempts += 1
            call.status = "dispatched"
            run.model_attempts += 1
            run.reserved_tokens += reservation
            attempt = ModelAttemptRecord(run_key=self.key, call_id=call_id, attempt=call.attempts,
                request=encode(request), status="dispatched", reserved_tokens=reservation, started_at=datetime.now(UTC).isoformat())
            session.add(attempt)
            session.flush()
            for index, message in enumerate(request["messages"]):
                session.add(MessageRecord(run_key=self.key, call_id=call_id, attempt=call.attempts,
                                          sequence=index, direction="input", payload=encode(message)))
            return {"attempt": call.attempts, "cached": False}

    def finish_model(self, call_id: str, attempt: int, *, response: ChatResponse | None = None,
                     error: str | None = None, retryable: bool = False) -> None:
        with self._write() as (session, run):
            call = session.get(ModelCallRecord, (self.key, call_id))
            record = session.get(ModelAttemptRecord, (self.key, call_id, attempt))
            if call is None or record is None or call.attempts != attempt or record.status != "dispatched":
                fail("模型结果身份过期或重复提交")
            status = "failed" if error else "done"
            raw = encode(asdict(response)) if response else None
            call.status, call.response, call.error, call.retryable = status, raw, error, retryable
            record.status, record.response, record.error = status, raw, error
            record.finished_at = datetime.now(UTC).isoformat()
            if response:
                index = len(json.loads(record.request)["messages"])
                session.add(MessageRecord(run_key=self.key, call_id=call_id, attempt=attempt, sequence=index,
                                          direction="output", payload=encode(asdict(response.message))))
            if response and response.usage is not None:
                usage = response.usage
                if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in
                       (usage.total_tokens, usage.prompt_tokens, usage.completion_tokens)):
                    fail("Provider Token 用量不合法", "protocol_error")
                tokens = max(usage.total_tokens, usage.prompt_tokens + usage.completion_tokens)
                if tokens < 0:
                    fail("Provider Token 用量不合法", "protocol_error")
                run.reserved_tokens -= record.reserved_tokens
                run.used_tokens += tokens
                record.usage_known = True
            # 未返回 usage 的尝试保留预留额度，不能当作免费调用。

    def admit_tool(self, call_id: str, tool_name: str, arguments: Any, binding: dict,
                   policy: ToolExecutionPolicy, *, requires_approval: bool = False) -> None:
        validate_call_id(call_id)
        validate_call_id(tool_name)
        resolved = asdict(policy.for_tool(tool_name))
        resolved["requires_approval"] = resolved["requires_approval"] or requires_approval
        fingerprint = digest([tool_name, arguments, binding, resolved])
        with self._write() as (session, run):
            if json.loads(run.policy)["tools"] != asdict(policy):
                fail("工具准入策略与 Run 快照不一致")
            existing = session.get(ToolCallRecord, (self.key, call_id))
            if existing:
                if existing.fingerprint != fingerprint:
                    fail("同一工具逻辑调用 ID 的参数、绑定或策略发生变化")
                return
            if run.version != binding["run_version"]:
                fail("工具调用基于过期 RunState")
            count = len(session.scalars(select(ToolCallRecord).where(
                ToolCallRecord.run_key == self.key, ToolCallRecord.tool_name == tool_name)).all())
            if (policy.max_total_calls is not None and run.tool_calls >= policy.max_total_calls) or count >= resolved["max_calls"]:
                fail("工具逻辑调用预算耗尽", "budget_exceeded")
            run.tool_calls += 1
            session.add(ToolCallRecord(run_key=self.key, call_id=call_id, fingerprint=fingerprint,
                tool_name=tool_name, arguments=encode(arguments), binding=encode(binding), policy=encode(resolved),
                operation_id=uuid.uuid4().hex, status="admitted"))

    def claim_tool(self, call_id: str, *, retry: bool = False) -> dict:
        with self._write() as (session, run):
            call = session.get(ToolCallRecord, (self.key, call_id))
            if call is None:
                fail("工具尚未准入", "not_found")
            if call.status == "dispatched":
                return {"status": "unknown", "operation_id": call.operation_id}
            if call.status == "done":
                if not retry:
                    return {"status": "done"}
                result = json.loads(call.result)
                if result["error_code"] not in {"unknown_tool", "invalid_arguments", "rejected"}:
                    fail("无法证明上次未执行 Handler，禁止重放；需显式恢复核对", "unknown")
            if json.loads(call.binding)["run_version"] != run.version:
                fail("未执行的工具调用绑定已经过期")
            if run.tool_executions >= json.loads(run.policy)["loop"]["max_tool_executions"]:
                fail("工具执行尝试预算耗尽", "budget_exceeded")
            call.status = "dispatched"
            call.executions += 1
            run.tool_executions += 1
            session.add(ToolExecutionRecord(run_key=self.key, call_id=call_id, execution=call.executions,
                status="dispatched", started_at=datetime.now(UTC).isoformat()))
            return {"status": "execute", "operation_id": call.operation_id, "execution": call.executions}

    def finish_tool(self, result: ToolExecutionResult, execution: int, *, max_result_chars: int = 200_000) -> ToolExecutionResult:
        with self._write() as (session, _):
            call = session.get(ToolCallRecord, (self.key, result.call_id))
            record = session.get(ToolExecutionRecord, (self.key, result.call_id, execution))
            if call is None or record is None or call.status != "dispatched" or call.executions != execution:
                fail("工具结果身份过期或重复提交")
            content = encode({"data": result.data, "content": result.content})
            artifact_id = digest([self.key, content])
            if session.get(ArtifactRecord, (self.key, artifact_id)) is None:
                session.add(ArtifactRecord(run_key=self.key, artifact_id=artifact_id, content=content,
                    sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(), size_bytes=len(content.encode("utf-8")),
                    media_type="application/json"))
            result = replace(result, artifact_id=artifact_id)
            if result.truncated:
                # 摘要使用可解析 JSON，业务判断必须读取 data / Artifact。
                fields = {key: result.data[key] for key in (
                    "succeeded", "exit_code", "timed_out", "cancelled", "stdout_truncated", "stderr_truncated", "operation_id"
                ) if isinstance(result.data, dict) and key in result.data}
                projection = {"artifact_id": artifact_id, "truncated": True, **fields}
                if len(encode(projection)) <= max_result_chars:
                    projection["summary"] = result.content
                    while len(encode(projection)) > max_result_chars and projection["summary"]:
                        projection["summary"] = projection["summary"][:len(projection["summary"]) // 2]
                    if len(encode(projection)) > max_result_chars:
                        projection.pop("summary")
                    result = replace(result, content=encode(projection))
            payload = encode(asdict(replace(result, data=None)))
            call.status, call.result = "done", payload
            record.status, record.result, record.finished_at = "done", payload, datetime.now(UTC).isoformat()
            return result

    def read_artifact(self, artifact_id: str) -> Any:
        with self._session() as session:
            row = session.get(ArtifactRecord, (self.key, artifact_id))
            if row is None:
                fail("Artifact 不存在或不属于当前 Run", "not_found")
            if hashlib.sha256(row.content.encode("utf-8")).hexdigest() != row.sha256:
                fail("Artifact 内容摘要不匹配", "corrupt_artifact")
            return json.loads(row.content)

    def tool_result(self, call_id: str) -> ToolExecutionResult:
        with self._session() as session:
            row = session.get(ToolCallRecord, (self.key, call_id))
            if row is None or row.status != "done":
                fail("工具结果尚未确定", "unknown")
            raw = json.loads(row.result)
        raw["warnings"] = tuple(raw["warnings"])
        raw["data"] = self.read_artifact(raw["artifact_id"])["data"]
        return ToolExecutionResult(**raw)

    def tool_counts(self) -> dict[str, int]:
        with self._session() as session:
            counts: dict[str, int] = {}
            for name in session.scalars(select(ToolCallRecord.tool_name).where(ToolCallRecord.run_key == self.key)):
                counts[name] = counts.get(name, 0) + 1
            return counts

    def model_trace(self, call_id: str) -> list[dict]:
        with self._session() as session:
            call = session.get(ModelCallRecord, (self.key, call_id))
            if call is None:
                return []
            return [{"role": call.role, "binding": json.loads(call.binding),
                     "attempt": row.attempt, "status": row.status, "request": json.loads(row.request),
                     "response": json.loads(row.response) if row.response else None, "error": row.error,
                     "usage_known": row.usage_known} for row in session.scalars(select(ModelAttemptRecord).where(
                         ModelAttemptRecord.run_key == self.key, ModelAttemptRecord.call_id == call_id
                     ).order_by(ModelAttemptRecord.attempt))]
