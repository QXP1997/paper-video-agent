# -*- coding: utf-8 -*-
"""示例十：使用逻辑名称 node 在 SRT 沙箱中执行 JavaScript。"""

from __future__ import annotations

import logging

from _common import (
    PROJECT_ROOT,
    SANDBOX_CONFIG_PATH,
    SANDBOX_WORKSPACE_ROOT,
    run_example,
)
from qharness.logging import configure_logging
from qharness.sandbox import (
    SandboxExecutionRequest,
    create_sandbox_backend,
    load_sandbox_config,
)
from qharness.workspace import WorkspaceContext


_LOGGER = logging.getLogger("qharness.examples.node_sandbox")


async def main() -> None:
    """自动准备 Node 和 SRT，然后执行一段写死的 JavaScript。"""

    config = load_sandbox_config(SANDBOX_CONFIG_PATH)
    configure_logging(
        level=logging.DEBUG if config.srt.debug else logging.INFO,
        log_file=PROJECT_ROOT / ".qharness" / "logs" / "qharness.log",
    )
    SANDBOX_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    sandbox = create_sandbox_backend(
        config,
        WorkspaceContext(SANDBOX_WORKSPACE_ROOT),
    )

    status = await sandbox.prepare()
    _LOGGER.info(
        "SRT 状态：available=%s，version=%s，说明=%s",
        status.available,
        status.version or "未知",
        status.message,
    )
    if status.setup_required:
        setup = await sandbox.setup()
        _LOGGER.info("初始化结果：%s", setup.message)
        status = setup.status or status
    if not status.available:
        _LOGGER.error("沙箱尚不可用，已停止示例执行：%s", status.message)
        return

    result = await sandbox.execute(
        SandboxExecutionRequest(
            # 逻辑名称 node 会被解析为 QHarness 托管 Node 的绝对路径。
            executable="node",
            arguments=(
                "-e",
                (
                    "const payload = {runtime: process.version, sandbox: true};"
                    "process.stdout.write('你好，JavaScript 已在 SRT 中运行。\\n' + "
                    "JSON.stringify(payload) + '\\n');"
                ),
            ),
            cwd=".",
            timeout_seconds=30.0,
            run_id="manual-node-sandbox-example",
            operation_id="node-hello",
        )
    )
    _LOGGER.info("退出码：%s", result.exit_code)
    _LOGGER.info("耗时：%.3f 秒", result.duration_seconds)
    _LOGGER.info("执行成功：%s", result.succeeded)
    _LOGGER.info("标准输出：\n%s", result.stdout.rstrip() or "<空>")
    if result.stderr and not result.succeeded:
        _LOGGER.error("标准错误：\n%s", result.stderr.rstrip())


if __name__ == "__main__":
    run_example(main)
