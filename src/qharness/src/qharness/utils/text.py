# -*- coding: utf-8 -*-
"""通用字符串处理工具。"""

from __future__ import annotations


def strip_to_none(value: object) -> str | None:
    """
    将输入值转换为去除首尾空白的文本，空值和空白文本返回 ``None``。

    Args:
        value: 待处理的值；传入 ``None`` 时直接返回 ``None``。

    Returns:
        去除首尾空白后的非空字符串，或 ``None``。
    """

    if value is None:
        return None

    text = str(value).strip()
    return text or None
