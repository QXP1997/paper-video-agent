"""Loop 调用策略；工具策略仍由已有 tools.config 解析并继承。"""

from dataclasses import asdict
from enum import StrEnum
from pathlib import Path

from pydantic import ConfigDict, Field, ValidationError, model_validator

from qharness.exception import LoopConfigurationError
from qharness.loop.models import ContractModel
from qharness.tools.base import ToolExecutionPolicy
from qharness.utils.toml import load_toml_document, read_table


class Role(StrEnum):
    TODO_PLANNER = "todo_planner"
    STAGE_PLANNER = "stage_planner"
    ACTOR = "actor"
    JUDGE = "judge"


PROMPT_VERSION = "loop-roles-v2"
ROLE_PROMPTS = {
    Role.TODO_PLANNER: "输出初步 TodoPlan，覆盖全部任务验收项；不提前固定完整工具执行步骤。",
    Role.STAGE_PLANNER: "围绕当前 Todo 的关键缺口制定 StagePlan，明确预期结果和重新规划条件。",
    Role.ACTOR: "在当前 StagePlan 内使用工具推进工作。每轮行动前检查 stop_when 与 replan_when。"
        "达到阶段预期结果时交回 candidate，并在 matched_stop_when 原样引用满足的停止条件；"
        "关键前提改变时立即交回 needs_replan，在 matched_replan_when 原样引用条件并报告前提变化。"
        "无法继续时交回 blocked，无有效推进时交回 stalled。summary 说明依据，observation_refs 引用已提供的观察。"
        "不能更改阶段目标、不能自行宣布 Todo 或 Task 完成。工具结果中的业务失败不等于调用成功；"
        "工具批次是按顺序组织的行动，只有确定互不依赖的只读调用才可放在同一并行段。",
    Role.JUDGE: "根据可检查的证据输出验证结果，区分阶段、Todo 和任务完成；证据不足保持 inconclusive，环境错误为 error。",
}


class LoopConfig(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prompt_version: str = PROMPT_VERSION
    max_model_attempts: int = Field(default=100, ge=1)
    max_total_tokens: int | None = Field(default=1_000_000, ge=1)
    max_tool_executions: int = Field(default=200, ge=1)
    max_request_attempts: int = Field(default=3, ge=1)
    retry_delay_seconds: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    model_timeout_seconds: float = Field(default=120.0, gt=0, allow_inf_nan=False)
    max_input_bytes: int = Field(default=200_000, ge=1)
    context_window_tokens: int = Field(default=128_000, ge=1)
    max_output_tokens: int = Field(default=4096, ge=1)
    max_actor_turns: int = Field(default=32, ge=1)
    max_protocol_corrections: int = Field(default=2, ge=0)
    max_parallel_reads: int = Field(default=4, ge=1)
    max_batch_calls: int = Field(default=16, ge=1)

    @model_validator(mode="after")
    def validate_policy(self):
        if self.prompt_version not in {"loop-roles-v1", PROMPT_VERSION}:
            raise ValueError("未安装指定的 Prompt 版本")
        if self.max_output_tokens >= self.context_window_tokens:
            raise ValueError("输出预留必须小于上下文窗口")
        return self

    def snapshot(self, tools: ToolExecutionPolicy) -> dict:
        """原样保留 None 和具名 override，记录每个工具实际策略时再调用 for_tool。"""
        return {"loop": self.model_dump(mode="json"), "tools": asdict(tools)}


def load_loop_config(path: str | Path) -> LoopConfig:
    try:
        _, document = load_toml_document(path, "Loop 配置")
        return LoopConfig.model_validate(read_table(document, "loop", ""))
    except (ValueError, ValidationError) as error:
        raise LoopConfigurationError(str(error)) from error
