# -*- coding: utf-8 -*-
"""由 DatabaseManager 以编程方式驱动的 Alembic 环境。"""

from __future__ import annotations

from alembic import context
from sqlalchemy.engine import Connection

from qharness.persistence.base import OrmBase

# 导入当前功能模型，保证生成后续 revision 时 Metadata 是完整的。
# 迁移的实际结构仍由 versions 下的不可变脚本决定，运行时不会 create_all。
import qharness.workspace.history  # noqa: F401, E402


def run_migrations() -> None:
    """在 DatabaseManager 传入的事务连接上执行在线迁移。"""

    connection = context.config.attributes.get("connection")
    if not isinstance(connection, Connection):
        raise RuntimeError("Alembic 缺少 DatabaseManager 提供的数据库连接。")
    context.configure(
        connection=connection,
        target_metadata=OrmBase.metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
