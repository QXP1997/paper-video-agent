"""检查复用 ToolService → ToolExecutor → run_command → 现有沙箱。"""

import asyncio
from dataclasses import asdict
from typing import Callable

from qharness.exception import LoopConfigurationError, LoopExecutionError, WorkspaceError
from qharness.loop.models import CheckStatus, Phase, Scope
from qharness.loop.repository import digest
from qharness.loop.tool_service import ToolService
from qharness.verification.contracts import CheckEvidence, CheckObservation, CheckSpec
from qharness.verification.evidence import combine, interpret
from qharness.workspace.version import workspace_fingerprint


class CheckRunner:
    def __init__(self, tools: ToolService, *, environment: Callable[[], str | None] | None = None):
        self.tools, self.repository = tools, tools.repository
        # 服务端应提供镜像/解释器/依赖/外部服务版本身份，不能由 Actor 填写。
        self.environment = environment or (lambda: tools.context.metadata.get("verification_environment"))

    def definition_hash(self, spec: CheckSpec) -> str:
        tool = self.tools.executor.registry.get("run_command")
        return digest(["check-parser-v1", spec.model_dump(mode="json"),
                       asdict(tool.to_definition()) if tool else None])

    async def fingerprints(self, spec: CheckSpec) -> tuple[str, str]:
        workspace = await asyncio.to_thread(workspace_fingerprint, self.tools.context.workspace, spec.inputs)
        identity = await asyncio.to_thread(self.environment)
        if not isinstance(identity, str) or not identity.strip():
            raise LoopConfigurationError("检查需要应用提供 verification_environment 身份，覆盖运行时和外部依赖版本")
        status = await self.tools.context.sandbox.check_status()
        if not status.available:
            raise LoopExecutionError("验证沙箱不可用", code="check_environment")
        return workspace, digest([identity, status.backend, status.version])

    def _optional_artifact(self, ref):
        try:
            return self.repository.read_artifact(ref)
        except LoopExecutionError as error:
            if error.code != "not_found":
                raise
            return None

    def read(self, ref: str) -> CheckEvidence:
        value = self.repository.read_artifact(ref)
        if not isinstance(value, dict) or value.get("kind") != "check_evidence_v1" or value.get("id") != ref:
            raise LoopExecutionError("引用不是检查证据", code="invalid_evidence")
        return CheckEvidence.model_validate(value)

    async def current(self, evidence: CheckEvidence, spec: CheckSpec | None = None) -> bool:
        spec = spec or evidence.spec
        if evidence.definition_hash != self.definition_hash(spec):
            return False
        try:
            workspace, environment = await self.fingerprints(spec)
        except (OSError, ValueError, LoopExecutionError, WorkspaceError):
            return False
        return (evidence.workspace_before == evidence.workspace_after == workspace
                and evidence.environment_before == evidence.environment_after == environment)

    async def run(self, verification_id: str, spec: CheckSpec, *, expected_version: int,
                  baseline_ref: str | None = None) -> CheckEvidence:
        spec = CheckSpec.model_validate(spec)
        snapshot = await asyncio.to_thread(self.repository.snapshot)
        state = snapshot["state"]
        if state is None or state.version != expected_version:
            raise LoopExecutionError("验证上下文已过期", code="stale_context")
        allowed = Phase.VERIFYING_TASK if spec.scope == Scope.TASK else Phase.VERIFYING
        if state.phase != allowed:
            raise LoopConfigurationError("检查 scope 与当前验证阶段不一致")
        ref = digest([self.repository.key, "check", verification_id, spec.id])
        definition = self.definition_hash(spec)
        existing = await asyncio.to_thread(self._optional_artifact, ref)
        if existing is not None:
            evidence = self.read(ref)
            if evidence.run_version != expected_version or evidence.definition_hash != definition or evidence.baseline_ref != baseline_ref:
                raise LoopExecutionError("检查身份被用于不同上下文", code="conflict")
            if not await self.current(evidence, spec):
                raise LoopExecutionError("旧检查证据已失效；请创建新验证轮次", code="stale_evidence")
            return evidence
        try:
            before, environment = await self.fingerprints(spec)
        except (OSError, WorkspaceError, LoopExecutionError) as error:
            # 没有实际运行检查；环境/输入读取错误不构成业务失败。
            observation = CheckObservation(status=CheckStatus.ERROR, reason=f"检查输入或环境不可用：{type(error).__name__}")
            evidence = CheckEvidence(id=ref, spec=spec, definition_hash=definition, run_version=expected_version,
                attempt_id=state.active_attempt_id, contract_version=state.contract.version,
                workspace_before="unavailable", workspace_after="unavailable",
                environment_before="unavailable", environment_after="unavailable", observations=(observation,),
                status=CheckStatus.ERROR, reason=observation.reason, baseline_ref=baseline_ref)
            await asyncio.to_thread(self.repository.put_artifact, ref, evidence.model_dump(mode="json"),
                                    expected_version=expected_version)
            return evidence
        intent = dict(definition=definition, before=before, environment=environment,
                      run_version=expected_version, baseline_ref=baseline_ref)
        # 先保存输入身份；进程重建后不能把旧工具结果绑定到新文件。
        await asyncio.to_thread(self.repository.put_artifact, digest([ref, "intent"]), intent,
                                expected_version=expected_version)
        observations = []
        for index in range(spec.repetitions):
            result = await self.tools.execute_check(digest([ref, "execution", index]),
                {"command": spec.command, "cwd": spec.cwd}, check_binding={"evidence_ref": ref, **intent},
                expected_version=expected_version)
            observation = interpret(spec, result)
            observations.append(observation)
            if result.error_code in {"unknown", "cancelled"}:
                break  # 未知效果禁止再开一个逻辑 ID 自动重跑。
        try:
            after, environment_after = await self.fingerprints(spec)
        except (OSError, WorkspaceError, LoopExecutionError) as error:
            after, environment_after = "unavailable", "unavailable"
            observations.append(CheckObservation(status=CheckStatus.ERROR,
                reason=f"检查后无法核对输入或环境：{type(error).__name__}"))
        status = combine(o.status for o in observations)
        reason = "; ".join(dict.fromkeys(o.reason for o in observations))
        if len({(o.status, o.tests_run, o.failure_signature) for o in observations}) > 1:
            status, reason = CheckStatus.INCONCLUSIVE, "相同输入重复检查结果不一致（flaky），需调查"
        if before != after or environment != environment_after:
            status, reason = CheckStatus.INCONCLUSIVE, "检查期间输入或环境变化，不能归属到稳定版本"
        if after == "unavailable":
            status, reason = CheckStatus.ERROR, observations[-1].reason
        pre_existing = False
        if baseline_ref is not None:
            baseline = await asyncio.to_thread(self.read, baseline_ref)
            if baseline.definition_hash != definition or baseline.environment_after != environment:
                raise LoopExecutionError("基线检查定义或环境不同，不能比较", code="invalid_evidence")
            pre_existing = (status == baseline.status == CheckStatus.FAIL and
                baseline.workspace_before == baseline.workspace_after and
                baseline.environment_before == baseline.environment_after and
                {o.failure_signature for o in observations} == {o.failure_signature for o in baseline.observations})
        evidence = CheckEvidence(id=ref, spec=spec, definition_hash=definition, run_version=expected_version,
            attempt_id=state.active_attempt_id, contract_version=state.contract.version,
            workspace_before=before, workspace_after=after, environment_before=environment,
            environment_after=environment_after, observations=tuple(observations), status=status,
            reason=reason, baseline_ref=baseline_ref, pre_existing=pre_existing)
        await asyncio.to_thread(self.repository.put_artifact, ref, evidence.model_dump(mode="json"))
        return evidence
