# -*- coding: utf-8 -*-
"""工作区持久化实现共用的安全标识工具。"""

from __future__ import annotations

import hashlib

from qharness.exception import WorkspaceHistoryError


def validate_history_identifier(value: str, name: str) -> str:
    """验证仅用于数据关联的租户、工作区和 Run 标识。"""

    if not isinstance(value, str) or not value.strip():
        raise WorkspaceHistoryError(f"{name} 必须是非空字符串。")
    if "\x00" in value or len(value) > 256:
        raise WorkspaceHistoryError(f"{name} 包含空字符或超过 256 个字符。")
    return value


def workspace_storage_namespace(tenant_id: str, workspace_id: str) -> str:
    """生成固定长度目录名，避免把外部标识直接拼接到文件系统路径。"""

    normalized_tenant_id = validate_history_identifier(tenant_id, "tenant_id")
    normalized_workspace_id = validate_history_identifier(
        workspace_id,
        "workspace_id",
    )
    source = f"{normalized_tenant_id}\0{normalized_workspace_id}".encode("utf-8")
    return hashlib.sha256(source).hexdigest()

