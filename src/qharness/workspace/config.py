# -*- coding: utf-8 -*-
"""工作区版本历史和数据库的动态配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from qharness.exception import WorkspaceHistoryConfigurationError
from qharness.utils.toml import (
    TomlDocumentError,
    load_toml_document,
    read_bool,
    read_float,
    read_int,
    read_required_string,
    read_string,
    read_table,
    reject_unknown_keys,
    resolve_config_path,
)


_HISTORY_KEYS = {"storage_root", "database"}
_DATABASE_KEYS = {
    "url",
    "echo",
    "pool_pre_ping",
    "pool_recycle_seconds",
    "sqlite_timeout_seconds",
}


@dataclass(frozen=True, slots=True)
class WorkspaceHistoryConfig:
    """操作数据库与 Dulwich 私有仓库的运行配置。"""

    # SQLAlchemy URL 自带方言信息；更换数据库只需要修改这个字段。
    database_url: str = field(repr=False)

    # Dulwich 对象仓库的宿主目录；每个逻辑工作区仍使用独立摘要子目录。
    storage_root: Path

    # 是否打印 SQLAlchemy SQL，仅建议本地排查时开启。
    echo: bool = False

    # 从连接池取出连接时是否先验证连接可用性。
    pool_pre_ping: bool = True

    # 池连接回收秒数，避免 MySQL 服务端先行关闭长期空闲连接。
    pool_recycle_seconds: int = 1800

    # SQLite 获取文件锁时等待的最长秒数；其他数据库不会使用。
    sqlite_timeout_seconds: float = 30.0


def load_workspace_history_config(
    config_path: str | Path,
) -> WorkspaceHistoryConfig:
    """从 TOML 加载历史配置，并规范化相对 SQLite 与存储路径。"""

    try:
        resolved_config_path, document = load_toml_document(
            config_path,
            "工作区历史配置",
        )
        history_data = read_table(document, "history", "", required=True)
        reject_unknown_keys(history_data, _HISTORY_KEYS, "history")
        database_data = read_table(
            history_data,
            "database",
            "history",
            required=True,
        )
        reject_unknown_keys(database_data, _DATABASE_KEYS, "history.database")

        storage_root_text = read_string(
            history_data,
            "storage_root",
            "../.qharness/history",
            "history",
        )
        database_url = _normalize_database_url(
            resolved_config_path,
            read_required_string(database_data, "url", "history.database"),
        )
        return WorkspaceHistoryConfig(
            database_url=database_url,
            storage_root=resolve_config_path(
                resolved_config_path,
                storage_root_text,
            ),
            echo=read_bool(database_data, "echo", False, "history.database"),
            pool_pre_ping=read_bool(
                database_data,
                "pool_pre_ping",
                True,
                "history.database",
            ),
            pool_recycle_seconds=read_int(
                database_data,
                "pool_recycle_seconds",
                1800,
                "history.database",
                positive=True,
            ),
            sqlite_timeout_seconds=read_float(
                database_data,
                "sqlite_timeout_seconds",
                30.0,
                "history.database",
                positive=True,
            ),
        )
    except (TomlDocumentError, TypeError, ValueError) as error:
        raise WorkspaceHistoryConfigurationError(
            f"工作区历史配置不合法：{error}"
        ) from error


def _normalize_database_url(config_path: Path, value: str) -> str:
    """让 SQLite 相对数据库路径相对于配置文件，而不是进程工作目录。"""

    try:
        url = make_url(value)
    except ArgumentError as error:
        raise ValueError(
            f"history.database.url 不是有效的 SQLAlchemy URL：{error}"
        ) from error
    if url.get_backend_name() != "sqlite" or url.database in {None, "", ":memory:"}:
        return url.render_as_string(hide_password=False)

    database_path = Path(url.database).expanduser()
    if not database_path.is_absolute():
        database_path = config_path.parent / database_path
    database_path = database_path.resolve(strict=False)
    return url.set(database=database_path.as_posix()).render_as_string(
        hide_password=False
    )
