# -*- coding: utf-8 -*-
"""应用级数据库配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from qharness.exception import DatabaseConfigurationError
from qharness.utils.toml import (
    TomlDocumentError,
    load_toml_document,
    read_bool,
    read_float,
    read_int,
    read_required_string,
    read_table,
    reject_unknown_keys,
)


_DATABASE_KEYS = {
    "url",
    "echo",
    "pool_pre_ping",
    "pool_recycle_seconds",
    "sqlite_timeout_seconds",
}


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """整个 QHarness 进程共享的数据库和连接池配置。"""

    # SQLAlchemy URL 可能包含密码，因此对象 repr 不展示该字段。
    url: str = field(repr=False)

    # 是否记录 SQLAlchemy SQL，仅建议本地诊断时开启。
    echo: bool = False

    # 从连接池取出连接时是否先检查连接有效性。
    pool_pre_ping: bool = True

    # 池连接回收秒数，主要防止 MySQL 长期空闲连接失效。
    pool_recycle_seconds: int = 1800

    # SQLite 等待数据库文件锁的最长秒数；其他方言不会使用。
    sqlite_timeout_seconds: float = 30.0


def load_database_config(config_path: str | Path) -> DatabaseConfig:
    """从 TOML 的 ``[database]`` 节读取全局数据库配置。"""

    try:
        resolved_path, document = load_toml_document(
            config_path,
            "数据库配置",
        )
        raw = read_table(document, "database", "", required=True)
        reject_unknown_keys(raw, _DATABASE_KEYS, "database")
        url = _normalize_database_url(
            resolved_path,
            read_required_string(raw, "url", "database"),
        )
        return DatabaseConfig(
            url=url,
            echo=read_bool(raw, "echo", False, "database"),
            pool_pre_ping=read_bool(
                raw,
                "pool_pre_ping",
                True,
                "database",
            ),
            pool_recycle_seconds=read_int(
                raw,
                "pool_recycle_seconds",
                1800,
                "database",
                positive=True,
            ),
            sqlite_timeout_seconds=read_float(
                raw,
                "sqlite_timeout_seconds",
                30.0,
                "database",
                positive=True,
            ),
        )
    except (TomlDocumentError, TypeError, ValueError) as error:
        raise DatabaseConfigurationError(
            f"数据库配置不合法：{error}"
        ) from error


def _normalize_database_url(config_path: Path, value: str) -> str:
    """让 SQLite 相对路径稳定地相对于配置文件目录。"""

    try:
        url = make_url(value)
    except ArgumentError as error:
        raise ValueError(
            f"database.url 不是有效的 SQLAlchemy URL：{error}"
        ) from error
    if url.get_backend_name() != "sqlite" or url.database in {
        None,
        "",
        ":memory:",
    }:
        return url.render_as_string(hide_password=False)

    database_path = Path(url.database).expanduser()
    if not database_path.is_absolute():
        database_path = config_path.parent / database_path
    database_path = database_path.resolve(strict=False)
    return url.set(database=database_path.as_posix()).render_as_string(
        hide_password=False
    )
