# -*- coding: utf-8 -*-
"""直接检查并试运行 Anthropic SRT 沙箱。"""

from __future__ import annotations

import sys

from _common import (
    PROJECT_ROOT,
    SANDBOX_CONFIG_PATH,
    run_example,
)
from qharness.sandbox import (
    SandboxExecutionRequest,
    create_sandbox_backend,
    load_sandbox_config,
)
from qharness.workspace import WorkspaceContext


async def main() -> None:
    """预检 SRT，并在隔离环境中执行一段写死的 Python 代码。"""

    config = load_sandbox_config(SANDBOX_CONFIG_PATH)
    workspace = WorkspaceContext(PROJECT_ROOT)
    sandbox = create_sandbox_backend(config, workspace)

    status = await sandbox.check_status()
    print(f"后端：{status.backend}")
    print(f"版本：{status.version or '未知'}")
    print(f"可用：{status.available}")
    print(f"说明：{status.message}")
    if not status.available:
        if status.setup_command:
            # 这里只展示命令，不自动提权或修改 Windows 系统状态。
            executable, *arguments = status.setup_command
            quoted_arguments = " ".join(f'"{item}"' for item in arguments)
            print(f'请人工执行一次：& "{executable}" {quoted_arguments}'.rstrip())
        return

    request = SandboxExecutionRequest(
        executable=sys.executable,
        arguments=(
            "-c",
            "print('你好，代码已经在 Anthropic SRT 沙箱中运行。')",
        ),
        cwd=".",
        timeout_seconds=10.0,
        run_id="manual-sandbox-example",
        operation_id="python-hello",
    )
    result = await sandbox.execute(request)
    print(f"退出码：{result.exit_code}")
    print(f"耗时：{result.duration_seconds:.3f} 秒")
    print(f"执行成功：{result.succeeded}")
    print(f"标准输出：{result.stdout.rstrip()}")
    if result.stderr:
        print(f"标准错误：{result.stderr.rstrip()}")


if __name__ == "__main__":
    run_example(main)
