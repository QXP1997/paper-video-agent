# -*- coding: utf-8 -*-
"""示例三：让模型选择工具，并将本地工具结果回填给模型。"""

from __future__ import annotations

import json
import logging

from _common import create_backend, log_usage, run_example
from qharness.model.models import ChatMessage, ChatRequest, ToolDefinition


_LOGGER = logging.getLogger("qharness.examples.tool_call")


def get_weather(location: str) -> dict[str, str]:
    """模拟天气工具；实际项目中可以替换成真实服务。"""

    return {
        "location": location,
        "temperature": "24℃",
        "condition": "晴",
    }


async def main() -> None:
    """完成模型请求工具、执行工具、回填结果和生成答案的闭环。"""

    backend = create_backend()
    try:
        weather_tool = ToolDefinition(
            name="get_weather",
            description="查询指定地点的当前天气。",
            parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "城市名称，例如杭州或上海。",
                    }
                },
                "required": ["location"],
                "additionalProperties": False,
            },
        )
        messages = [
            ChatMessage(
                role="system",
                content="你可以使用工具查询天气，请基于工具结果回答。",
            ),
            ChatMessage(role="user", content="杭州现在天气怎么样？"),
        ]

        first_response = await backend.complete(
            ChatRequest(
                messages=messages,
                tools=[weather_tool],
                thinking_mode="disabled",
            )
        )
        if not first_response.message.tool_calls:
            _LOGGER.info(
                "模型没有调用工具，直接回复：\n%s",
                first_response.message.content or "<空>",
            )
            return

        # 必须先回填包含 tool_calls 的 assistant 消息，再追加每个工具结果。
        messages.append(first_response.message)
        for tool_call in first_response.message.tool_calls:
            arguments = json.loads(tool_call.function.arguments)
            result = get_weather(location=arguments["location"])
            _LOGGER.info(
                "执行工具：%s，参数：%s",
                tool_call.function.name,
                arguments,
            )
            messages.append(
                ChatMessage(
                    role="tool",
                    tool_call_id=tool_call.id,
                    content=json.dumps(result, ensure_ascii=False),
                )
            )

        final_response = await backend.complete(
            ChatRequest(
                messages=messages,
                tools=[weather_tool],
                thinking_mode="disabled",
            )
        )
        _LOGGER.info(
            "模型最终回复：\n%s",
            final_response.message.content or "<空>",
        )
        log_usage(final_response.usage)
    finally:
        await backend.close()


if __name__ == "__main__":
    run_example(main)
