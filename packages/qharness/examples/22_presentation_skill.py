# -*- coding: utf-8 -*-
"""用真实 QHarness Agent 从 MinerU 结果生成 Spec 和 PPTX。

这是给 PyCharm 断点调试使用的固定场景，不提供命令行参数。需要换论文或保留多次
结果时，直接修改下面“调试变量”区域。模型负责读取 Skill、分析材料、写 Spec、
调用校验脚本并发起 PPTX 渲染；本示例本身不直接执行渲染命令。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import uuid
from dataclasses import replace
from pathlib import Path

QHARNESS_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
for source_root in (QHARNESS_ROOT / "src", REPOSITORY_ROOT / "src"):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from qharness.backends import OpenAICompatibleBackend  # noqa: E402
from qharness.logging import configure_logging  # noqa: E402
from qharness.loop.config import load_loop_config  # noqa: E402
from qharness.model.config import ModelBackendConfig, load_model_config  # noqa: E402
from qharness.persistence import DatabaseManager, load_database_config  # noqa: E402
from qharness.run import RunService, create_loop_services, create_run_context  # noqa: E402
from qharness.sandbox import load_sandbox_config  # noqa: E402
from qharness.skills import SkillCatalog, SkillToolProvider  # noqa: E402
from qharness.tools import (  # noqa: E402
    BuiltinToolProvider,
    FileMutationToolProvider,
    SandboxToolProvider,
    ToolExecutionPolicy,
    ToolExecutor,
    ToolPolicyOverride,
    ToolRegistry,
    ToolRuntimePolicy,
    load_tool_providers,
)
from qharness.workspace import load_workspace_history_config  # noqa: E402

from presentation_agent import (  # noqa: E402
    build_presentation_check_catalog,
    build_presentation_contract,
    load_presentation_skills,
)

# ---------------------------------------------------------------------------
# 调试变量：换论文或开始新一轮调试时，只修改这里。
# ---------------------------------------------------------------------------
PAPER_ROOT = REPOSITORY_ROOT / "paper" / "2210.03629v3"
MINERU_SOURCE = PAPER_ROOT / "output" / "mineru"
DEBUG_RUN_NAME = "react-presentation-001"
WORKSPACE_ROOT = PAPER_ROOT / "output" / "presentation_agent_debug" / DEBUG_RUN_NAME

MODEL_CONFIG = QHARNESS_ROOT / "config" / "model.toml"
FALLBACK_ENV_FILE = REPOSITORY_ROOT / ".env"
DATABASE_CONFIG = QHARNESS_ROOT / "config" / "database.example.toml"
HISTORY_CONFIG = QHARNESS_ROOT / "config" / "history.example.toml"
SANDBOX_CONFIG = QHARNESS_ROOT / "config" / "sandbox.example.toml"
LOOP_CONFIG = QHARNESS_ROOT / "config" / "loop.example.toml"

PPTXGENJS_NODE_MODULES = (
    QHARNESS_ROOT
    / ".qharness"
    / "runtime"
    / "packages"
    / "presentation-pptxgenjs-4.0.1"
    / "node_modules"
)

SPEC_PATH = "output/deck_spec.json"
PPTX_PATH = "output/react-paper-agent.pptx"
TARGET_AUDIENCE = "希望快速理解 ReAct 论文核心方法与实验结论的中文技术读者"
SLIDE_COUNT = "6 至 8 页"
STREAM_MODEL_OUTPUT = True


_LOGGER = logging.getLogger("qharness.examples.presentation_agent")


def _configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def _prepare_workspace() -> None:
    """把已有 MinerU 结果作为不可变输入快照放进独立 Agent 工作区。"""

    if not (MINERU_SOURCE / "full.md").is_file():
        raise FileNotFoundError(f"MinerU 解析结果不存在: {MINERU_SOURCE}")
    spec = WORKSPACE_ROOT / SPEC_PATH
    pptx = WORKSPACE_ROOT / PPTX_PATH
    if spec.exists() or pptx.exists():
        raise FileExistsError(
            f"本轮输出已经存在。为避免覆盖，请修改脚本顶部的 DEBUG_RUN_NAME：{WORKSPACE_ROOT}"
        )

    input_root = WORKSPACE_ROOT / "input" / "mineru"
    if not input_root.exists():
        input_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(MINERU_SOURCE, input_root)
    (WORKSPACE_ROOT / "output").mkdir(parents=True, exist_ok=True)


def _require_local_configuration() -> None:
    _load_debug_model_config().require_api_key()
    pptxgen_entry = PPTXGENJS_NODE_MODULES / "pptxgenjs" / "dist" / "pptxgen.es.js"
    if not pptxgen_entry.is_file():
        raise FileNotFoundError(
            "缺少 QHarness 托管的 PptxGenJS，请检查 PPTXGENJS_NODE_MODULES："
            f"{PPTXGENJS_NODE_MODULES}"
        )


def _read_env_values(path: Path) -> dict[str, str]:
    """读取调试示例需要的少量 dotenv 字段，不修改进程环境。"""

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in {
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_BASE_URL",
            "DEEPSEEK_MODEL",
        }:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _load_debug_model_config() -> ModelBackendConfig:
    """优先使用 QHarness TOML，缺失时复用工作台现有的 DeepSeek 配置。"""

    if MODEL_CONFIG.is_file():
        return load_model_config(MODEL_CONFIG)
    values = {
        **(_read_env_values(FALLBACK_ENV_FILE) if FALLBACK_ENV_FILE.is_file() else {}),
        **{
            key: value
            for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL")
            if (value := os.environ.get(key, "").strip())
        },
    }
    api_key = values.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise FileNotFoundError(
            "没有可用的模型配置：请创建 packages/qharness/config/model.toml，"
            "或在仓库根目录 .env 中配置 DEEPSEEK_API_KEY。"
        )
    return ModelBackendConfig(
        provider="deepseek",
        base_url=values.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        api_key=api_key,
        model=values.get("DEEPSEEK_MODEL", "deepseek-flash"),
        timeout_seconds=120.0,
        max_retries=2,
        temperature=0.2,
        max_tokens=16_384,
        stream_include_usage=True,
        thinking_mode="disabled",
    )


def _tool_policy() -> ToolExecutionPolicy:
    """调试场景允许 Agent 分页读论文，并给沙箱渲染留出足够时间。"""

    return ToolExecutionPolicy(
        max_total_calls=200,
        max_concurrency=4,
        defaults=ToolRuntimePolicy(
            max_calls=80,
            timeout_seconds=120.0,
            max_argument_chars=200_000,
            max_result_chars=200_000,
        ),
        tool_overrides={
            "run_command": ToolPolicyOverride(
                max_calls=20,
                timeout_seconds=180.0,
                max_concurrency=1,
            ),
        },
    )


def _objective() -> str:
    return f"""根据工作区中已经由 MinerU 解析好的论文材料，制作一份 {SLIDE_COUNT} 的中文技术演示文稿。

目标听众：{TARGET_AUDIENCE}。
输入入口：input/mineru/full.md；图片位于 input/mineru/images，布局信息位于 input/mineru/layout.json。
输出：{SPEC_PATH} 和 {PPTX_PATH}。

开始设计前必须调用 read_skill_resource，完整读取 create-presentation 的
references/spec-design.md 与 references/pptx-render.md。先通过 list_directory、search_text、read_file
理解论文结构、关键实验结论以及 MinerU 图片 caption，再生成 SlideDeckSpec。只使用材料中可定位的事实、
数字和图片。资产 path 必须相对 Spec 所在目录填写，因此 MinerU 图片写成 ../input/mineru/images/<文件名>。
不得沿用工作区外已有的 deck_spec.json 或 PPTX。Spec 写入后，先调用 Skill 自带的
scripts/validate_spec.py 校验；校验通过后，由你调用 scripts/render_pptx.py 生成 PPTX，最后调用
scripts/inspect_pptx.py 检查产物。不要只写计划或命令说明，必须实际调用工具生成两个输出文件。
"""


async def main() -> None:
    _configure_console_encoding()
    _require_local_configuration()
    _prepare_workspace()

    os.environ["RUNTIME_NODE_MODULES"] = str(PPTXGENJS_NODE_MODULES)
    configure_logging(
        level=logging.DEBUG,
        log_file=WORKSPACE_ROOT / ".qharness" / "logs" / "presentation-agent.log",
    )
    database = DatabaseManager(load_database_config(DATABASE_CONFIG))
    backend: OpenAICompatibleBackend | None = None
    try:
        database.initialize()
        catalog = SkillCatalog(
            database.session_factory,
            skill_root=WORKSPACE_ROOT / ".qharness" / "skills",
        )
        run_id = f"presentation-agent-{uuid.uuid4().hex}"
        contract = build_presentation_contract(
            task_id=run_id,
            objective=_objective(),
            skill_catalog=catalog,
            include_pptx_skill=True,
            output_path=SPEC_PATH,
            pptx_output_path=PPTX_PATH,
        )
        skills = load_presentation_skills(catalog)
        checks = build_presentation_check_catalog(
            SPEC_PATH,
            pptx_output_path=PPTX_PATH,
        )

        sandbox_config = load_sandbox_config(SANDBOX_CONFIG)
        sandbox_config = replace(
            sandbox_config,
            filesystem=replace(
                sandbox_config.filesystem,
                allow_read=(
                    *sandbox_config.filesystem.allow_read,
                    str(PPTXGENJS_NODE_MODULES),
                ),
            ),
        )
        context = create_run_context(
            tenant_id="local-debug",
            workspace_id=DEBUG_RUN_NAME,
            run_id=run_id,
            workspace_root=WORKSPACE_ROOT,
            sandbox_config=sandbox_config,
            database_manager=database,
            history_config=load_workspace_history_config(HISTORY_CONFIG),
        )
        status = await context.sandbox.prepare()
        if not status.available:
            raise RuntimeError(f"SRT 沙箱不可用，请先运行示例 08 完成初始化：{status.message}")

        registry = ToolRegistry()
        providers = [
            BuiltinToolProvider(context.workspace),
            FileMutationToolProvider(context.mutation_service),
            SandboxToolProvider(context),
            SkillToolProvider.from_contract(
                contract,
                {skill.code: skill for skill in skills},
            ),
        ]
        await load_tool_providers(registry, providers)
        executor = ToolExecutor(registry, policy=_tool_policy())
        backend = OpenAICompatibleBackend(_load_debug_model_config())
        loop_config = load_loop_config(LOOP_CONFIG).model_copy(update={"max_output_tokens": 16_384})
        services = create_loop_services(
            context,
            backend=backend,
            executor=executor,
            database_manager=database,
            config=loop_config,
            contract=contract,
        )
        lifecycle = RunService(services)

        _LOGGER.info("Agent Run：%s", run_id)
        _LOGGER.info("工作区：%s", WORKSPACE_ROOT)
        _LOGGER.info("激活 Skill：%s", ", ".join(skill.code for skill in skills))
        state = await lifecycle.run(
            checks,
            stream=STREAM_MODEL_OUTPUT,
            observations=(
                "用户已确认 PDF 的 MinerU 解析完成；本任务从工作区 input/mineru 开始，不再解析 PDF。",
                "输入快照只作证据来源，不修改 input 目录。",
            ),
        )

        _LOGGER.info("最终阶段：%s", state.phase)
        if state.wait_reason:
            _LOGGER.warning("等待原因：%s", state.wait_reason)
        _LOGGER.info("Spec：%s", WORKSPACE_ROOT / SPEC_PATH)
        _LOGGER.info("PPTX：%s", WORKSPACE_ROOT / PPTX_PATH)
        if str(state.phase) != "completed":
            raise RuntimeError(f"Agent 未完成任务，当前阶段为 {state.phase}: {state.wait_reason}")
    finally:
        if backend is not None:
            await backend.close()
        database.close()


if __name__ == "__main__":
    asyncio.run(main())
