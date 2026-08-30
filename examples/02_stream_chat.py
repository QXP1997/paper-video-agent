# -*- coding: utf-8 -*-
"""示例二：执行一次流式模型调用。"""

from __future__ import annotations

from _common import create_backend, print_usage, run_example
from qharness.model.models import ChatMessage, ChatRequest, ModelEventType


async def main() -> None:
    """逐段打印模型正文，并在结束后显示 Token 用量。"""

    backend = create_backend()
    final_usage = None
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

        print("模型流式回复：")
        async for event in backend.stream(request):
            if event.type == ModelEventType.TEXT_DELTA and event.text:
                print(event.text, end="", flush=True)
            elif event.type == ModelEventType.USAGE_UPDATED:
                final_usage = event.usage
            elif event.type == ModelEventType.RESPONSE_COMPLETED:
                final_usage = event.usage or final_usage

        print()
        print_usage(final_usage)
    finally:
        await backend.close()


if __name__ == "__main__":
    run_example(main)
