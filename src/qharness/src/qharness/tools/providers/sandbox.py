# -*- coding: utf-8 -*-
"""当前 Agent Run 的沙箱工具提供器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from qharness.tools.base import Tool
from qharness.tools.builtin.run_command import create_run_command_tool
from qharness.tools.providers.base import ToolProvider

if TYPE_CHECKING:
    from qharness.run import RunContext


@dataclass(frozen=True, slots=True)
class SandboxToolProvider(ToolProvider):
    """为一个 RunContext 动态创建外部进程执行工具。"""

    # 当前租户本次 Run 的完整隔离上下文。
    context: RunContext

    @property
    def name(self) -> str:
        """返回稳定的工具提供器名称。"""

        return "sandbox"

    async def load_tools(self) -> list[Tool]:
        """返回绑定到当前 Run 工作区和沙箱的工具。"""

        return [create_run_command_tool(self.context)]
