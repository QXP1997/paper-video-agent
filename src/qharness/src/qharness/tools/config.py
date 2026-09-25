# -*- coding: utf-8 -*-
"""工具执行策略的 TOML 配置读取模块。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qharness.exception.error import ToolConfigurationError
from qharness.tools.base import (
    ToolExecutionPolicy,
    ToolPolicyOverride,
    ToolRuntimePolicy,
)
from qharness.utils.toml import (
    TomlDocumentError,
    load_toml_document,
    read_bool,
    read_float,
    read_int,
    read_optional_bool,
    read_optional_float,
    read_optional_int,
    read_table,
    reject_unknown_keys,
)


_GLOBAL_KEYS = {"max_total_calls", "max_concurrency", "defaults", "tools"}
_TOOL_POLICY_KEYS = {
    "max_calls",
    "timeout_seconds",
    "max_concurrency",
    "max_argument_chars",
    "max_result_chars",
    "truncate_oversized_results",
    "requires_approval",
}


def load_tool_policy(config_path: str | Path) -> ToolExecutionPolicy:
    """从 TOML 文件动态读取工具执行策略，不使用环境变量覆盖。"""

    try:
        _, document = load_toml_document(config_path, "工具配置")
    except TomlDocumentError as error:
        raise ToolConfigurationError(str(error)) from error

    policy_data = document.get("tool_execution")
    if not isinstance(policy_data, dict):
        raise ToolConfigurationError("配置文件缺少 [tool_execution] 节。")

    try:
        reject_unknown_keys(policy_data, _GLOBAL_KEYS, "tool_execution")
        defaults_data = read_table(
            policy_data,
            "defaults",
            "tool_execution",
            required=False,
        )
        tools_data = read_table(
            policy_data,
            "tools",
            "tool_execution",
            required=False,
        )
        defaults = _read_default_policy(defaults_data)
        tool_overrides = {
            tool_name: _read_tool_override(tool_name, override_data)
            for tool_name, override_data in tools_data.items()
        }
        return ToolExecutionPolicy(
            max_total_calls=read_optional_int(
                policy_data,
                "max_total_calls",
                "tool_execution",
                positive=True,
            ),
            max_concurrency=read_optional_int(
                policy_data,
                "max_concurrency",
                "tool_execution",
                positive=True,
            ),
            defaults=defaults,
            tool_overrides=tool_overrides,
        )
    except (TypeError, ValueError) as error:
        raise ToolConfigurationError(f"工具执行策略配置不合法：{error}") from error


def _read_default_policy(data: dict[str, Any]) -> ToolRuntimePolicy:
    """读取所有未单独配置工具共同使用的默认策略。"""

    section = "tool_execution.defaults"
    reject_unknown_keys(data, _TOOL_POLICY_KEYS, section)
    return ToolRuntimePolicy(
        max_calls=read_int(data, "max_calls", 10, section, positive=True),
        timeout_seconds=read_float(
            data,
            "timeout_seconds",
            30.0,
            section,
            positive=True,
        ),
        max_concurrency=read_optional_int(
            data,
            "max_concurrency",
            section,
            positive=True,
        ),
        max_argument_chars=read_int(
            data,
            "max_argument_chars",
            64_000,
            section,
            positive=True,
        ),
        max_result_chars=read_int(
            data,
            "max_result_chars",
            200_000,
            section,
            positive=True,
        ),
        truncate_oversized_results=read_bool(
            data,
            "truncate_oversized_results",
            True,
            section,
        ),
        requires_approval=read_bool(
            data,
            "requires_approval",
            False,
            section,
        ),
    )


def _read_tool_override(
    tool_name: str,
    raw_data: Any,
) -> ToolPolicyOverride:
    """读取某个具名工具的可选覆盖项。"""

    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("tool_execution.tools 中的工具名称不能为空。")
    if not isinstance(raw_data, dict):
        raise ValueError(f"tool_execution.tools.{tool_name} 必须是 TOML 表。")

    section = f"tool_execution.tools.{tool_name}"
    reject_unknown_keys(raw_data, _TOOL_POLICY_KEYS, section)
    return ToolPolicyOverride(
        max_calls=read_optional_int(
            raw_data,
            "max_calls",
            section,
            positive=True,
        ),
        timeout_seconds=read_optional_float(
            raw_data,
            "timeout_seconds",
            section,
            positive=True,
        ),
        max_concurrency=read_optional_int(
            raw_data,
            "max_concurrency",
            section,
            positive=True,
        ),
        max_argument_chars=read_optional_int(
            raw_data,
            "max_argument_chars",
            section,
            positive=True,
        ),
        max_result_chars=read_optional_int(
            raw_data,
            "max_result_chars",
            section,
            positive=True,
        ),
        truncate_oversized_results=read_optional_bool(
            raw_data,
            "truncate_oversized_results",
            section,
        ),
        requires_approval=read_optional_bool(
            raw_data,
            "requires_approval",
            section,
        ),
    )
