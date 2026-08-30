# -*- coding: utf-8 -*-
"""工作区内置只读工具提供器。"""

from __future__ import annotations

from dataclasses import dataclass

from qharness.tools.base import Tool
from qharness.tools.builtin import (
    create_list_directory_tool,
    create_read_file_tool,
    create_search_text_tool,
)
from qharness.tools.executables import resolve_ripgrep_path
from qharness.tools.providers.base import ToolProvider
from qharness.workspace import WorkspaceContext


@dataclass(frozen=True, slots=True, init=False)
class BuiltinToolProvider(ToolProvider):
    """根据当前工作区动态创建可用的内置只读工具。"""

    workspace: WorkspaceContext
    ripgrep_path: str | None
    list_directory_cursor_ttl_seconds: float
    list_directory_max_cursors: int

    def __init__(
        self,
        workspace: WorkspaceContext,
        *,
        ripgrep_path: str | None = None,
        list_directory_cursor_ttl_seconds: float = 30 * 60,
        list_directory_max_cursors: int = 1024,
    ) -> None:
        """保存工作区、目录游标策略，并探测 ripgrep 可执行文件。"""

        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(
            self,
            "ripgrep_path",
            resolve_ripgrep_path(ripgrep_path),
        )
        object.__setattr__(
            self,
            "list_directory_cursor_ttl_seconds",
            list_directory_cursor_ttl_seconds,
        )
        object.__setattr__(
            self,
            "list_directory_max_cursors",
            list_directory_max_cursors,
        )

    @property
    def name(self) -> str:
        """返回内置工具提供器名称。"""

        return "builtin"

    async def load_tools(self) -> list[Tool]:
        """创建当前环境实际可用的内置工具。"""

        tools = [
            create_list_directory_tool(
                self.workspace,
                cursor_ttl_seconds=self.list_directory_cursor_ttl_seconds,
                max_cursors=self.list_directory_max_cursors,
            ),
            create_read_file_tool(self.workspace),
        ]
        if self.ripgrep_path is not None:
            tools.append(
                create_search_text_tool(
                    self.workspace,
                    self.ripgrep_path,
                )
            )
        return tools
