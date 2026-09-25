"""未知效果只能由受信任应用核对；不把超时、工作区未变化或一段模型文字当完成。"""

import asyncio
from dataclasses import asdict
from typing import Literal

from pydantic import Field, TypeAdapter, model_validator

from qharness.exception import LoopExecutionError
from qharness.loop.models import ContractModel, Text
from qharness.loop.repository import digest, encode
from qharness.tools.base import ToolExecutionResult
from qharness.workspace.version import workspace_fingerprint


class RecoveryEvidence(ContractModel):
    kind: Literal["recovery_evidence_v1"] = "recovery_evidence_v1"
    call_kind: Literal["tool", "model"]
    call_id: Text
    attempt: int = Field(ge=1)
    operation_id: str | None = None
    resolution: Literal["completed", "not_executed", "partial", "abandoned"]
    quiescent: Literal[True]
    description: Text
    workspace: Text
    result: dict | None = None

    @model_validator(mode="after")
    def validate_resolution(self):
        if self.call_kind == "model":
            if self.resolution != "abandoned" or self.result is not None or self.operation_id is not None:
                raise ValueError("模型恢复只允许显式放弃未知响应")
        else:
            if self.resolution == "abandoned" or self.result is None or self.operation_id is None:
                raise ValueError("工具恢复需要原 operation 和具体结果")
            result = TypeAdapter(ToolExecutionResult).validate_json(encode(self.result), strict=True)
            if result.call_id != self.call_id or (self.resolution != "completed" and result.success):
                raise ValueError("核对结果身份或完成语义不正确")
            if result.error_code in ("unknown", "cancelled", "timeout"):
                raise ValueError("未知结果不能解除恢复阻塞")
        return self


class RecoveryController:
    def __init__(self, service):
        self.service, self.repository = service, service.repository

    async def observe(self, call_kind, call_id, *, resolution, description, quiescent, result=None):
        """应用检查远端运行/进程/操作历史后提供事实；此接口不注册为模型工具。"""
        if quiescent is not True or self.service.services.tools.executor._background:
            raise LoopExecutionError("实际执行尚未静止，不能解除未知效果", code="unknown")
        call = await asyncio.to_thread(self.repository.call_record, call_kind, call_id)
        if call is None or call["status"] != "dispatched":
            raise LoopExecutionError("只有已派发且结果未知的调用需要核对", code="conflict")
        if call_kind == "model" and (resolution != "abandoned" or result is not None):
            raise ValueError("丢失的模型响应只允许显式放弃；Token 预留不会返还")
        if call_kind == "tool":
            if resolution == "abandoned" or not isinstance(result, ToolExecutionResult):
                raise ValueError("工具恢复必须提供核对后的具体结果")
            if result.call_id != call_id or result.tool_name != call["tool_name"]:
                raise ValueError("结果与原工具身份不一致")
            if resolution != "completed" and result.success:
                raise ValueError("未执行或部分效果不能标成成功")
            if result.error_code in ("unknown", "cancelled", "timeout"):
                raise ValueError("结果仍未知，不能作为恢复结论")
        evidence = RecoveryEvidence(call_kind=call_kind, call_id=call_id,
            attempt=call["attempts"] if call_kind == "model" else call["executions"],
            operation_id=call.get("operation_id"), resolution=resolution, quiescent=True, description=description,
            workspace=await asyncio.to_thread(workspace_fingerprint, self.service.context.workspace),
            result=asdict(result) if result is not None else None)
        ref = digest(["recovery-evidence", self.repository.key, evidence.model_dump(mode="json")])
        await asyncio.to_thread(self.repository.put_artifact, ref, evidence.model_dump(mode="json"))
        return ref

    async def reconcile(self, evidence_ref):
        self.service.ownership.acquire()
        try:
            evidence = RecoveryEvidence.model_validate(await asyncio.to_thread(self.repository.read_artifact, evidence_ref))
            if self.service.services.tools.executor._background:
                raise LoopExecutionError("线程仍可能产生副作用", code="unknown")
            current = await asyncio.to_thread(workspace_fingerprint, self.service.context.workspace)
            if current != evidence.workspace:
                raise LoopExecutionError("核对后工作区已变化，必须重新取证", code="stale_evidence")
            call = await asyncio.to_thread(self.repository.call_record, evidence.call_kind, evidence.call_id)
            if call is None or (call.get("operation_id"), call.get("executions", call.get("attempts"))) != (
                    evidence.operation_id, evidence.attempt):
                raise LoopExecutionError("核对证据的执行身份已过期", code="conflict")
            receipt = digest(["reconciled", evidence.call_kind, evidence.call_id, evidence.attempt])
            if call["status"] != "dispatched":
                saved = await asyncio.to_thread(self.repository.read_artifact, receipt)
                if saved != {"evidence_ref": evidence_ref}:
                    raise LoopExecutionError("调用已有不同的核对结论", code="conflict")
                return call["status"]
            if evidence.call_kind == "model":
                await asyncio.to_thread(self.repository.finish_model, evidence.call_id, evidence.attempt,
                                        error="应用已核对并放弃丢失的模型响应；保留用量预留", retryable=False,
                                        reconciliation_ref=evidence_ref)
            else:
                raw = dict(evidence.result)
                raw["warnings"] = tuple(raw.get("warnings", ()))
                result = ToolExecutionResult(**raw)
                if result.call_id != evidence.call_id or result.tool_name != call["tool_name"]:
                    raise ValueError("核对结果身份不匹配")
                def guard():
                    if workspace_fingerprint(self.service.context.workspace) != evidence.workspace:
                        raise LoopExecutionError("提交核对结果时工作区已变化", code="stale_evidence")
                await asyncio.to_thread(self.repository.finish_tool, result, evidence.attempt, guard=guard,
                                        reconciliation_ref=evidence_ref)
            return "reconciled"
        finally:
            uncertain = True
            try:
                uncertain = bool(self.repository.unresolved_calls())
            finally:
                self.service.ownership.release(uncertain=uncertain)
