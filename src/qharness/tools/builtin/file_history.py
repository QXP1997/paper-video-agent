# -*- coding: utf-8 -*-
"""查询和回滚工作区文件变更的内置工具。"""

from __future__ import annotations

from pydantic import Field

from qharness.tools.base import Tool, ToolParameters, current_operation_id
from qharness.workspace import WorkspaceMutationService


class FileOperationParameters(ToolParameters):
    """按操作编号访问文件历史时使用的公共参数。"""

    operation_id: str = Field(
        min_length=32,
        max_length=32,
        pattern=r"^[0-9a-f]{32}$",
        description="写文件工具返回的 32 位 operation_id。",
    )


class FileHistoryParameters(ToolParameters):
    """查询某个文件的私有 Git 提交历史时使用的参数。"""

    path: str = Field(description="工作区相对文件路径；文件当前可以已经被删除。")
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="最多返回多少次文件变化，默认 20，最大 100。",
    )


class WorkspaceStatusParameters(ToolParameters):
    """工作区状态工具没有业务参数。"""


def create_get_file_history_tool(service: WorkspaceMutationService) -> Tool:
    """创建直接查询 Dulwich Commit 链的文件历史工具。"""

    def get_file_history(path: str, limit: int = 20) -> dict[str, object]:
        """返回文件最近发生变化的 Commit，不读取数据库缓存 Diff。"""

        entries = service.file_history(path, limit=limit)
        return {
            "path": path.replace("\\", "/"),
            "entries": [entry.to_dict() for entry in entries],
            "count": len(entries),
        }

    return Tool(
        name="get_file_history",
        description=(
            "从 QHarness 私有 Dulwich Commit 链查询某个文件的真实版本历史，"
            "包括 Commit、父 Commit、操作说明、时间和创建/修改/删除类型。"
        ),
        parameters=FileHistoryParameters,
        handler=get_file_history,
    )


def create_get_workspace_status_tool(service: WorkspaceMutationService) -> Tool:
    """创建查看当前磁盘未提交变化的只读工具。"""

    def get_workspace_status() -> dict[str, object]:
        """比较 Dulwich HEAD 和当前工作区，不生成 Commit。"""

        status = service.workspace_status()
        return {
            "head_commit_id": status.head_commit_id,
            "is_dirty": status.is_dirty,
            "changed_files": [change.path for change in status.files],
            "files": [change.to_dict() for change in status.files],
        }

    return Tool(
        name="get_workspace_status",
        description=(
            "比较 Dulwich 私有历史 HEAD 与当前磁盘文件，返回用户、编辑器、"
            "沙箱或其他进程产生但尚未建立检查点的变化；本工具不会提交文件。"
        ),
        parameters=WorkspaceStatusParameters,
        handler=get_workspace_status,
    )


def create_rollback_file_change_tool(
    service: WorkspaceMutationService,
) -> Tool:
    """创建带并发冲突保护的文件变更回滚工具。"""

    def rollback_file_change(operation_id: str) -> dict[str, object]:
        """恢复操作前版本，并把回滚自身记录为一条新操作。"""

        return service.rollback(operation_id, rollback_operation_id=current_operation_id()).to_dict()

    return Tool(
        name="rollback_file_change",
        description=(
            "反向应用一次已完成的单文件或多文件 Commit。若其中任一文件此后"
            "又被用户或其他 Run 修改，本工具会拒绝覆盖整个回滚操作。"
        ),
        parameters=FileOperationParameters,
        handler=rollback_file_change,
    )
