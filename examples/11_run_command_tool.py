# -*- coding: utf-8 -*-
"""示例十一：执行完整沙箱命令并记录命令产生的文件变化。"""

from __future__ import annotations

import json
import logging

from _common import (
    DATABASE_CONFIG_PATH,
    HISTORY_CONFIG_PATH,
    PROJECT_ROOT,
    SANDBOX_CONFIG_PATH,
    TOOL_CONFIG_PATH,
    run_example,
)
from qharness.logging import configure_logging
from qharness.persistence import DatabaseManager, load_database_config
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
from qharness.workspace import load_workspace_history_config


_LOGGER = logging.getLogger("qharness.examples.run_command_tool")


async def main() -> None:
    """执行带管道的 Node 命令，并展示自动生成的工作区 Commit。"""

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
    database_manager = DatabaseManager(
        load_database_config(DATABASE_CONFIG_PATH)
    )
    history_config = load_workspace_history_config(HISTORY_CONFIG_PATH)
    context = create_run_context(
        tenant_id="tenant-demo",
        workspace_id="workspace-demo",
        run_id="run-command-example",
        workspace_root=workspace_root,
        sandbox_config=sandbox_config,
        # RuntimeManager 可以在同一进程的多个 Run 之间复用；工作区、沙箱策略
        # 实例、调用计数和取消事件仍然属于各自 RunContext。
        runtime_manager=runtime_manager,
        database_manager=database_manager,
        history_config=history_config,
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
        call_id="call-run-command-001",
        tool_name="run_command",
        raw_arguments={
            "command": (
                "node -e \"const fs = require('fs'); const value = "
                "{message: 'run_command 执行成功', "
                "runtime: process.version}; "
                "const text = JSON.stringify(value); "
                "fs.writeFileSync('command-output.json', text + '\\n', 'utf8'); "
                "process.stdout.write(text + '\\n');\" "
                "| Select-String 'run_command'"
            ),
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
            "命令产生的工作区变化：%s",
            process_result["workspace_change"],
        )
    _LOGGER.info(
        "本次 Run 工具计数：total=%s，by_tool=%s",
        context.tool_state.total_calls,
        context.tool_state.calls_by_tool,
    )
    database_manager.close()


if __name__ == "__main__":
    run_example(main)
