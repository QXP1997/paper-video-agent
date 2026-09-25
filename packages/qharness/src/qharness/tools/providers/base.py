# -*- coding: utf-8 -*-
"""工具提供器接口和统一加载流程。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from qharness.exception import ToolProviderError
from qharness.tools.base import Tool
from qharness.tools.registry import ToolRegistry


class ToolProvider(ABC):
    """从一个具体来源异步加载一组工具。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """返回稳定的工具提供器名称，用于日志和错误信息。"""

    @abstractmethod
    async def load_tools(self) -> Iterable[Tool]:
        """加载当前可用工具；远程提供器可以在这里执行异步发现。"""


async def load_tool_providers(
    registry: ToolRegistry,
    providers: Iterable[ToolProvider],
) -> list[Tool]:
    """依次加载所有提供器，并把有效工具注册到同一个注册表。"""

    loaded_tools: list[Tool] = []
    for provider in providers:
        try:
            tools = list(await provider.load_tools())
        except Exception as error:
            raise ToolProviderError(
                f"工具提供器 {provider.name} 加载失败：{error}"
            ) from error

        for tool in tools:
            if not isinstance(tool, Tool):
                raise ToolProviderError(
                    f"工具提供器 {provider.name} 返回了非 Tool 对象："
                    f"{type(tool).__name__}"
                )
            registry.register(tool)
            loaded_tools.append(tool)
    return loaded_tools
