# -*- coding: utf-8 -*-
"""演示带 Diff、私有历史和安全回滚的文件修改工具。"""

from __future__ import annotations

import json
import logging

from _common import PROJECT_ROOT, SANDBOX_WORKSPACE_ROOT, run_example
from qharness.tools import (
    FileMutationToolProvider,
    ToolExecutionPolicy,
    ToolExecutionRequest,
    ToolExecutionState,
    ToolExecutor,
    ToolRegistry,
    load_tool_providers,
)
from qharness.workspace import (
    DulwichFileVersionStore,
    SqliteWorkspaceHistoryRepository,
    WorkspaceContext,
    WorkspaceMutationService,
)


_LOGGER = logging.getLogger("qharness.examples.file_mutation_history")

# 示例数据固定放在开发工作区中，不会修改 QHarness 项目源码。
_DEMO_WORKSPACE = SANDBOX_WORKSPACE_ROOT / "file-mutation-demo"
_HISTORY_ROOT = PROJECT_ROOT / ".qharness" / "history"
_DEMO_FILE = "hello.py"


async def main() -> None:
    """依次演示完整写入、精确替换、历史查询和安全回滚。"""

    _DEMO_WORKSPACE.mkdir(parents=True, exist_ok=True)
    workspace = WorkspaceContext(_DEMO_WORKSPACE)
    history_repository = SqliteWorkspaceHistoryRepository(
        _HISTORY_ROOT,
        tenant_id="local-demo-tenant",
        workspace_id="file-mutation-demo",
    )
    version_store = DulwichFileVersionStore(
        _HISTORY_ROOT,
        tenant_id="local-demo-tenant",
        workspace_id="file-mutation-demo",
    )
    mutation_service = WorkspaceMutationService(
        workspace,
        history_repository,
        version_store,
        run_id="example-12-run",
    )

    registry = ToolRegistry()
    await load_tool_providers(
        registry,
        [FileMutationToolProvider(mutation_service)],
    )
    executor = ToolExecutor(
        registry,
        # 示例不依赖 tool.toml，生产入口仍可通过 load_tool_policy 动态配置。
        policy=ToolExecutionPolicy(),
    )
    state = ToolExecutionState()

    write_result = await executor.execute(
        ToolExecutionRequest(
            call_id="write-demo-file",
            tool_name="write_file",
            raw_arguments={
                "path": _DEMO_FILE,
                "content": (
                    "# -*- coding: utf-8 -*-\n"
                    '"""由 QHarness 文件变更示例创建。"""\n\n'
                    'message = "你好，QHarness"\n'
                ),
                "overwrite": True,
            },
        ),
        state,
    )
    _LOGGER.info("完整写入结果：\n%s", write_result.to_model_content())
    if not write_result.success:
        return

    replace_result = await executor.execute(
        ToolExecutionRequest(
            call_id="replace-demo-text",
            tool_name="replace_text",
            raw_arguments={
                "path": _DEMO_FILE,
                "old_text": "你好，QHarness",
                "new_text": "你好，文件历史",
            },
        ),
        state,
    )
    _LOGGER.info("精确替换结果：\n%s", replace_result.to_model_content())
    if not replace_result.success:
        return

    replace_payload = json.loads(replace_result.content)
    replace_operation_id = replace_payload["operation_id"]

    inspect_result = await executor.execute(
        ToolExecutionRequest(
            call_id="inspect-demo-change",
            tool_name="inspect_file_change",
            raw_arguments={"operation_id": replace_operation_id},
        ),
        state,
    )
    _LOGGER.info("历史查询结果：\n%s", inspect_result.to_model_content())

    rollback_result = await executor.execute(
        ToolExecutionRequest(
            call_id="rollback-demo-change",
            tool_name="rollback_file_change",
            raw_arguments={"operation_id": replace_operation_id},
        ),
        state,
    )
    _LOGGER.info("回滚结果：\n%s", rollback_result.to_model_content())

    original_after_rollback = await executor.execute(
        ToolExecutionRequest(
            call_id="inspect-original-after-rollback",
            tool_name="inspect_file_change",
            raw_arguments={"operation_id": replace_operation_id},
        ),
        state,
    )
    _LOGGER.info(
        "原操作回滚后的状态：\n%s",
        original_after_rollback.to_model_content(),
    )


if __name__ == "__main__":
    run_example(main)
