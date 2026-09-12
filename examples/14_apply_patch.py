# -*- coding: utf-8 -*-
"""演示一次补丁修改多个文件、查询 Commit 历史并整体回滚。"""

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
    SqlAlchemyWorkspaceHistoryRepository,
    WorkspaceContext,
    WorkspaceMutationService,
    load_workspace_history_config,
)


_LOGGER = logging.getLogger("qharness.examples.apply_patch")
_DEMO_WORKSPACE = SANDBOX_WORKSPACE_ROOT / "apply-patch-demo"
_HISTORY_CONFIG_PATH = PROJECT_ROOT / "config" / "history.example.toml"


async def main() -> None:
    """应用包含更新和新增的补丁，再按 operation_id 回滚两个文件。"""

    _DEMO_WORKSPACE.mkdir(parents=True, exist_ok=True)
    source_file = _DEMO_WORKSPACE / "app.py"
    added_file = _DEMO_WORKSPACE / "notes.md"

    # 每次运行先构造相同的磁盘输入；已有私有 HEAD 会把这里识别为外部变化。
    source_file.write_text(
        "def greet():\n    return \"旧内容\"\n",
        encoding="utf-8",
    )
    added_file.unlink(missing_ok=True)

    workspace = WorkspaceContext(_DEMO_WORKSPACE)
    history_config = load_workspace_history_config(_HISTORY_CONFIG_PATH)
    history_repository = SqlAlchemyWorkspaceHistoryRepository.from_config(
        history_config,
        tenant_id="local-demo-tenant",
        workspace_id="apply-patch-demo",
    )
    version_store = DulwichFileVersionStore(
        history_config.storage_root,
        tenant_id="local-demo-tenant",
        workspace_id="apply-patch-demo",
    )
    service = WorkspaceMutationService(
        workspace,
        history_repository,
        version_store,
        run_id="example-14-run",
    )

    registry = ToolRegistry()
    await load_tool_providers(registry, [FileMutationToolProvider(service)])
    executor = ToolExecutor(registry, policy=ToolExecutionPolicy())
    state = ToolExecutionState()

    patch_text = """*** Begin Patch
*** Update File: app.py
@@
 def greet():
-    return "旧内容"
+    return "补丁已经生效"
*** Add File: notes.md
+# 补丁示例
+
+这个文件与 app.py 属于同一个 Commit。
*** End Patch"""
    apply_result = await executor.execute(
        ToolExecutionRequest(
            call_id="apply-multi-file-patch",
            tool_name="apply_patch",
            raw_arguments={"patch": patch_text},
        ),
        state,
    )
    _LOGGER.info("多文件补丁结果：\n%s", apply_result.to_model_content())
    if not apply_result.success:
        return

    operation_id = json.loads(apply_result.content)["operation_id"]
    inspect_result = await executor.execute(
        ToolExecutionRequest(
            call_id="inspect-patch-operation",
            tool_name="inspect_file_change",
            raw_arguments={"operation_id": operation_id},
        ),
        state,
    )
    _LOGGER.info("从 Commit 实时计算的操作详情：\n%s", inspect_result.content)

    history_result = await executor.execute(
        ToolExecutionRequest(
            call_id="get-app-history",
            tool_name="get_file_history",
            raw_arguments={"path": "app.py", "limit": 10},
        ),
        state,
    )
    _LOGGER.info("app.py 的 Dulwich 历史：\n%s", history_result.content)

    rollback_result = await executor.execute(
        ToolExecutionRequest(
            call_id="rollback-multi-file-patch",
            tool_name="rollback_file_change",
            raw_arguments={"operation_id": operation_id},
        ),
        state,
    )
    _LOGGER.info("多文件整体回滚结果：\n%s", rollback_result.to_model_content())

    invalid_patch = """*** Begin Patch
*** Update File: app.py
@@
-这段上下文并不存在
+因此补丁必须被拒绝
*** End Patch"""
    invalid_result = await executor.execute(
        ToolExecutionRequest(
            call_id="reject-stale-patch",
            tool_name="apply_patch",
            raw_arguments={"patch": invalid_patch},
        ),
        state,
    )
    _LOGGER.info(
        "上下文不匹配的补丁（预期 conflict）：\n%s",
        invalid_result.to_model_content(),
    )

    # 模拟用户通过编辑器直接改文件，状态工具应报告 dirty，但不自动提交。
    source_file.write_text(
        "def greet():\n    return \"用户在工具外修改\"\n",
        encoding="utf-8",
    )
    status_result = await executor.execute(
        ToolExecutionRequest(
            call_id="get-workspace-status",
            tool_name="get_workspace_status",
            raw_arguments={},
        ),
        state,
    )
    _LOGGER.info("用户外部编辑后的工作区状态：\n%s", status_result.content)

    # 恢复演示输入，避免示例结束后给工作区留下未预期内容。
    source_file.write_text(
        "def greet():\n    return \"旧内容\"\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    run_example(main)
