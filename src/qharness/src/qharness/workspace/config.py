# -*- coding: utf-8 -*-
"""Dulwich 私有工作区历史的动态配置。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qharness.exception import WorkspaceHistoryConfigurationError
from qharness.utils.toml import (
    TomlDocumentError,
    load_toml_document,
    read_string,
    read_table,
    reject_unknown_keys,
    resolve_config_path,
)


_WORKSPACE_HISTORY_KEYS = {"storage_root"}


@dataclass(frozen=True, slots=True)
class WorkspaceHistoryConfig:
    """Dulwich 私有仓库的存储配置。"""

    # 每个逻辑工作区会在该目录下使用独立的摘要子目录。
    storage_root: Path


def load_workspace_history_config(
    config_path: str | Path,
) -> WorkspaceHistoryConfig:
    """从 TOML 的 ``[workspace_history]`` 节读取版本存储目录。"""

    try:
        resolved_path, document = load_toml_document(
            config_path,
            "工作区历史配置",
        )
        raw = read_table(
            document,
            "workspace_history",
            "",
            required=True,
        )
        reject_unknown_keys(raw, _WORKSPACE_HISTORY_KEYS, "workspace_history")
        storage_root = read_string(
            raw,
            "storage_root",
            "../.qharness/history",
            "workspace_history",
        )
        return WorkspaceHistoryConfig(
            storage_root=resolve_config_path(resolved_path, storage_root)
        )
    except (TomlDocumentError, TypeError, ValueError) as error:
        raise WorkspaceHistoryConfigurationError(
            f"工作区历史配置不合法：{error}"
        ) from error
