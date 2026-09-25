# -*- coding: utf-8 -*-
"""示例一：执行一次普通的非流式模型调用。"""

from __future__ import annotations

import logging

from _common import create_backend, log_usage, run_example
from qharness.model.models import ChatMessage, ChatRequest


_LOGGER = logging.getLogger("qharness.examples.basic_chat")


async def main() -> None:
    """发送固定问题并记录完整回复。"""

    backend = create_backend()
    try:
        # 示例变量直接写在代码中，方便修改后重复运行。
        question = "请用三点说明一个生产级 Coding Agent Harness 最重要的能力。"
        request = ChatRequest(
            messages=[
                ChatMessage(
                    role="system",
                    content="你是一名资深软件架构师，请使用中文回答。",
                ),
                ChatMessage(role="user", content=question),
            ],
            thinking_mode="disabled",
        )

        response = await backend.complete(request)
        _LOGGER.info("模型回复：\n%s", response.message.content or "<空>")
        log_usage(response.usage)
    finally:
        await backend.close()


if __name__ == "__main__":
    run_example(main)
