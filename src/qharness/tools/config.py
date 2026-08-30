# -*- coding: utf-8 -*-
"""工具执行策略的 TOML 配置读取模块。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from qharness.exception.error import ToolConfigurationError
from qharness.tools.base import (
    ToolExecutionPolicy,
    ToolPolicyOverride,
    ToolRuntimePolicy,
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

    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise ToolConfigurationError(f"工具配置文件不存在：{path}")

    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise ToolConfigurationError(f"工具配置文件格式错误：{error}") from error

    policy_data = document.get("tool_execution")
    if not isinstance(policy_data, dict):
        raise ToolConfigurationError("配置文件缺少 [tool_execution] 节。")

    try:
        _reject_unknown_keys(policy_data, _GLOBAL_KEYS, "tool_execution")
        defaults_data = _read_table(policy_data, "defaults")
        tools_data = _read_table(policy_data, "tools")
        defaults = _read_default_policy(defaults_data)
        tool_overrides = {
            tool_name: _read_tool_override(tool_name, override_data)
            for tool_name, override_data in tools_data.items()
        }
        return ToolExecutionPolicy(
            max_total_calls=_read_optional_int(
                policy_data,
                "max_total_calls",
                "tool_execution",
            ),
            max_concurrency=_read_optional_int(
                policy_data,
                "max_concurrency",
                "tool_execution",
            ),
            defaults=defaults,
            tool_overrides=tool_overrides,
        )
    except (TypeError, ValueError) as error:
        raise ToolConfigurationError(f"工具执行策略配置不合法：{error}") from error


def _read_default_policy(data: dict[str, Any]) -> ToolRuntimePolicy:
    """读取所有未单独配置工具共同使用的默认策略。"""

    section = "tool_execution.defaults"
    _reject_unknown_keys(data, _TOOL_POLICY_KEYS, section)
    return ToolRuntimePolicy(
        max_calls=_read_int(data, "max_calls", 10, section),
        timeout_seconds=_read_float(data, "timeout_seconds", 30.0, section),
        max_concurrency=_read_optional_int(data, "max_concurrency", section),
        max_argument_chars=_read_int(
            data,
            "max_argument_chars",
            64_000,
            section,
        ),
        max_result_chars=_read_int(
            data,
            "max_result_chars",
            200_000,
            section,
        ),
        truncate_oversized_results=_read_bool(
            data,
            "truncate_oversized_results",
            True,
            section,
        ),
        requires_approval=_read_bool(
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
    _reject_unknown_keys(raw_data, _TOOL_POLICY_KEYS, section)
    return ToolPolicyOverride(
        max_calls=_read_optional_int(raw_data, "max_calls", section),
        timeout_seconds=_read_optional_float(
            raw_data,
            "timeout_seconds",
            section,
        ),
        max_concurrency=_read_optional_int(
            raw_data,
            "max_concurrency",
            section,
        ),
        max_argument_chars=_read_optional_int(
            raw_data,
            "max_argument_chars",
            section,
        ),
        max_result_chars=_read_optional_int(
            raw_data,
            "max_result_chars",
            section,
        ),
        truncate_oversized_results=_read_optional_bool(
            raw_data,
            "truncate_oversized_results",
            section,
        ),
        requires_approval=_read_optional_bool(
            raw_data,
            "requires_approval",
            section,
        ),
    )


def _read_table(data: dict[str, Any], name: str) -> dict[str, Any]:
    """读取可选 TOML 子表；缺失时返回空表。"""

    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"tool_execution.{name} 必须是 TOML 表。")
    return value


def _reject_unknown_keys(
    data: dict[str, Any],
    allowed_keys: set[str],
    section: str,
) -> None:
    """拒绝未知键，避免配置拼写错误被静默忽略。"""

    unknown_keys = sorted(set(data) - allowed_keys)
    if unknown_keys:
        names = "、".join(unknown_keys)
        raise ValueError(f"{section} 包含未知配置项：{names}")


def _read_int(
    data: dict[str, Any],
    name: str,
    default: int,
    section: str,
) -> int:
    """读取整数配置，并拒绝 TOML 布尔值被当作整数使用。"""

    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{section}.{name} 必须是整数。")
    return value


def _read_optional_int(
    data: dict[str, Any],
    name: str,
    section: str,
) -> int | None:
    """读取可选整数；配置项不存在时表示继承默认值。"""

    if name not in data:
        return None
    return _read_int(data, name, 0, section)


def _read_float(
    data: dict[str, Any],
    name: str,
    default: float,
    section: str,
) -> float:
    """读取整数或浮点数配置，并统一转换为浮点数。"""

    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{section}.{name} 必须是数字。")
    return float(value)


def _read_optional_float(
    data: dict[str, Any],
    name: str,
    section: str,
) -> float | None:
    """读取可选浮点数；配置项不存在时表示继承默认值。"""

    if name not in data:
        return None
    return _read_float(data, name, 0.0, section)


def _read_bool(
    data: dict[str, Any],
    name: str,
    default: bool,
    section: str,
) -> bool:
    """读取布尔配置并进行严格类型校验。"""

    value = data.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{section}.{name} 必须是布尔值。")
    return value


def _read_optional_bool(
    data: dict[str, Any],
    name: str,
    section: str,
) -> bool | None:
    """读取可选布尔值；配置项不存在时表示继承默认值。"""

    if name not in data:
        return None
    return _read_bool(data, name, False, section)
