# -*- coding: utf-8 -*-
"""直接检查并试运行 Anthropic SRT 沙箱。"""

from __future__ import annotations

import logging

from _common import (
    SANDBOX_CONFIG_PATH,
    SANDBOX_WORKSPACE_ROOT,
    PROJECT_ROOT,
    run_example,
)
from qharness.logging import configure_logging
from qharness.sandbox import (
    SandboxExecutionRequest,
    create_sandbox_backend,
    load_sandbox_config,
)
from qharness.workspace import WorkspaceContext


_LOGGER = logging.getLogger("qharness.examples.srt_sandbox")


async def main() -> None:
    """预检 SRT，并用 QHarness 内置 Python 执行一段写死的代码。"""

    config = load_sandbox_config(SANDBOX_CONFIG_PATH)
    configure_logging(
        level=logging.DEBUG if config.srt.debug else logging.INFO,
        log_file=PROJECT_ROOT / ".qharness" / "logs" / "qharness.log",
    )

    # 示例使用独立托管目录，不把 QHarness 源码仓库作为 Agent 工作区。
    # mkdir 使用 exist_ok=True，允许该示例被重复执行。
    SANDBOX_WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    workspace = WorkspaceContext(SANDBOX_WORKSPACE_ROOT)
    sandbox = create_sandbox_backend(config, workspace)

    status = await sandbox.check_status()
    _LOGGER.info("后端：%s", status.backend)
    _LOGGER.info("版本：%s", status.version or "未知")
    _LOGGER.info("可用：%s", status.available)
    _LOGGER.info("说明：%s", status.message)
    if not status.available:
        if status.setup_command:
            # 这里只展示命令，不自动提权或修改 Windows 系统状态。
            executable, *arguments = status.setup_command
            quoted_arguments = " ".join(f'"{item}"' for item in arguments)
            _LOGGER.warning(
                '请人工执行一次：& "%s" %s',
                executable,
                quoted_arguments,
            )
        return

    request = SandboxExecutionRequest(
        # Agent 只需要使用逻辑名称 python。沙箱后端会自动替换成当前平台
        # 随 QHarness 分发的解释器路径，不会调用用户的 Anaconda 或系统 Python。
        executable="python",
        arguments=(
            "-c",
            (
                "import json, sys; "
                "sys.stdout.write("
                "'你好，代码已经在 Anthropic SRT 沙箱中运行。\\n' + "
                "'收到的参数：' + "
                "json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')"
            ),
            "包含 空格",
            "单引号'",
            "符号&|<>$",
        ),
        cwd=".",
        timeout_seconds=30.0,
        run_id="manual-sandbox-example",
        operation_id="python-hello",
    )
    result = await sandbox.execute(request)
    _LOGGER.info("退出码：%s", result.exit_code)
    _LOGGER.info("耗时：%.3f 秒", result.duration_seconds)
    _LOGGER.info("执行成功：%s", result.succeeded)
    _LOGGER.info("标准输出：\n%s", result.stdout.rstrip() or "<空>")
    if result.stderr:
        # 开启 SRT debug 时，原生日志已经被后端实时转发；这里仍保留完整
        # stderr 供失败诊断，但只在执行失败时输出，避免成功路径重复刷屏。
        # SRT 原生日志中的完整命令可能包含用户参数或密钥，写日志前必须隐藏。
        if not result.succeeded:
            _LOGGER.error(
                "标准错误：\n%s",
                _redact_srt_commands(result.stderr).rstrip(),
            )


def _redact_srt_commands(stderr: str) -> str:
    """隐藏 SRT debug 输出中可能携带敏感参数的完整目标命令。"""

    sensitive_markers = ("Command string mode (-c):", "Original command:")
    lines: list[str] = []
    for line in stderr.splitlines():
        if any(marker in line for marker in sensitive_markers):
            lines.append("[SandboxDebug] 命令内容已隐藏")
        else:
            lines.append(line)
    return "\n".join(lines)


if __name__ == "__main__":
    run_example(main)
