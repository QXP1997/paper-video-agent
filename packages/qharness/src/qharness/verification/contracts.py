"""受信任应用提供检查定义；模型的完成声明不能生成通过证据。"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from qharness.loop.models import CheckStatus, ContractModel, FailureLayer, Scope, Text, Version


class CheckKind(StrEnum):
    COMMAND = "command"
    UNITTEST = "unittest"
    PYTEST = "pytest"


class QuestionConclusion(ContractModel):
    question_id: Text
    finding: Text
    kind: Literal["resolved", "narrowed", "eliminated"] = "resolved"
    fact_id: Text | None = None


class DiagnosisRule(ContractModel):
    """应用声明此检查失败能证明的错误层级；不能从退出码自动猜测根因。"""
    layer: FailureLayer
    summary: Text
    invalidated_assumptions: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def validate_layer(self) -> Self:
        if self.invalidated_assumptions and self.layer != FailureLayer.STAGE_ASSUMPTION:
            raise ValueError("前提失效只能由 stage_assumption 诊断声明")
        return self


class CheckSpec(ContractModel):
    id: Text
    version: Version = 1
    scope: Scope
    # Stage 使用 expected_results 原文；Todo/Task 使用原始 criterion ID。
    targets: tuple[Text, ...] = Field(min_length=1)
    command: Text
    cwd: Text = "."
    kind: CheckKind = CheckKind.COMMAND
    inputs: tuple[Text, ...] | None = None
    repetitions: Annotated[int, Field(strict=True, ge=1, le=5)] = 1
    # 仅 COMMAND 生效；默认不把未知非零退出码当业务失败。
    failure_exit_codes: tuple[Annotated[int, Field(strict=True, ge=1)], ...] = ()
    conclusions: tuple[QuestionConclusion, ...] = ()
    addresses: tuple[Text, ...] = ()
    on_failure: DiagnosisRule | None = None

    @model_validator(mode="after")
    def validate_spec(self) -> Self:
        if self.inputs == ():
            raise ValueError("输入依赖不能是空集；未知依赖请使用 None")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("检查目标不能重复")
        if self.conclusions and self.scope != Scope.STAGE:
            raise ValueError("调查问题结论只能绑定阶段检查")
        return self


class CheckObservation(ContractModel):
    status: CheckStatus
    reason: Text
    tests_run: int | None = Field(default=None, ge=0)
    failure_signature: str | None = None
    artifact_id: str | None = None


class CheckEvidence(ContractModel):
    kind: str = "check_evidence_v1"
    id: Text
    spec: CheckSpec
    definition_hash: Text
    run_version: Version
    attempt_id: str | None
    contract_version: Version
    workspace_before: Text
    workspace_after: Text
    environment_before: Text
    environment_after: Text
    observations: tuple[CheckObservation, ...] = Field(min_length=1)
    status: CheckStatus
    reason: Text
    baseline_ref: str | None = None
    pre_existing: bool = False


class FailureBundle(ContractModel):
    check_id: Text
    status: CheckStatus
    reason: Text
    evidence_ref: Text | None = None
    artifact_refs: tuple[Text, ...] = ()
    pre_existing: bool = False
    next_action: Text
