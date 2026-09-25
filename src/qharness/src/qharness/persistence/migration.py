# -*- coding: utf-8 -*-
"""使用 Alembic 集中升级应用数据库结构。"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from qharness.exception import DatabaseMigrationError


def migrate_database(engine: Engine) -> None:
    """把数据库幂等升级到随当前程序发布的最新 Alembic revision。"""

    try:
        with engine.begin() as connection:
            configuration = Config()
            script_location = Path(__file__).with_name("alembic")
            configuration.set_main_option(
                "script_location",
                str(script_location),
            )
            # 复用 DatabaseManager 的现有连接，不让 Alembic 自己读取 URL、
            # 创建第二个 Engine 或在日志中意外展示数据库密码。
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, "head")
    except (CommandError, OSError, RuntimeError, SQLAlchemyError) as error:
        raise DatabaseMigrationError("数据库结构迁移失败。") from error
