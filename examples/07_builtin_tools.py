  # -*- coding: utf-8 -*-
"""示例七：通过 Provider 动态加载并执行工作区只读工具。"""

from __future__ import annotations

import json

from _common import PROJECT_ROOT, TOOL_CONFIG_PATH, run_example
from qharness.tools import (
    BuiltinToolProvider,
    ToolExecutionRequest,
    ToolExecutionState,
    ToolExecutor,
    ToolRegistry,
    load_tool_policy,
    load_tool_providers,
)
from qharness.workspace import WorkspaceContext


async def main() -> None:
    """动态注册内置工具，并依次验证目录、文件和文本搜索。"""

    workspace = WorkspaceContext(PROJECT_ROOT)
    registry = ToolRegistry()
    loaded_tools = await load_tool_providers(
        registry,
        [BuiltinToolProvider(workspace)],
    )
    print(f"动态加载工具：{[tool.name for tool in loaded_tools]}\n")

    executor = ToolExecutor(
        registry,
        policy=load_tool_policy(TOOL_CONFIG_PATH),
    )
    state = ToolExecutionState()
    first_page_request = ToolExecutionRequest(
        call_id="call_list_directory_page_1",
        tool_name="list_directory",
        raw_arguments={
            "path": "src/qharness",
            "recursive": False,
            "page_size": 3,
        },
    )
    first_page_result = await executor.execute(first_page_request, state)
    print("工具：list_directory（第一页）")
    print(first_page_result.to_model_content())
    print()

    # 如果存在下一页，直接把工具返回的游标传回，不需要自行解析游标内容。
    if first_page_result.success:
        first_page = json.loads(first_page_result.content)
        next_cursor = first_page.get("next_cursor")
        if next_cursor is not None:
            second_page_result = await executor.execute(
                ToolExecutionRequest(
                    call_id="call_list_directory_page_2",
                    tool_name="list_directory",
                    raw_arguments={
                        "path": "src/qharness",
                        "recursive": False,
                        "page_size": 3,
                        "cursor": next_cursor,
                    },
                ),
                state,
            )
            print("工具：list_directory（第二页）")
            print(second_page_result.to_model_content())
            print()

    requests = [
        ToolExecutionRequest(
            call_id="call_read_file",
            tool_name="read_file",
            raw_arguments={
                "path": "README.md",
                "start_line": 1,
                "max_lines": 8,
            },
        ),
    ]
    if registry.get("search_text") is not None:
        requests.append(
            ToolExecutionRequest(
                call_id="call_search_text",
                tool_name="search_text",
                raw_arguments={
                    "query": "WorkspaceContext",
                    "path": "src",
                    "glob": ["*.py"],
                    "max_results": 5,
                },
            )
        )

    for request in requests:
        result = await executor.execute(request, state)
        print(f"工具：{request.tool_name}，成功：{result.success}")
        print(result.to_model_content())
        print()


if __name__ == "__main__":
    run_example(main)
