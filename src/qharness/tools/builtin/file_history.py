# -*- coding: utf-8 -*-
"""查询和回滚工作区文件变更的内置工具。"""

from __future__ import annotations

from pydantic import Field

from qharness.tools.base import Tool, ToolParameters
from qharness.workspace import WorkspaceMutationService


class FileOperationParameters(ToolParameters):
    """按操作编号访问文件历史时使用的公共参数。"""

    operation_id: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
        description="写文件工具返回的 32 位 operation_id。",
    )


def create_inspect_file_change_tool(
    service: WorkspaceMutationService,
) -> Tool:
    """创建只读的文件变更详情查询工具。"""

    def inspect_file_change(operation_id: str) -> dict[str, object]:
        """返回历史操作的 Diff、版本和当前状态。"""

        return service.inspect(operation_id).to_dict()

    return Tool(
        name="inspect_file_change",
        description=(
            "按 operation_id 查询一次历史文件变更，返回操作状态、文件列表、"
            "前后版本和 Diff；本工具不会读取或修改当前文件。"
        ),
        parameters=FileOperationParameters,
        handler=inspect_file_change,
    )


def create_rollback_file_change_tool(
    service: WorkspaceMutationService,
) -> Tool:
    """创建带并发冲突保护的文件变更回滚工具。"""

    def rollback_file_change(operation_id: str) -> dict[str, object]:
        """恢复操作前版本，并把回滚自身记录为一条新操作。"""

        return service.rollback(operation_id).to_dict()

    return Tool(
        name="rollback_file_change",
        description=(
            "把一次已应用的单文件变更恢复到操作前状态。若文件此后又被用户"
            "或其他 Run 修改，本工具会拒绝覆盖，并要求重新检查当前内容。"
        ),
        parameters=FileOperationParameters,
        handler=rollback_file_change,
    )
