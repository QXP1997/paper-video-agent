# -*- coding: utf-8 -*-
"""创建多文件结构化补丁工具。"""

from __future__ import annotations

from pydantic import Field

from qharness.tools.base import Tool, ToolParameters, current_operation_id
from qharness.workspace import WorkspaceMutationService


class ApplyPatchParameters(ToolParameters):
    """多文件补丁工具参数。"""

    patch: str = Field(
        min_length=1,
        description=(
            "以 *** Begin Patch 开始、*** End Patch 结束的补丁文本；"
            "文件段使用 *** Add File、*** Update File 或 *** Delete File。"
        ),
    )


def create_apply_patch_tool(service: WorkspaceMutationService) -> Tool:
    """创建一次 Commit 应用多个文件变化的补丁工具。"""

    def apply_patch(patch: str) -> dict[str, object]:
        """解析、校验并应用补丁，返回统一操作回执。"""

        return service.apply_patch(patch, operation_id=current_operation_id()).to_dict()

    return Tool(
        name="apply_patch",
        description=(
            "使用结构化补丁新增、更新或删除一个或多个 UTF-8 文本文件。"
            "所有文件共用一个 operation_id 和一个 Dulwich Commit；任一 hunk "
            "上下文不匹配时不会应用补丁。Update File 的 hunk 行必须以空格、"
            "+ 或 - 开头。"
        ),
        parameters=ApplyPatchParameters,
        handler=apply_patch,
    )
