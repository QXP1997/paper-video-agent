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

# 当 status 显示正常但实际执行提示 Windows 状态数据库损坏时，可临时改为
# True。程序会再次显示确认窗口，并在用户同意后通过 UAC 执行修复安装。
FORCE_SRT_REPAIR = False


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

    # prepare 会自动下载并校验托管 Node 和固定版本 SRT，不需要用户手动 npm。
    status = await sandbox.prepare()
    _LOGGER.info("后端：%s", status.backend)
    _LOGGER.info("版本：%s", status.version or "未知")
    _LOGGER.info("可用：%s", status.available)
    _LOGGER.info("说明：%s", status.message)
    if status.setup_required or FORCE_SRT_REPAIR:
        # 系统级初始化不会静默执行：QHarness 先显示说明窗口，用户点击“是”
        # 后 Windows 才显示 UAC。取消不会使程序降级到宿主机直接执行。
        setup = await sandbox.setup(force=FORCE_SRT_REPAIR)
        _LOGGER.info("初始化结果：%s", setup.message)
        status = setup.status or status
    if not status.available:
        _LOGGER.error("沙箱尚不可用，已停止示例执行：%s", status.message)
        return

    request = SandboxExecutionRequest(
        # Agent 直接提供完整 PowerShell 命令；python 裸名称由沙箱 PATH
        # 解析为 QHarness 托管解释器，管道由 SRT 内部 Shell 处理。
        command=(
            "python -c \"import sys; "
            "print('你好，代码已经在 Anthropic SRT 沙箱中运行。'); "
            "print('参数数量：', len(sys.argv) - 1)\" "
            "'包含 空格' '符号&|<>$' | Select-String 'Anthropic|参数'"
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
