# -*- coding: utf-8 -*-
"""提供工作区写入、变更查询和安全回滚工具。"""

from __future__ import annotations

from dataclasses import dataclass

from qharness.tools.base import Tool
from qharness.tools.builtin.apply_patch import create_apply_patch_tool
from qharness.tools.builtin.file_history import (
    create_get_file_history_tool,
    create_get_workspace_status_tool,
    create_rollback_file_change_tool,
)
from qharness.tools.builtin.replace_text import create_replace_text_tool
from qharness.tools.builtin.write_file import create_write_file_tool
from qharness.tools.providers.base import ToolProvider
from qharness.workspace import WorkspaceMutationService


@dataclass(frozen=True, slots=True)
class FileMutationToolProvider(ToolProvider):
    """把一个 Run 的文件变更服务转换为动态工具集合。"""

    # 服务中已经绑定工作区、Run 标识和私有历史存储。
    service: WorkspaceMutationService

    @property
    def name(self) -> str:
        """返回稳定的工具提供器名称。"""

        return "file_mutation"

    async def load_tools(self) -> list[Tool]:
        """创建当前 Run 可使用的全部文件修改与历史工具。"""

        return [
            create_write_file_tool(self.service),
            create_replace_text_tool(self.service),
            create_apply_patch_tool(self.service),
            create_get_file_history_tool(self.service),
            create_get_workspace_status_tool(self.service),
            create_rollback_file_change_tool(self.service),
        ]
