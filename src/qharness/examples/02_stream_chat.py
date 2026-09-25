# -*- coding: utf-8 -*-
"""示例二：执行一次流式模型调用。"""

from __future__ import annotations

import logging

from _common import create_backend, log_usage, run_example
from qharness.model.models import ChatMessage, ChatRequest, ModelEventType


_LOGGER = logging.getLogger("qharness.examples.stream_chat")


async def main() -> None:
    """逐段接收模型正文，并在结束后记录完整正文和 Token 用量。"""

    backend = create_backend()
    final_usage = None
    text_parts: list[str] = []
    try:
        request = ChatRequest(
            messages=[
                ChatMessage(
                    role="system",
                    content="你是一名 Python 专家，请使用中文回答。",
                ),
                ChatMessage(
                    role="user",
                    content="请简要解释 Python asyncio 的事件循环。",
                ),
            ],
            thinking_mode="disabled",
        )

        async for event in backend.stream(request):
            if event.type == ModelEventType.TEXT_DELTA and event.text:
                # logging 以完整记录为单位。先收集增量，避免每个 Token 都生成
                # 一条带时间戳的碎片日志，同时仍然完整验证流式接口。
                text_parts.append(event.text)
            elif event.type == ModelEventType.USAGE_UPDATED:
                final_usage = event.usage
            elif event.type == ModelEventType.RESPONSE_COMPLETED:
                final_usage = event.usage or final_usage

        _LOGGER.info("模型流式回复：\n%s", "".join(text_parts) or "<空>")
        log_usage(final_usage)
    finally:
        await backend.close()


if __name__ == "__main__":
    run_example(main)
