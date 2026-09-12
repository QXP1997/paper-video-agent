# -*- coding: utf-8 -*-
"""工作区历史元数据接口及 SQLite 本地实现。"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from qharness.exception import WorkspaceHistoryError
from qharness.workspace.identity import (
    validate_history_identifier,
    workspace_storage_namespace,
)
from qharness.workspace.models import ChangeStatus, FileChange


_SCHEMA_VERSION = 1
_LOCKS_GUARD = threading.Lock()
_WORKSPACE_LOCKS: dict[str, threading.RLock] = {}


@dataclass(frozen=True, slots=True)
class StoredOperation:
    """从持久化历史中读取的一次完整文件操作。"""

    # 写工具返回并供查询、回滚使用的唯一操作编号。
    operation_id: str

    # 创建记录的工具名称。
    tool_name: str

    # 操作当前是待确认、已应用、失败还是已回滚。
    status: ChangeStatus

    # 本次操作涉及的文件变化；当前写工具为一个，后续支持多个。
    files: tuple[FileChange, ...]

    # 如果原操作已回滚，这里保存对应回滚操作编号。
    reverted_by_operation_id: str | None

    # 操作在实际写文件前失败时保存的诊断信息。
    error_message: str | None


@runtime_checkable
class WorkspaceHistoryRepository(Protocol):
    """工作区变更元数据的数据库无关接口。

    SQLite、PostgreSQL 和 MySQL 实现都必须提供相同的条件状态更新与事务
    语义，业务服务不得直接依赖连接对象、SQL 方言或连接池。
    """

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
        files: tuple[FileChange, ...],
    ) -> str:
        """创建 pending 操作并返回操作编号。"""
        ...

    def complete_operation(self, operation_id: str) -> None:
        """把 pending 操作条件更新为 applied。"""
        ...

    def fail_operation(self, operation_id: str, message: str) -> None:
        """把文件落盘前失败的 pending 操作标记为 failed。"""
        ...

    def complete_rollback(
        self,
        rollback_operation_id: str,
        original_operation_id: str,
    ) -> None:
        """在一个事务中确认回滚，并更新原操作状态。"""
        ...

    def get_operation(self, operation_id: str) -> StoredOperation:
        """读取一次操作及其全部文件变化。"""
        ...


class SqliteWorkspaceHistoryRepository:
    """使用 SQLite 保存一个逻辑工作区的操作元数据。"""

    def __init__(
        self,
        history_root: str | Path,
        *,
        tenant_id: str,
        workspace_id: str,
    ) -> None:
        """初始化当前工作区对应的 SQLite 元数据索引。"""

        # 两个逻辑标识只进入数据库和目录摘要，不直接用作目录名称。
        self.tenant_id = validate_history_identifier(tenant_id, "tenant_id")
        self.workspace_id = validate_history_identifier(
            workspace_id,
            "workspace_id",
        )

        # 所有工作区 SQLite 索引共同使用的宿主目录。
        self._local_root = Path(history_root).expanduser().resolve(strict=False)
        self._local_root.mkdir(parents=True, exist_ok=True)

        # 固定长度摘要既隔离不同工作区，也避免外部标识参与路径解析。
        self.namespace = workspace_storage_namespace(
            self.tenant_id,
            self.workspace_id,
        )

        # 当前逻辑工作区独享的 SQLite 索引目录。
        self.workspace_history_root = self._local_root / self.namespace
        self.database_path = self.workspace_history_root / "history.sqlite3"
        self.workspace_history_root.mkdir(parents=True, exist_ok=True)

        # 同一进程内同一工作区的修改串行执行，避免多个 Run 互相覆盖。
        self._lock = _workspace_lock(self.namespace)

        with self._lock:
            self._initialize_database()

    @property
    def local_root(self) -> Path:
        """返回 SQLite 历史索引的本地宿主目录。"""

        return self._local_root

    def bind_workspace_root(self, workspace_root: str | Path) -> None:
        """把逻辑工作区固定到规范化根目录，拒绝标识被复用于其他目录。"""

        normalized_root = str(Path(workspace_root).resolve(strict=True))
        comparable_root = os.path.normcase(normalized_root)
        root_digest = hashlib.sha256(comparable_root.encode("utf-8")).hexdigest()
        with (
            self._lock,
            contextlib.closing(self._connect()) as connection,
            connection,
        ):
            row = connection.execute(
                "SELECT root_digest FROM workspace_binding WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO workspace_binding (
                        singleton, root_digest, workspace_root
                    ) VALUES (1, ?, ?)
                    """,
                    (root_digest, normalized_root),
                )
                return
            if row["root_digest"] != root_digest:
                raise WorkspaceHistoryError(
                    "相同 tenant_id 与 workspace_id 已绑定到另一个工作区根目录；"
                    "拒绝复用其文件历史。"
                )

    def begin_operation(
        self,
        *,
        run_id: str,
        tool_name: str,
        files: tuple[FileChange, ...],
    ) -> str:
        """在修改文件前写入 pending 记录，供异常退出后诊断恢复。"""

        operation_id = uuid.uuid4().hex
        with (
            self._lock,
            contextlib.closing(self._connect()) as connection,
            connection,
        ):
            connection.execute(
                """
                INSERT INTO operations (
                    operation_id, tenant_id, workspace_id, run_id,
                    tool_name, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    self.tenant_id,
                    self.workspace_id,
                    validate_history_identifier(run_id, "run_id"),
                    tool_name,
                    ChangeStatus.PENDING.value,
                ),
            )
            connection.executemany(
                """
                INSERT INTO file_changes (
                    operation_id, sequence, path,
                    before_exists, after_exists,
                    before_revision, after_revision,
                    before_sha256, after_sha256, diff_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        operation_id,
                        sequence,
                        change.path,
                        int(change.before_exists),
                        int(change.after_exists),
                        change.before_revision,
                        change.after_revision,
                        change.before_sha256,
                        change.after_sha256,
                        change.diff,
                    )
                    for sequence, change in enumerate(files)
                ],
            )
        return operation_id

    def complete_operation(self, operation_id: str) -> None:
        """把已经成功落盘的 pending 操作标记为 applied。"""

        self._change_status(
            operation_id,
            expected=ChangeStatus.PENDING,
            target=ChangeStatus.APPLIED,
        )

    def fail_operation(self, operation_id: str, message: str) -> None:
        """记录文件写入阶段失败，保留审计信息供后续排查。"""

        with (
            self._lock,
            contextlib.closing(self._connect()) as connection,
            connection,
        ):
            cursor = connection.execute(
                """
                UPDATE operations
                SET status = ?, error_message = ?
                WHERE operation_id = ? AND status = ?
                """,
                (
                    ChangeStatus.FAILED.value,
                    message[:2000],
                    operation_id,
                    ChangeStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkspaceHistoryError(
                    f"无法把历史操作标记为失败：{operation_id}"
                )

    def complete_rollback(
        self,
        rollback_operation_id: str,
        original_operation_id: str,
    ) -> None:
        """原子确认回滚操作，并把原操作标记为 rolled_back。"""

        with (
            self._lock,
            contextlib.closing(self._connect()) as connection,
            connection,
        ):
            rollback_cursor = connection.execute(
                """
                UPDATE operations SET status = ?
                WHERE operation_id = ? AND status = ?
                """,
                (
                    ChangeStatus.APPLIED.value,
                    rollback_operation_id,
                    ChangeStatus.PENDING.value,
                ),
            )
            original_cursor = connection.execute(
                """
                UPDATE operations
                SET status = ?, reverted_by_operation_id = ?
                WHERE operation_id = ? AND status = ?
                """,
                (
                    ChangeStatus.ROLLED_BACK.value,
                    rollback_operation_id,
                    original_operation_id,
                    ChangeStatus.APPLIED.value,
                ),
            )
            if rollback_cursor.rowcount != 1 or original_cursor.rowcount != 1:
                raise WorkspaceHistoryError("回滚历史状态发生冲突，未能完成确认。")

    def get_operation(self, operation_id: str) -> StoredOperation:
        """按操作编号读取历史记录和全部文件变化。"""

        with (
            self._lock,
            contextlib.closing(self._connect()) as connection,
            connection,
        ):
            operation_row = connection.execute(
                """
                SELECT operation_id, tool_name, status,
                       reverted_by_operation_id, error_message
                FROM operations WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if operation_row is None:
                raise WorkspaceHistoryError(f"找不到历史操作：{operation_id}")
            file_rows = connection.execute(
                """
                SELECT path, before_exists, after_exists,
                       before_revision, after_revision,
                       before_sha256, after_sha256, diff_text
                FROM file_changes
                WHERE operation_id = ? ORDER BY sequence
                """,
                (operation_id,),
            ).fetchall()

        try:
            status = ChangeStatus(operation_row["status"])
        except ValueError as error:
            raise WorkspaceHistoryError(
                f"历史操作状态无法识别：{operation_row['status']}"
            ) from error
        files = tuple(
            FileChange(
                path=row["path"],
                before_exists=bool(row["before_exists"]),
                after_exists=bool(row["after_exists"]),
                before_revision=row["before_revision"],
                after_revision=row["after_revision"],
                before_sha256=row["before_sha256"],
                after_sha256=row["after_sha256"],
                diff=row["diff_text"],
            )
            for row in file_rows
        )
        return StoredOperation(
            operation_id=operation_row["operation_id"],
            tool_name=operation_row["tool_name"],
            status=status,
            files=files,
            reverted_by_operation_id=operation_row["reverted_by_operation_id"],
            error_message=operation_row["error_message"],
        )

    def _change_status(
        self,
        operation_id: str,
        *,
        expected: ChangeStatus,
        target: ChangeStatus,
    ) -> None:
        """使用期望状态更新操作，避免并发下重复确认。"""

        with (
            self._lock,
            contextlib.closing(self._connect()) as connection,
            connection,
        ):
            cursor = connection.execute(
                """
                UPDATE operations SET status = ?
                WHERE operation_id = ? AND status = ?
                """,
                (target.value, operation_id, expected.value),
            )
            if cursor.rowcount != 1:
                raise WorkspaceHistoryError(
                    f"历史操作状态更新冲突：{operation_id}"
                )

    def _initialize_database(self) -> None:
        """初始化 SQLite Schema，并启用完整性和并发相关选项。"""

        with contextlib.closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info (
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workspace_binding (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    root_digest TEXT NOT NULL,
                    workspace_root TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reverted_by_operation_id TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS file_changes (
                    operation_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    path TEXT NOT NULL,
                    before_exists INTEGER NOT NULL,
                    after_exists INTEGER NOT NULL,
                    before_revision TEXT,
                    after_revision TEXT,
                    before_sha256 TEXT,
                    after_sha256 TEXT,
                    diff_text TEXT NOT NULL,
                    PRIMARY KEY (operation_id, sequence),
                    FOREIGN KEY (operation_id)
                        REFERENCES operations(operation_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_operations_run
                    ON operations(run_id, created_at);
                """
            )
            row = connection.execute(
                "SELECT version FROM schema_info LIMIT 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_info(version) VALUES (?)",
                    (_SCHEMA_VERSION,),
                )
            elif row["version"] != _SCHEMA_VERSION:
                raise WorkspaceHistoryError(
                    f"不支持的历史数据库版本：{row['version']}"
                )

    def _connect(self) -> sqlite3.Connection:
        """创建短生命周期数据库连接，支持工具在线程池内并发执行。"""

        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection


def _workspace_lock(namespace: str) -> threading.RLock:
    """为同一进程内的每个逻辑工作区返回共享可重入锁。"""

    with _LOCKS_GUARD:
        lock = _WORKSPACE_LOCKS.get(namespace)
        if lock is None:
            lock = threading.RLock()
            _WORKSPACE_LOCKS[namespace] = lock
        return lock
