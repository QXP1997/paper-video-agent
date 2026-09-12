# -*- coding: utf-8 -*-
"""工作区操作台账接口及 SQLAlchemy 持久化实现。"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, Self, runtime_checkable
from weakref import WeakSet

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from qharness.exception import WorkspaceHistoryError
from qharness.workspace.config import WorkspaceHistoryConfig
from qharness.workspace.identity import (
    validate_history_identifier,
)
from qharness.workspace.models import ChangeStatus


_LOCKS_GUARD = threading.Lock()
_WORKSPACE_LOCKS: dict[str, threading.RLock] = {}

# 同一个 Harness 进程中的所有 Run 复用数据库 Engine 和连接池。键值使用配置
# 摘要，既能区分不同数据库，也不会把数据库密码直接保存在缓存键中。
_ENGINES_GUARD = threading.Lock()
_ENGINES: dict[str, tuple[Engine, Path | None]] = {}

# 一个 Engine 只初始化一次 ORM 表结构，避免多个 Run 并发执行 create_all。
_SCHEMA_GUARD = threading.Lock()
_INITIALIZED_ENGINES: WeakSet[Engine] = WeakSet()


@dataclass(frozen=True, slots=True)
class StoredOperation:
    """从数据库读取的一次工作区操作台账。"""

    # 写工具返回并供查询、回滚使用的唯一操作编号。
    operation_id: str

    # 创建记录的工具名称，例如 write_file 或 apply_patch。
    tool_name: str

    # 操作来源；常见值为 agent、external 和 rollback。
    origin: str

    # 操作当前是待确认、已应用、失败还是已回滚。
    status: ChangeStatus

    # 本次操作涉及的相对路径，只作为检索索引而非版本事实来源。
    paths: tuple[str, ...]

    # 操作基于的私有 Git Commit；初始基线之前可以为空。
    base_commit_id: str | None

    # 操作成功后产生的私有 Git Commit。
    commit_id: str | None

    # 如果原操作已回滚，这里保存对应回滚操作编号。
    reverted_by_operation_id: str | None

    # 操作失败或恢复诊断使用的简短信息。
    error_message: str | None

    # 数据库记录的 UTC 创建时间。
    created_at: datetime


@runtime_checkable
class WorkspaceHistoryRepository(Protocol):
    """工作区操作台账的数据库无关接口。"""

    @property
    def local_root(self) -> Path | None:
        """返回本地数据库宿主目录；远程数据库实现返回 None。"""
        ...

    def bind_workspace_root(self, workspace_root: str | Path) -> None:
        """把逻辑工作区绑定到经过授权的规范化根目录。"""
        ...

    def begin_operation(
        self,
        *,
        run_id: str,
        tool_name: str,
        origin: str,
        paths: tuple[str, ...],
        base_commit_id: str | None,
    ) -> str:
        """创建 pending 操作并返回操作编号。"""
        ...

    def complete_operation(self, operation_id: str, commit_id: str) -> None:
        """把 pending 操作更新为 applied，并绑定最终 Commit。"""
        ...

    def fail_operation(self, operation_id: str, message: str) -> None:
        """把失败的 pending 操作标记为 failed。"""
        ...

    def complete_rollback(
        self,
        rollback_operation_id: str,
        original_operation_id: str,
        commit_id: str,
    ) -> None:
        """在一个事务中确认回滚，并更新原操作状态。"""
        ...

    def get_operation(self, operation_id: str) -> StoredOperation:
        """读取一次操作台账。"""
        ...


class _Base(DeclarativeBase):
    """QHarness 工作区历史 ORM 模型基类。"""


class _WorkspaceBindingRecord(_Base):
    """租户工作区标识与真实目录之间的稳定绑定。"""

    __tablename__ = "workspace_bindings"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    root_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_root: Mapped[str] = mapped_column(Text, nullable=False)


class _WorkspaceOperationRecord(_Base):
    """一次文件工具、外部检查点或回滚操作。"""

    __tablename__ = "workspace_operations"

    operation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    base_commit_id: Mapped[str | None] = mapped_column(String(40))
    commit_id: Mapped[str | None] = mapped_column(String(40))
    reverted_by_operation_id: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    files: Mapped[list["_OperationFileRecord"]] = relationship(
        back_populates="operation",
        cascade="all, delete-orphan",
        order_by="_OperationFileRecord.sequence",
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "workspace_id",
            "commit_id",
            name="uq_workspace_operation_commit",
        ),
        Index(
            "idx_workspace_operations_run",
            "tenant_id",
            "workspace_id",
            "run_id",
            "created_at",
        ),
        Index(
            "idx_workspace_operations_commit",
            "tenant_id",
            "workspace_id",
            "commit_id",
        ),
    )


class _OperationFileRecord(_Base):
    """操作涉及文件的轻量检索索引。"""

    __tablename__ = "workspace_operation_files"

    operation_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("workspace_operations.operation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)

    operation: Mapped[_WorkspaceOperationRecord] = relationship(
        back_populates="files"
    )


class SqlAlchemyWorkspaceHistoryRepository:
    """使用 SQLAlchemy 保存工作区操作台账。

    本实现既可以连接 SQLite，也可以由服务端传入 PostgreSQL/MySQL Engine；
    业务服务不接触连接、Session、连接池或 SQL 方言。
    """

    def __init__(
        self,
        engine: Engine,
        *,
        tenant_id: str,
        workspace_id: str,
        local_root: Path | None = None,
    ) -> None:
        """绑定数据库 Engine 与逻辑工作区。"""

        self.tenant_id = validate_history_identifier(tenant_id, "tenant_id")
        self.workspace_id = validate_history_identifier(
            workspace_id,
            "workspace_id",
        )
        self._engine = engine
        self._local_root = local_root
        self._session_factory = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
        )
        lock_source = (
            f"{self._engine.url.render_as_string(hide_password=True)}|"
            f"{self.tenant_id}|{self.workspace_id}"
        )
        lock_key = hashlib.sha256(lock_source.encode("utf-8")).hexdigest()
        self._lock = _workspace_lock(lock_key)
        with _SCHEMA_GUARD:
            if self._engine not in _INITIALIZED_ENGINES:
                try:
                    _Base.metadata.create_all(self._engine)
                except SQLAlchemyError as error:
                    raise WorkspaceHistoryError(
                        "无法初始化工作区历史数据库。"
                    ) from error
                _INITIALIZED_ENGINES.add(self._engine)
        if self._engine.dialect.name == "sqlite":
            self._migrate_legacy_sqlite_tables()

    @property
    def local_root(self) -> Path | None:
        """返回本地数据库宿主目录。"""

        return self._local_root

    def bind_workspace_root(self, workspace_root: str | Path) -> None:
        """固定逻辑工作区对应的真实目录，防止历史串库。"""

        normalized_root = str(Path(workspace_root).resolve(strict=True))
        comparable_root = os.path.normcase(normalized_root)
        root_digest = hashlib.sha256(comparable_root.encode("utf-8")).hexdigest()
        with self._lock, self._session() as session:
            binding = session.get(
                _WorkspaceBindingRecord,
                (self.tenant_id, self.workspace_id),
            )
            if binding is None:
                session.add(
                    _WorkspaceBindingRecord(
                        tenant_id=self.tenant_id,
                        workspace_id=self.workspace_id,
                        root_digest=root_digest,
                        workspace_root=normalized_root,
                    )
                )
                session.commit()
                return
            if binding.root_digest != root_digest:
                raise WorkspaceHistoryError(
                    "相同 tenant_id 与 workspace_id 已绑定到另一个工作区根目录；"
                    "拒绝复用其文件历史。"
                )

    def begin_operation(
        self,
        *,
        run_id: str,
        tool_name: str,
        origin: str,
        paths: tuple[str, ...],
        base_commit_id: str | None,
    ) -> str:
        """在修改前创建 pending 台账及文件路径索引。"""

        operation_id = uuid.uuid4().hex
        normalized_paths = tuple(dict.fromkeys(paths))
        with self._lock, self._session() as session:
            record = _WorkspaceOperationRecord(
                operation_id=operation_id,
                tenant_id=self.tenant_id,
                workspace_id=self.workspace_id,
                run_id=validate_history_identifier(run_id, "run_id"),
                tool_name=validate_history_identifier(tool_name, "tool_name"),
                origin=validate_history_identifier(origin, "origin"),
                status=ChangeStatus.PENDING.value,
                base_commit_id=base_commit_id,
                files=[
                    _OperationFileRecord(sequence=index, path=path)
                    for index, path in enumerate(normalized_paths)
                ],
            )
            session.add(record)
            session.commit()
        return operation_id

    def complete_operation(self, operation_id: str, commit_id: str) -> None:
        """条件确认操作，并绑定 Dulwich Commit。"""

        with self._lock, self._session() as session:
            result = session.execute(
                update(_WorkspaceOperationRecord)
                .where(
                    _WorkspaceOperationRecord.operation_id == operation_id,
                    _WorkspaceOperationRecord.tenant_id == self.tenant_id,
                    _WorkspaceOperationRecord.workspace_id == self.workspace_id,
                    _WorkspaceOperationRecord.status == ChangeStatus.PENDING.value,
                )
                .values(status=ChangeStatus.APPLIED.value, commit_id=commit_id)
            )
            if result.rowcount != 1:
                raise WorkspaceHistoryError(
                    f"历史操作状态更新冲突：{operation_id}"
                )
            session.commit()

    def fail_operation(self, operation_id: str, message: str) -> None:
        """把未完成操作标为失败并保存简短诊断。"""

        with self._lock, self._session() as session:
            result = session.execute(
                update(_WorkspaceOperationRecord)
                .where(
                    _WorkspaceOperationRecord.operation_id == operation_id,
                    _WorkspaceOperationRecord.tenant_id == self.tenant_id,
                    _WorkspaceOperationRecord.workspace_id == self.workspace_id,
                    _WorkspaceOperationRecord.status == ChangeStatus.PENDING.value,
                )
                .values(
                    status=ChangeStatus.FAILED.value,
                    error_message=message[:2000],
                )
            )
            if result.rowcount != 1:
                raise WorkspaceHistoryError(
                    f"无法把历史操作标记为失败：{operation_id}"
                )
            session.commit()

    def complete_rollback(
        self,
        rollback_operation_id: str,
        original_operation_id: str,
        commit_id: str,
    ) -> None:
        """原子确认回滚台账，并把原操作标为 rolled_back。"""

        with self._lock, self._session() as session:
            rollback_result = session.execute(
                update(_WorkspaceOperationRecord)
                .where(
                    _WorkspaceOperationRecord.operation_id
                    == rollback_operation_id,
                    _WorkspaceOperationRecord.tenant_id == self.tenant_id,
                    _WorkspaceOperationRecord.workspace_id == self.workspace_id,
                    _WorkspaceOperationRecord.status == ChangeStatus.PENDING.value,
                )
                .values(status=ChangeStatus.APPLIED.value, commit_id=commit_id)
            )
            original_result = session.execute(
                update(_WorkspaceOperationRecord)
                .where(
                    _WorkspaceOperationRecord.operation_id
                    == original_operation_id,
                    _WorkspaceOperationRecord.tenant_id == self.tenant_id,
                    _WorkspaceOperationRecord.workspace_id == self.workspace_id,
                    _WorkspaceOperationRecord.status == ChangeStatus.APPLIED.value,
                )
                .values(
                    status=ChangeStatus.ROLLED_BACK.value,
                    reverted_by_operation_id=rollback_operation_id,
                )
            )
            if rollback_result.rowcount != 1 or original_result.rowcount != 1:
                session.rollback()
                raise WorkspaceHistoryError("回滚历史状态发生冲突，未能完成确认。")
            session.commit()

    def get_operation(self, operation_id: str) -> StoredOperation:
        """读取当前逻辑工作区的一次操作台账。"""

        with self._lock, self._session() as session:
            record = session.scalar(
                select(_WorkspaceOperationRecord).where(
                    _WorkspaceOperationRecord.operation_id == operation_id,
                    _WorkspaceOperationRecord.tenant_id == self.tenant_id,
                    _WorkspaceOperationRecord.workspace_id == self.workspace_id,
                )
            )
            if record is None:
                raise WorkspaceHistoryError(f"找不到历史操作：{operation_id}")
            try:
                status = ChangeStatus(record.status)
            except ValueError as error:
                raise WorkspaceHistoryError(
                    f"历史操作状态无法识别：{record.status}"
                ) from error
            return StoredOperation(
                operation_id=record.operation_id,
                tool_name=record.tool_name,
                origin=record.origin,
                status=status,
                paths=tuple(item.path for item in record.files),
                base_commit_id=record.base_commit_id,
                commit_id=record.commit_id,
                reverted_by_operation_id=record.reverted_by_operation_id,
                error_message=record.error_message,
                created_at=record.created_at,
            )

    @contextmanager
    def _session(self) -> Iterator[Session]:
        """统一管理短生命周期 Session，并转换数据库基础设施异常。"""

        session = self._session_factory()
        try:
            yield session
        except WorkspaceHistoryError:
            session.rollback()
            raise
        except SQLAlchemyError as error:
            session.rollback()
            raise WorkspaceHistoryError("工作区操作台账访问失败。") from error
        finally:
            session.close()

    @classmethod
    def from_config(
        cls,
        config: WorkspaceHistoryConfig,
        *,
        tenant_id: str,
        workspace_id: str,
    ) -> Self:
        """按照 SQLAlchemy URL 创建统一仓储，数据库切换不影响业务代码。

        SQLite URL 会自动启用 WAL、外键与线程池连接参数；MySQL、PostgreSQL
        等其他 URL 则直接交给对应 SQLAlchemy 方言和驱动。
        """

        engine, local_root = _engine_from_config(config)

        repository = cls(
            engine,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            local_root=local_root,
        )
        return repository

    def _migrate_legacy_sqlite_tables(self) -> None:
        """迁移早期 sqlite3 版本的操作摘要，旧 Diff 不再复制。

        旧实现没有 Tree/Commit，因此旧操作只能保留审计字段和文件路径，不能
        伪造为新的 Git 历史；新操作会完整写入 base_commit_id 和 commit_id。
        """

        table_names = set(inspect(self._engine).get_table_names())
        if "operations" not in table_names:
            return
        try:
            with self._engine.begin() as connection:
                if "workspace_binding" in table_names:
                    connection.execute(
                        text(
                            """
                            INSERT OR IGNORE INTO workspace_bindings (
                                tenant_id, workspace_id, root_digest, workspace_root
                            )
                            SELECT :tenant_id, :workspace_id,
                                   root_digest, workspace_root
                            FROM workspace_binding WHERE singleton = 1
                            """
                        ),
                        {
                            "tenant_id": self.tenant_id,
                            "workspace_id": self.workspace_id,
                        },
                    )
                connection.execute(
                    text(
                        """
                        INSERT OR IGNORE INTO workspace_operations (
                            operation_id, tenant_id, workspace_id, run_id,
                            tool_name, origin, status, base_commit_id, commit_id,
                            reverted_by_operation_id, error_message, created_at
                        )
                        SELECT operation_id, tenant_id, workspace_id, run_id,
                               tool_name, 'legacy', status, NULL, NULL,
                               reverted_by_operation_id, error_message, created_at
                        FROM operations
                        """
                    )
                )
                if "file_changes" in table_names:
                    connection.execute(
                        text(
                            """
                            INSERT OR IGNORE INTO workspace_operation_files (
                                operation_id, sequence, path
                            )
                            SELECT operation_id, sequence, path FROM file_changes
                            """
                        )
                    )
        except SQLAlchemyError as error:
            raise WorkspaceHistoryError("旧版 SQLite 历史迁移失败。") from error


def _workspace_lock(namespace: str) -> threading.RLock:
    """为同一进程内的每个逻辑工作区返回共享可重入锁。"""

    with _LOCKS_GUARD:
        lock = _WORKSPACE_LOCKS.get(namespace)
        if lock is None:
            lock = threading.RLock()
            _WORKSPACE_LOCKS[namespace] = lock
        return lock


def _engine_from_config(
    config: WorkspaceHistoryConfig,
) -> tuple[Engine, Path | None]:
    """按数据库配置返回进程级共享 Engine 与本地数据库目录。"""

    cache_source = "|".join(
        (
            config.database_url,
            str(config.echo),
            str(config.pool_pre_ping),
            str(config.pool_recycle_seconds),
            str(config.sqlite_timeout_seconds),
        )
    )
    cache_key = hashlib.sha256(cache_source.encode("utf-8")).hexdigest()
    with _ENGINES_GUARD:
        cached = _ENGINES.get(cache_key)
        if cached is not None:
            return cached

        try:
            url = make_url(config.database_url)
            local_root: Path | None = None
            connect_args: dict[str, object] = {}
            if url.get_backend_name() == "sqlite":
                connect_args = {
                    "check_same_thread": False,
                    "timeout": config.sqlite_timeout_seconds,
                }
                if url.database not in {None, "", ":memory:"}:
                    database_path = Path(url.database).resolve(strict=False)
                    database_path.parent.mkdir(parents=True, exist_ok=True)
                    local_root = database_path.parent

            engine = create_engine(
                url,
                connect_args=connect_args,
                echo=config.echo,
                pool_pre_ping=config.pool_pre_ping,
                pool_recycle=config.pool_recycle_seconds,
            )
        except (ImportError, ModuleNotFoundError, SQLAlchemyError) as error:
            raise WorkspaceHistoryError(
                "无法创建历史数据库 Engine，请检查 SQLAlchemy URL 和数据库驱动。"
            ) from error

        if url.get_backend_name() == "sqlite":

            @event.listens_for(engine, "connect")
            def _configure_sqlite(connection: object, _: object) -> None:
                """为每条 SQLite DB-API 连接启用外键并使用 WAL 日志。"""

                cursor = connection.cursor()  # type: ignore[attr-defined]
                cursor.execute("PRAGMA foreign_keys = ON")
                cursor.execute("PRAGMA journal_mode = WAL")
                cursor.close()

        resolved = (engine, local_root)
        _ENGINES[cache_key] = resolved
        return resolved
