# -*- coding: utf-8 -*-
"""应用级 SQLAlchemy Engine、连接池和 Session 管理器。"""

from __future__ import annotations

import threading
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from qharness.exception import DatabaseError
from qharness.persistence.config import DatabaseConfig
from qharness.persistence.migration import migrate_database


class DatabaseManager:
    """为一个 QHarness 进程集中持有唯一 Engine 和 SessionFactory。"""

    def __init__(self, config: DatabaseConfig) -> None:
        """按方言创建 Engine；构造过程本身不执行建表迁移。"""

        self.config = config
        self._initialize_lock = threading.Lock()
        self._initialized = False
        self._local_root: Path | None = None
        self._engine = self._create_engine()
        self._session_factory = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
        )

    @property
    def engine(self) -> Engine:
        """返回整个应用共享的 SQLAlchemy Engine。"""

        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        """返回供各功能仓储创建短生命周期 Session 的工厂。"""

        return self._session_factory

    @property
    def local_root(self) -> Path | None:
        """SQLite 返回数据库文件所在目录；远程数据库返回 None。"""

        return self._local_root

    def initialize(self) -> None:
        """幂等初始化数据库策略和结构；同一实例重复调用不会重复迁移。"""

        with self._initialize_lock:
            if self._initialized:
                return
            if self._engine.dialect.name == "sqlite":
                self._initialize_sqlite()
            migrate_database(self._engine)
            self._initialized = True

    def close(self) -> None:
        """释放连接池；通常在 Harness 进程退出时调用一次。"""

        self._engine.dispose()

    def _create_engine(self) -> Engine:
        """创建与数据库方言匹配的 Engine 和基础连接参数。"""

        try:
            url = make_url(self.config.url)
            connect_args: dict[str, object] = {}
            if url.get_backend_name() == "sqlite":
                connect_args = {
                    "check_same_thread": False,
                    "timeout": self.config.sqlite_timeout_seconds,
                }
                if url.database not in {None, "", ":memory:"}:
                    database_path = Path(url.database).resolve(strict=False)
                    database_path.parent.mkdir(parents=True, exist_ok=True)
                    self._local_root = database_path.parent
            engine = create_engine(
                url,
                connect_args=connect_args,
                echo=self.config.echo,
                pool_pre_ping=self.config.pool_pre_ping,
                pool_recycle=self.config.pool_recycle_seconds,
            )
        except (ImportError, ModuleNotFoundError, OSError, SQLAlchemyError) as error:
            raise DatabaseError(
                "无法创建数据库 Engine，请检查 URL、目录和数据库驱动。"
            ) from error

        if url.get_backend_name() == "sqlite":

            @event.listens_for(engine, "connect")
            def _configure_sqlite(connection: object, _: object) -> None:
                """为每条 SQLite 连接启用外键、等待时间和同步策略。"""

                cursor = connection.cursor()  # type: ignore[attr-defined]
                timeout_ms = int(self.config.sqlite_timeout_seconds * 1000)
                cursor.execute("PRAGMA foreign_keys = ON")
                cursor.execute(f"PRAGMA busy_timeout = {timeout_ms}")
                cursor.execute("PRAGMA synchronous = NORMAL")
                cursor.close()

        return engine

    def _initialize_sqlite(self) -> None:
        """在结构迁移前为共享 SQLite 文件启用 WAL 日志模式。"""

        try:
            with self._engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode = WAL")
        except SQLAlchemyError as error:
            raise DatabaseError("无法为 SQLite 启用 WAL 日志模式。") from error
