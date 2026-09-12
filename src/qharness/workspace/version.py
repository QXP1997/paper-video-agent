# -*- coding: utf-8 -*-
"""文件版本内容存储接口及本地 Dulwich 实现。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from dulwich.objects import Blob
from dulwich.repo import Repo

from qharness.exception import WorkspaceHistoryError
from qharness.workspace.identity import workspace_storage_namespace


_LOCKS_GUARD = threading.Lock()
_VERSION_STORE_LOCKS: dict[str, threading.RLock] = {}


@runtime_checkable
class FileVersionStore(Protocol):
    """文件历史内容的可替换存储接口。

    本地客户端可以使用 Dulwich；分布式服务可以实现 S3、MinIO 或其他对象
    存储版本。业务层只依赖版本编号和原始字节，不感知具体存储产品。
    """

    @property
    def local_root(self) -> Path | None:
        """返回本地存储根目录；远程对象存储返回 None。"""
        ...

    def save(self, content: bytes) -> str:
        """保存文件原始字节，并返回稳定版本编号。"""
        ...

    def load(self, revision: str) -> bytes:
        """按版本编号读取文件原始字节。"""
        ...


class DulwichFileVersionStore:
    """使用私有裸 Git 仓库保存文件 Blob 的本地版本存储。"""

    def __init__(
        self,
        storage_root: str | Path,
        *,
        tenant_id: str,
        workspace_id: str,
    ) -> None:
        """创建或打开当前逻辑工作区的私有 Git 对象仓库。"""

        # 所有工作区版本内容共同使用的宿主目录。
        self._local_root = Path(storage_root).expanduser().resolve(strict=False)
        self._local_root.mkdir(parents=True, exist_ok=True)

        # 摘要目录隔离租户和工作区，不暴露外部标识。
        self.namespace = workspace_storage_namespace(tenant_id, workspace_id)
        self.workspace_storage_root = self._local_root / self.namespace
        self.repository_path = self.workspace_storage_root / "objects.git"
        self.workspace_storage_root.mkdir(parents=True, exist_ok=True)

        # 同一路径的多个 Run 或实例共用进程级锁。
        self._lock = _version_store_lock(self.repository_path)
        with self._lock:
            self._initialize_repository()

    @property
    def local_root(self) -> Path:
        """返回 Dulwich 文件版本的本地宿主目录。"""

        return self._local_root

    def save(self, content: bytes) -> str:
        """把文件字节保存为 Git Blob，并返回稳定对象编号。"""

        blob = Blob.from_string(content)
        with self._lock, Repo(str(self.repository_path)) as repository:
            repository.object_store.add_object(blob)
            # 使用内部引用保护仍被元数据引用的 Blob，避免未来 GC 误删。
            repository.refs[b"refs/qharness/blobs/" + blob.id] = blob.id
        return blob.id.decode("ascii")

    def load(self, revision: str) -> bytes:
        """按 Git 对象编号读取原始文件字节。"""

        try:
            object_id = revision.encode("ascii")
            with self._lock, Repo(str(self.repository_path)) as repository:
                stored_object = repository[object_id]
        except (KeyError, OSError, ValueError) as error:
            raise WorkspaceHistoryError(
                f"历史文件版本不存在或已经损坏：{revision}"
            ) from error
        if not isinstance(stored_object, Blob):
            raise WorkspaceHistoryError(f"历史对象不是文件 Blob：{revision}")
        return bytes(stored_object.data)

    def _initialize_repository(self) -> None:
        """创建只由 QHarness 使用的裸 Git 对象仓库。"""

        if self.repository_path.exists():
            try:
                with Repo(str(self.repository_path)):
                    return
            except Exception as error:
                raise WorkspaceHistoryError(
                    f"私有历史仓库无法打开：{self.repository_path}"
                ) from error
        try:
            Repo.init_bare(str(self.repository_path), mkdir=True).close()
        except OSError as error:
            raise WorkspaceHistoryError(
                f"无法创建私有历史仓库：{self.repository_path}"
            ) from error


def _version_store_lock(repository_path: Path) -> threading.RLock:
    """按规范化仓库路径复用 Dulwich 进程级访问锁。"""

    lock_key = str(repository_path.resolve(strict=False))
    with _LOCKS_GUARD:
        lock = _VERSION_STORE_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.RLock()
            _VERSION_STORE_LOCKS[lock_key] = lock
        return lock
