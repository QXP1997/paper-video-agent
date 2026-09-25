# -*- coding: utf-8 -*-
"""TOML 配置表的通用严格读取工具。

本模块只负责类型、必填项和未知字段校验，不包含模型、工具或沙箱的领域
默认值。调用方仍应在各自的 ``config.py`` 中声明允许字段和业务约束。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


class TomlDocumentError(ValueError):
    """表示 TOML 文件不存在、不可读取或语法不合法。"""


def load_toml_document(
    config_path: str | Path,
    description: str,
) -> tuple[Path, dict[str, Any]]:
    """读取完整 TOML 文档，并返回规范化路径和根字典。

    ``description`` 用于生成面向用户的错误，例如“模型配置”或“沙箱配置”。
    领域加载器捕获 ``TomlDocumentError`` 后，应转换为自己的业务异常。
    """

    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise TomlDocumentError(f"{description}文件不存在：{path}")
    try:
        with path.open("rb") as file:
            return path, tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise TomlDocumentError(
            f"{description}文件格式错误：{error}"
        ) from error
    except OSError as error:
        raise TomlDocumentError(
            f"{description}文件无法读取：{error}"
        ) from error


def read_table(
    data: dict[str, Any],
    name: str,
    section: str,
    *,
    required: bool = True,
) -> dict[str, Any]:
    """读取 TOML 子表；可选表不存在时返回新的空字典。"""

    if name not in data:
        if required:
            raise ValueError(f"{_field_name(section, name)} 必须是 TOML 表。")
        return {}
    value = data[name]
    if not isinstance(value, dict):
        raise ValueError(f"{_field_name(section, name)} 必须是 TOML 表。")
    return value


def reject_unknown_keys(
    data: dict[str, Any],
    allowed_keys: set[str] | frozenset[str],
    section: str,
) -> None:
    """拒绝拼写错误或当前版本不支持的配置项。"""

    unknown_keys = sorted(set(data) - allowed_keys)
    if unknown_keys:
        raise ValueError(
            f"{section} 包含未知配置项：{'、'.join(unknown_keys)}"
        )


def read_string(
    data: dict[str, Any],
    name: str,
    default: str,
    section: str,
    *,
    allow_empty: bool = False,
) -> str:
    """读取字符串；默认去除首尾空白，并拒绝空字符。"""

    value = data.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"{_field_name(section, name)} 必须是字符串。")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ValueError(f"{_field_name(section, name)} 必须是非空字符串。")
    if "\x00" in normalized:
        raise ValueError(f"{_field_name(section, name)} 不能包含空字符。")
    return normalized


def read_required_string(
    data: dict[str, Any],
    name: str,
    section: str,
    *,
    allow_empty: bool = False,
) -> str:
    """读取没有默认值的必需字符串。"""

    if name not in data:
        raise ValueError(f"{section} 缺少 {name}。")
    return read_string(
        data,
        name,
        "",
        section,
        allow_empty=allow_empty,
    )


def read_optional_string(
    data: dict[str, Any],
    name: str,
    section: str,
    *,
    allow_empty: bool = False,
) -> str | None:
    """读取可选字符串；配置项不存在时返回 ``None``。"""

    if name not in data:
        return None
    return read_string(
        data,
        name,
        "",
        section,
        allow_empty=allow_empty,
    )


def read_int(
    data: dict[str, Any],
    name: str,
    default: int,
    section: str,
    *,
    positive: bool = False,
) -> int:
    """严格读取整数，避免 TOML 布尔值被当作整数。"""

    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{_field_name(section, name)} 必须是整数。")
    if positive and value <= 0:
        raise ValueError(f"{_field_name(section, name)} 必须大于 0。")
    return value


def read_optional_int(
    data: dict[str, Any],
    name: str,
    section: str,
    *,
    positive: bool = False,
) -> int | None:
    """读取可选整数；配置项不存在时返回 ``None``。"""

    if name not in data:
        return None
    return read_int(data, name, 0, section, positive=positive)


def read_float(
    data: dict[str, Any],
    name: str,
    default: float,
    section: str,
    *,
    positive: bool = False,
) -> float:
    """读取整数或浮点数，并统一转换为浮点数。"""

    value = data.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{_field_name(section, name)} 必须是数字。")
    if positive and value <= 0:
        raise ValueError(f"{_field_name(section, name)} 必须大于 0。")
    return float(value)


def read_optional_float(
    data: dict[str, Any],
    name: str,
    section: str,
    *,
    positive: bool = False,
) -> float | None:
    """读取可选数字；配置项不存在时返回 ``None``。"""

    if name not in data:
        return None
    return read_float(data, name, 0.0, section, positive=positive)


def read_bool(
    data: dict[str, Any],
    name: str,
    default: bool,
    section: str,
) -> bool:
    """严格读取布尔值，不接受字符串或数字替代。"""

    value = data.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{_field_name(section, name)} 必须是布尔值。")
    return value


def read_optional_bool(
    data: dict[str, Any],
    name: str,
    section: str,
) -> bool | None:
    """读取可选布尔值；配置项不存在时返回 ``None``。"""

    if name not in data:
        return None
    return read_bool(data, name, False, section)


def read_string_list(
    data: dict[str, Any],
    name: str,
    default: tuple[str, ...],
    section: str,
) -> tuple[str, ...]:
    """读取不含空白项和空字符的字符串数组。"""

    value = data.get(name, list(default))
    if not isinstance(value, list):
        raise ValueError(f"{_field_name(section, name)} 必须是字符串数组。")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise ValueError(
                f"{_field_name(section, name)} 只能包含非空字符串。"
            )
        result.append(item.strip())
    return tuple(result)


def resolve_config_path(config_path: Path, value: str) -> Path:
    """将相对路径固定解析为相对于配置文件所在目录。"""

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = config_path.parent / candidate
    return candidate.resolve(strict=False)


def _field_name(section: str, name: str) -> str:
    """拼接适合错误提示的完整 TOML 字段名称。"""

    return f"{section}.{name}" if section else name
