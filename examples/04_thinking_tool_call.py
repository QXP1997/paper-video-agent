# -*- coding: utf-8 -*-
"""示例四：DeepSeek 思考模式下的多轮工具调用。"""

from __future__ import annotations

import json
from datetime import datetime

from _common import create_backend, print_usage, run_example
from qharness.model.models import ChatMessage, ChatRequest, ToolDefinition


def get_current_time(timezone: str) -> dict[str, str]:
    """返回演示时间；此处只用于验证工具调用流程。"""

    return {
        "timezone": timezone,
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


async def main() -> None:
    """验证 reasoning_content 在工具调用后的下一轮被完整回填。"""

    backend = create_backend()
    try:
        time_tool = ToolDefinition(
            name="get_current_time",
            description="获取指定时区的当前时间。",
            parameters={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA 时区名称，例如 Asia/Shanghai。",
                    }
                },
                "required": ["timezone"],
                "additionalProperties": False,
            },
        )
        messages = [
            ChatMessage(
                role="system",
                content="你可以调用时间工具，请使用中文回答。",
            ),
            ChatMessage(role="user", content="告诉我上海当前时间，并说明现在是上午还是下午。"),
        ]

        first_response = await backend.complete(
            ChatRequest(
                messages=messages,
                tools=[time_tool],
                thinking_mode="enabled",
                reasoning_effort="medium",
            )
        )
        if not first_response.message.tool_calls:
            print("模型没有调用工具，直接回复：")
            print(first_response.message.content)
            return

        print(
            "已收到 reasoning_content：",
            bool(first_response.message.reasoning_content),
        )

        # ChatMessage 会自动保留 reasoning_content；DeepSeek 思考模式要求下一轮原样回传。
        messages.append(first_response.message)
        for tool_call in first_response.message.tool_calls:
            arguments = json.loads(tool_call.function.arguments)
            result = get_current_time(timezone=arguments["timezone"])
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
                tools=[time_tool],
                thinking_mode="enabled",
                reasoning_effort="medium",
            )
        )
        print("模型最终回复：")
        print(final_response.message.content)
        print_usage(final_response.usage)
    finally:
        await backend.close()


if __name__ == "__main__":
    run_example(main)
