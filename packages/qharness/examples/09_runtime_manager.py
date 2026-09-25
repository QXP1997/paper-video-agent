# -*- coding: utf-8 -*-
"""示例九：检查并解析 QHarness 托管语言运行时。"""

from __future__ import annotations

import asyncio
import logging

from _common import PROJECT_ROOT, run_example
from qharness.runtime import (
    RuntimeManager,
    RuntimeName,
    RuntimePreparationProgress,
)


_LOGGER = logging.getLogger("qharness.examples.runtime_manager")


async def main() -> None:
    """检查 Python 状态，确保安装完成，并展示逻辑名称解析结果。"""

    manager = RuntimeManager(PROJECT_ROOT / ".qharness" / "runtime")

    before = manager.check_status(RuntimeName.PYTHON)
    _LOGGER.info(
        "Python 准备前状态：available=%s，install_required=%s，说明=%s",
        before.available,
        before.install_required,
        before.message,
    )

    # 摘要计算和解压可能涉及大量磁盘 I/O，异步入口应放到工作线程执行。
    python_path = await asyncio.to_thread(manager.ensure, RuntimeName.PYTHON)
    _LOGGER.info("Python 实际路径：%s", python_path)
    _LOGGER.info(
        "逻辑名称 python 的解析结果：%s",
        manager.resolve_executable("python"),
    )

    after = manager.check_status(RuntimeName.PYTHON)
    _LOGGER.info(
        "Python 准备后状态：available=%s，version=%s，说明=%s",
        after.available,
        after.version,
        after.message,
    )

    # Node 归档不随 Python wheel 一起塞进安装包。首次 ensure 会从清单固定的
    # HTTPS 地址下载、校验 SHA-256 并原子安装；只读状态检查不会触发网络访问。
    node_before = manager.check_status(RuntimeName.NODE)
    _LOGGER.info(
        "Node 准备前状态：available=%s，download_required=%s，说明=%s",
        node_before.available,
        node_before.download_required,
        node_before.message,
    )
    node_path = await asyncio.to_thread(
        manager.ensure,
        RuntimeName.NODE,
        progress_callback=_log_runtime_progress,
    )
    process = await asyncio.create_subprocess_exec(
        str(node_path),
        "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    _LOGGER.info(
        "Node 实际路径：%s；版本输出：%s；错误输出：%s",
        node_path,
        stdout.decode("utf-8", errors="replace").strip() or "<空>",
        stderr.decode("utf-8", errors="replace").strip() or "<空>",
    )


def _log_runtime_progress(progress: RuntimePreparationProgress) -> None:
    """把下载器回调转换为标准日志，供未来客户端进度条复用。"""

    if progress.completed_bytes is None or progress.total_bytes is None:
        _LOGGER.info("运行时准备：phase=%s，%s", progress.phase, progress.message)
        return
    percentage = progress.completed_bytes * 100 / progress.total_bytes
    _LOGGER.info(
        "运行时准备：phase=%s，进度=%.1f%%（%s/%s 字节）",
        progress.phase,
        percentage,
        progress.completed_bytes,
        progress.total_bytes,
    )


if __name__ == "__main__":
    run_example(main)
