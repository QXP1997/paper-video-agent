# -*- coding: utf-8 -*-
"""示例十一：通过独立策略控制的 run_process 工具执行沙箱进程。"""

from __future__ import annotations

import json
import logging

from _common import (
    PROJECT_ROOT,
    SANDBOX_CONFIG_PATH,
    TOOL_CONFIG_PATH,
    run_example,
)
from qharness.logging import configure_logging
from qharness.run import create_run_context
from qharness.runtime import RuntimeManager
from qharness.sandbox import load_sandbox_config
from qharness.tools import (
    BuiltinToolProvider,
    SandboxToolProvider,
    ToolExecutor,
    ToolRegistry,
    load_tool_policy,
    load_tool_providers,
)


_LOGGER = logging.getLogger("qharness.examples.run_process_tool")


async def main() -> None:
    """创建单租户 RunContext，并通过 ToolExecutor 执行 Node 进程。"""

    sandbox_config = load_sandbox_config(SANDBOX_CONFIG_PATH)
    configure_logging(
        level=logging.DEBUG if sandbox_config.srt.debug else logging.INFO,
        log_file=PROJECT_ROOT / ".qharness" / "logs" / "qharness.log",
    )

    # 工作空间由本次 Run 动态传入，不写入全局沙箱配置。将来同一进程可以
    # 为不同 tenant_id 创建多个 RunContext，并分别绑定不同目录。
    workspace_root = (
        PROJECT_ROOT
        / ".qharness"
        / "workspaces"
        / "tenant-demo"
        / "workspace-demo"
    )
    workspace_root.mkdir(parents=True, exist_ok=True)
    runtime_manager = RuntimeManager(sandbox_config.runtime_directory.parent)
    context = create_run_context(
        tenant_id="tenant-demo",
        workspace_id="workspace-demo",
        run_id="run-process-example",
        workspace_root=workspace_root,
        sandbox_config=sandbox_config,
        # RuntimeManager 可以在同一进程的多个 Run 之间复用；工作区、沙箱策略
        # 实例、调用计数和取消事件仍然属于各自 RunContext。
        runtime_manager=runtime_manager,
    )

    status = await context.sandbox.prepare()
    if status.setup_required:
        setup = await context.sandbox.setup()
        _LOGGER.info("SRT 初始化结果：%s", setup.message)
        status = setup.status or status
    if not status.available:
        _LOGGER.error("沙箱尚不可用：%s", status.message)
        return

    registry = ToolRegistry()
    tools = await load_tool_providers(
        registry,
        [
            BuiltinToolProvider(context.workspace),
            SandboxToolProvider(context),
        ],
    )
    _LOGGER.info("本次 Run 动态加载工具：%s", [tool.name for tool in tools])
    executor = ToolExecutor(
        registry,
        policy=load_tool_policy(TOOL_CONFIG_PATH),
    )

    request = context.create_tool_request(
        call_id="call-run-process-001",
        tool_name="run_process",
        raw_arguments={
            "executable": "node",
            "arguments": [
                "-e",
                (
                    "const value = {message: 'run_process 执行成功', "
                    "runtime: process.version};"
                    "process.stdout.write(JSON.stringify(value) + '\\n');"
                ),
            ],
            "cwd": ".",
        },
        metadata={"source": "example"},
    )
    result = await executor.execute(request, context.tool_state)
    _LOGGER.info(
        "工具执行结果：success=%s，error_code=%s\n%s",
        result.success,
        result.error_code,
        result.to_model_content(),
    )
    if result.success:
        process_result = json.loads(result.content)
        _LOGGER.info(
            "目标进程：succeeded=%s，exit_code=%s，operation_id=%s",
            process_result["succeeded"],
            process_result["exit_code"],
            process_result["operation_id"],
        )
    _LOGGER.info(
        "本次 Run 工具计数：total=%s，by_tool=%s",
        context.tool_state.total_calls,
        context.tool_state.calls_by_tool,
    )


if __name__ == "__main__":
    run_example(main)
