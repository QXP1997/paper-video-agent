# -*- coding: utf-8 -*-
"""演示用户后续修改存在时，历史回滚会安全地拒绝覆盖。"""

from __future__ import annotations

import json
import logging

from _common import PROJECT_ROOT, SANDBOX_WORKSPACE_ROOT, run_example
from qharness.tools import (
    FileMutationToolProvider,
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


_LOGGER = logging.getLogger("qharness.examples.file_rollback_conflict")
_DEMO_WORKSPACE = SANDBOX_WORKSPACE_ROOT / "file-conflict-demo"
_HISTORY_ROOT = PROJECT_ROOT / ".qharness" / "history"
_DEMO_FILE = "conflict.txt"


async def main() -> None:
    """修改文件后模拟外部编辑，并验证回滚返回 conflict 错误。"""

    _DEMO_WORKSPACE.mkdir(parents=True, exist_ok=True)
    workspace = WorkspaceContext(_DEMO_WORKSPACE)
    history_repository = SqliteWorkspaceHistoryRepository(
        _HISTORY_ROOT,
        tenant_id="local-demo-tenant",
        workspace_id="file-conflict-demo",
    )
    version_store = DulwichFileVersionStore(
        _HISTORY_ROOT,
        tenant_id="local-demo-tenant",
        workspace_id="file-conflict-demo",
    )
    mutation_service = WorkspaceMutationService(
        workspace,
        history_repository,
        version_store,
        run_id="example-13-run",
    )

    registry = ToolRegistry()
    await load_tool_providers(
        registry,
        [FileMutationToolProvider(mutation_service)],
    )
    executor = ToolExecutor(registry)
    state = ToolExecutionState()

    await executor.execute(
        ToolExecutionRequest(
            call_id="prepare-conflict-file",
            tool_name="write_file",
            raw_arguments={
                "path": _DEMO_FILE,
                "content": "初始内容\n",
                "overwrite": True,
            },
        ),
        state,
    )
    replace_result = await executor.execute(
        ToolExecutionRequest(
            call_id="agent-change",
            tool_name="replace_text",
            raw_arguments={
                "path": _DEMO_FILE,
                "old_text": "初始内容",
                "new_text": "Agent 修改",
            },
        ),
        state,
    )
    if not replace_result.success:
        _LOGGER.error("Agent 修改失败：%s", replace_result.to_model_content())
        return

    operation_id = json.loads(replace_result.content)["operation_id"]

    # 直接写文件用于模拟用户在编辑器里产生的新修改；它故意绕过 Harness。
    workspace.resolve_file(_DEMO_FILE).write_text(
        "用户稍后修改的内容\n",
        encoding="utf-8",
    )

    rollback_result = await executor.execute(
        ToolExecutionRequest(
            call_id="conflicting-rollback",
            tool_name="rollback_file_change",
            raw_arguments={"operation_id": operation_id},
        ),
        state,
    )
    _LOGGER.info(
        "回滚成功：%s；错误码：%s；结果：%s",
        rollback_result.success,
        rollback_result.error_code,
        rollback_result.to_model_content(),
    )
    _LOGGER.info(
        "回滚被拒绝后保留的文件内容：%s",
        workspace.resolve_file(_DEMO_FILE).read_text(encoding="utf-8").strip(),
    )


if __name__ == "__main__":
    run_example(main)
