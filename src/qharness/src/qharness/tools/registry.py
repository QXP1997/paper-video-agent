# -*- coding: utf-8 -*-
"""本地工具注册表。"""

from __future__ import annotations

import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from qharness.exception.error import ToolRegistrationError
from qharness.model.models import ToolDefinition
from qharness.tools.base import Tool, ToolEffect


_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ToolRegistry:
    """保存工具定义，并保证工具名称与参数 Schema 合法。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册工具；名称重复或定义不合法时直接报错。"""

        if not isinstance(tool.effect, ToolEffect) or not isinstance(tool.parallel_safe, bool):
            raise ToolRegistrationError("工具 effect / parallel_safe 声明类型不合法")
        if tool.parallel_safe and tool.effect != ToolEffect.READ_ONLY:
            raise ToolRegistrationError("只有只读工具可以声明 parallel_safe")

        if not _TOOL_NAME_PATTERN.fullmatch(tool.name):
            raise ToolRegistrationError(
                "工具名称必须由 1 至 64 个字母、数字、下划线或连字符组成。"
            )
        if not tool.description.strip():
            raise ToolRegistrationError(f"工具 {tool.name} 的描述不能为空。")
        if not callable(tool.handler):
            raise ToolRegistrationError(f"工具 {tool.name} 的 handler 不可调用。")
        try:
            parameter_schema = tool.parameter_json_schema()
        except Exception as error:
            raise ToolRegistrationError(
                f"工具 {tool.name} 的参数声明无法转换为 JSON Schema：{error}"
            ) from error
        try:
            Draft202012Validator.check_schema(parameter_schema)
        except SchemaError as error:
            raise ToolRegistrationError(
                f"工具 {tool.name} 的参数 JSON Schema 不合法：{error.message}"
            ) from error
        if parameter_schema.get("type") != "object":
            raise ToolRegistrationError(
                f"工具 {tool.name} 的参数 Schema 顶层类型必须是 object。"
            )
        if tool.name in self._tools:
            raise ToolRegistrationError(f"工具名称已注册：{tool.name}")

        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """按名称查询工具；不存在时返回 ``None``。"""

        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        """按注册顺序返回可发送给模型的全部工具定义。"""

        return [tool.to_definition() for tool in self._tools.values()]

    def __len__(self) -> int:
        """返回当前已经注册的工具数量。"""

        return len(self._tools)
