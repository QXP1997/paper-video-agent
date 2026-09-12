# -*- coding: utf-8 -*-
"""基于 Dulwich 的完整工作区 Tree、Commit、Diff 与历史实现。"""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Protocol, TypeAlias, runtime_checkable

from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

from qharness.exception import WorkspaceConflictError, WorkspaceHistoryError
from qharness.workspace.identity import workspace_storage_namespace
from qharness.workspace.models import FileChange


_HEAD_REF = b"refs/qharness/workspace-head"
_AUTHOR = b"QHarness <history@qharness.local>"
_GIT_TREE_MODE = 0o040000
_GIT_FILE_MODE = 0o100644
_GIT_EXECUTABLE_MODE = 0o100755
_LOCKS_GUARD = threading.Lock()
_VERSION_STORE_LOCKS: dict[str, threading.RLock] = {}

# 文件内容与普通权限位。None 表示从 Tree 中删除文件。
VersionFile: TypeAlias = tuple[bytes, int]


@dataclass(frozen=True, slots=True)
class WorkspaceCommitResult:
    """创建或复用工作区 Commit 后返回的结果。"""

    # 当前最终 Commit 编号。
    commit_id: str

    # 当前 Commit 的第一父提交；初始基线没有父提交。
    parent_commit_id: str | None

    # 本次是否真的创建了新 Commit。
    created: bool

    # 相对于父提交实际发生变化的文件。
    files: tuple[FileChange, ...]


@dataclass(frozen=True, slots=True)
class FileHistoryEntry:
    """某个文件在私有 Git 历史中的一次变化。"""

    commit_id: str
    parent_commit_id: str | None
    message: str
    timestamp: int
    action: str
    blob_id: str | None

    def to_dict(self) -> dict[str, object]:
        """转换为可直接写入工具 JSON 结果的字典。"""

        return {
            "commit_id": self.commit_id,
            "parent_commit_id": self.parent_commit_id,
            "message": self.message,
            "timestamp": self.timestamp,
            "action": self.action,
            "blob_id": self.blob_id,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    """当前磁盘工作区相对于私有历史 HEAD 的状态。"""

    head_commit_id: str | None
    files: tuple[FileChange, ...]

    @property
    def is_dirty(self) -> bool:
        """返回当前磁盘是否存在尚未建立检查点的变化。"""

        return bool(self.files)


@runtime_checkable
class FileVersionStore(Protocol):
    """工作区版本历史的可替换接口。

    名称为兼容现有调用方而保留；接口现在管理完整 Tree 和 Commit，不再只是
    孤立的文件 Blob。
    """

    @property
    def local_root(self) -> Path | None:
        """返回本地存储根目录；远程实现返回 None。"""
        ...

    def head_commit_id(self) -> str | None:
        """返回当前工作区私有历史 HEAD。"""
        ...

    def checkpoint_workspace(
        self,
        workspace_root: Path,
        *,
        message: str,
    ) -> WorkspaceCommitResult:
        """将当前磁盘工作区保存为基线或外部修改检查点。"""
        ...

    def commit_changes(
        self,
        base_commit_id: str,
        changes: Mapping[str, VersionFile | None],
        *,
        message: str,
    ) -> WorkspaceCommitResult:
        """在指定父提交上应用文件变化并创建新 Commit。"""
        ...

    def diff_commits(
        self,
        base_commit_id: str | None,
        commit_id: str,
        *,
        paths: tuple[str, ...] | None = None,
    ) -> tuple[FileChange, ...]:
        """根据两个 Commit 动态计算文件变化。"""
        ...

    def read_file(self, commit_id: str, path: str) -> bytes | None:
        """读取 Commit 中的文件；不存在时返回 None。"""
        ...

    def read_file_state(self, commit_id: str, path: str) -> VersionFile | None:
        """读取 Commit 中的文件内容与权限位。"""
        ...

    def file_history(self, path: str, *, limit: int) -> tuple[FileHistoryEntry, ...]:
        """返回文件最近的提交历史。"""
        ...

    def workspace_status(self, workspace_root: Path) -> WorkspaceStatus:
        """比较当前磁盘与 HEAD，但不创建 Commit。"""
        ...


class DulwichFileVersionStore:
    """使用私有裸 Git 仓库保存完整工作区版本历史。"""

    def __init__(
        self,
        storage_root: str | Path,
        *,
        tenant_id: str,
        workspace_id: str,
    ) -> None:
        """创建或打开当前逻辑工作区的私有 Git 仓库。"""

        self._local_root = Path(storage_root).expanduser().resolve(strict=False)
        self._local_root.mkdir(parents=True, exist_ok=True)
        self.namespace = workspace_storage_namespace(tenant_id, workspace_id)
        self.workspace_storage_root = self._local_root / self.namespace
        self.repository_path = self.workspace_storage_root / "objects.git"
        self.workspace_storage_root.mkdir(parents=True, exist_ok=True)
        self._lock = _version_store_lock(self.repository_path)
        with self._lock:
            self._initialize_repository()

    @property
    def local_root(self) -> Path:
        """返回 Dulwich 私有历史的本地宿主目录。"""

        return self._local_root

    def head_commit_id(self) -> str | None:
        """返回 QHarness 专用引用指向的 Commit。"""

        with self._lock, Repo(str(self.repository_path)) as repository:
            return self._head(repository)

    def checkpoint_workspace(
        self,
        workspace_root: Path,
        *,
        message: str,
    ) -> WorkspaceCommitResult:
        """扫描当前磁盘并创建初始基线或外部修改检查点。"""

        resolved_root = workspace_root.resolve(strict=True)
        with self._lock, Repo(str(self.repository_path)) as repository:
            parent_id = self._head(repository)
            old_entries = self._entries_for_commit(repository, parent_id)
            new_entries = self._snapshot_workspace(repository, resolved_root)
            new_tree_id = self._write_tree(repository, new_entries)
            if parent_id is not None:
                parent = self._load_commit(repository, parent_id)
                if parent.tree == new_tree_id:
                    parent_parent_id = (
                        parent.parents[0].decode("ascii")
                        if parent.parents
                        else None
                    )
                    return WorkspaceCommitResult(
                        parent_id,
                        parent_parent_id,
                        False,
                        (),
                    )

            files = self._diff_entries(repository, old_entries, new_entries)
            commit_id = self._create_commit(
                repository,
                tree_id=new_tree_id,
                parent_id=parent_id,
                message=message,
            )
            return WorkspaceCommitResult(commit_id, parent_id, True, files)

    def commit_changes(
        self,
        base_commit_id: str,
        changes: Mapping[str, VersionFile | None],
        *,
        message: str,
    ) -> WorkspaceCommitResult:
        """从父提交构造新 Tree，并使用比较交换推进私有 HEAD。"""

        if not changes:
            raise WorkspaceHistoryError("创建 Commit 时至少需要一个文件变化。")
        with self._lock, Repo(str(self.repository_path)) as repository:
            current_head = self._head(repository)
            if current_head != base_commit_id:
                raise WorkspaceConflictError(
                    "工作区私有历史 HEAD 已被其他 Run 推进，请重新读取后再修改。"
                )
            old_entries = self._entries_for_commit(repository, base_commit_id)
            new_entries = dict(old_entries)
            for raw_path, version_file in changes.items():
                path = _validate_repository_path(raw_path)
                if version_file is None:
                    new_entries.pop(path, None)
                    continue
                content, file_mode = version_file
                blob = Blob.from_string(content)
                repository.object_store.add_object(blob)
                mode = _git_mode(file_mode)
                new_entries[path] = (mode, blob.id)

            tree_id = self._write_tree(repository, new_entries)
            parent = self._load_commit(repository, base_commit_id)
            if parent.tree == tree_id:
                return WorkspaceCommitResult(
                    base_commit_id,
                    base_commit_id,
                    False,
                    (),
                )
            files = self._diff_entries(repository, old_entries, new_entries)
            commit_id = self._create_commit(
                repository,
                tree_id=tree_id,
                parent_id=base_commit_id,
                message=message,
            )
            return WorkspaceCommitResult(commit_id, base_commit_id, True, files)

    def diff_commits(
        self,
        base_commit_id: str | None,
        commit_id: str,
        *,
        paths: tuple[str, ...] | None = None,
    ) -> tuple[FileChange, ...]:
        """实时读取 Git 对象并计算 Diff，不读取数据库中的旧 Diff。"""

        with self._lock, Repo(str(self.repository_path)) as repository:
            before = self._entries_for_commit(repository, base_commit_id)
            after = self._entries_for_commit(repository, commit_id)
            selected = None
            if paths is not None:
                selected = {_validate_repository_path(path) for path in paths}
            return self._diff_entries(repository, before, after, selected)

    def read_file(self, commit_id: str, path: str) -> bytes | None:
        """从指定 Commit 的 Tree 读取文件原始字节。"""

        state = self.read_file_state(commit_id, path)
        return None if state is None else state[0]

    def read_file_state(self, commit_id: str, path: str) -> VersionFile | None:
        """从指定 Commit 读取文件内容及可恢复的普通权限位。"""

        normalized = _validate_repository_path(path)
        with self._lock, Repo(str(self.repository_path)) as repository:
            entry = self._entries_for_commit(repository, commit_id).get(normalized)
            if entry is None:
                return None
            file_mode = 0o755 if entry[0] == _GIT_EXECUTABLE_MODE else 0o644
            return self._load_blob(repository, entry[1]), file_mode

    def file_history(
        self,
        path: str,
        *,
        limit: int,
    ) -> tuple[FileHistoryEntry, ...]:
        """沿第一父提交历史查找指定文件发生变化的 Commit。"""

        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit 必须是大于 0 的整数。")
        normalized = _validate_repository_path(path)
        with self._lock, Repo(str(self.repository_path)) as repository:
            current_id = self._head(repository)
            result: list[FileHistoryEntry] = []
            current_entries = self._entries_for_commit(repository, current_id)
            while current_id is not None and len(result) < limit:
                commit = self._load_commit(repository, current_id)
                parent_id = (
                    commit.parents[0].decode("ascii") if commit.parents else None
                )
                parent_entries = self._entries_for_commit(repository, parent_id)
                current_entry = current_entries.get(normalized)
                parent_entry = parent_entries.get(normalized)
                if current_entry != parent_entry:
                    action = _entry_action(parent_entry, current_entry)
                    result.append(
                        FileHistoryEntry(
                            commit_id=current_id,
                            parent_commit_id=parent_id,
                            message=commit.message.decode("utf-8", errors="replace"),
                            timestamp=commit.commit_time,
                            action=action,
                            blob_id=(
                                current_entry[1].decode("ascii")
                                if current_entry is not None
                                else None
                            ),
                        )
                    )
                current_id = parent_id
                current_entries = parent_entries
            return tuple(result)

    def workspace_status(self, workspace_root: Path) -> WorkspaceStatus:
        """动态比较磁盘 Tree 与 HEAD，供模型识别用户的未提交修改。"""

        resolved_root = workspace_root.resolve(strict=True)
        with self._lock, Repo(str(self.repository_path)) as repository:
            head_id = self._head(repository)
            committed = self._entries_for_commit(repository, head_id)
            working = self._snapshot_workspace(repository, resolved_root)
            return WorkspaceStatus(
                head_commit_id=head_id,
                files=self._diff_entries(repository, committed, working),
            )

    def save(self, content: bytes) -> str:
        """兼容旧调用：保存独立 Blob 并用内部引用保护。"""

        blob = Blob.from_string(content)
        with self._lock, Repo(str(self.repository_path)) as repository:
            repository.object_store.add_object(blob)
            repository.refs[b"refs/qharness/blobs/" + blob.id] = blob.id
        return blob.id.decode("ascii")

    def load(self, revision: str) -> bytes:
        """兼容旧调用：按 Blob 编号读取原始字节。"""

        with self._lock, Repo(str(self.repository_path)) as repository:
            return self._load_blob(repository, revision.encode("ascii"))

    def _snapshot_workspace(
        self,
        repository: Repo,
        root: Path,
    ) -> dict[str, tuple[int, bytes]]:
        """安全扫描普通文件；私有元数据和用户项目 .git 不进入历史。"""

        entries: dict[str, tuple[int, bytes]] = {}
        for directory, directory_names, file_names in os.walk(root):
            directory_names[:] = sorted(
                name
                for name in directory_names
                if name.casefold() not in {".git", ".qharness"}
                and not (Path(directory) / name).is_symlink()
            )
            for file_name in sorted(file_names):
                file_path = Path(directory) / file_name
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                relative_path = file_path.relative_to(root).as_posix()
                try:
                    content = file_path.read_bytes()
                    file_mode = file_path.stat().st_mode
                except OSError as error:
                    raise WorkspaceHistoryError(
                        f"无法为工作区创建版本快照：{relative_path}"
                    ) from error
                blob = Blob.from_string(content)
                repository.object_store.add_object(blob)
                mode = _git_mode(file_mode)
                entries[relative_path] = (mode, blob.id)
        return entries

    def _write_tree(
        self,
        repository: Repo,
        entries: Mapping[str, tuple[int, bytes]],
    ) -> bytes:
        """把扁平路径映射递归构造成 Git Tree 对象。"""

        root: dict[str, object] = {}
        for path, value in entries.items():
            parts = PurePosixPath(path).parts
            node = root
            for part in parts[:-1]:
                child = node.setdefault(part, {})
                if not isinstance(child, dict):
                    raise WorkspaceHistoryError(f"文件与目录路径发生冲突：{path}")
                node = child
            if parts[-1] in node and isinstance(node[parts[-1]], dict):
                raise WorkspaceHistoryError(f"文件与目录路径发生冲突：{path}")
            node[parts[-1]] = value

        def store_node(node: dict[str, object]) -> bytes:
            tree = Tree()
            for name in sorted(node):
                value = node[name]
                if isinstance(value, dict):
                    object_id = store_node(value)
                    tree.add(name.encode("utf-8"), _GIT_TREE_MODE, object_id)
                else:
                    mode, object_id = value  # type: ignore[misc]
                    tree.add(name.encode("utf-8"), mode, object_id)
            repository.object_store.add_object(tree)
            return tree.id

        return store_node(root)

    def _entries_for_commit(
        self,
        repository: Repo,
        commit_id: str | None,
    ) -> dict[str, tuple[int, bytes]]:
        """把 Commit Tree 展开为相对路径到文件对象的映射。"""

        if commit_id is None:
            return {}
        commit = self._load_commit(repository, commit_id)
        result: dict[str, tuple[int, bytes]] = {}

        def visit(tree_id: bytes, prefix: str) -> None:
            stored = repository[tree_id]
            if not isinstance(stored, Tree):
                raise WorkspaceHistoryError("Git Tree 对象类型错误。")
            for entry in stored.iteritems():
                name = entry.path.decode("utf-8")
                path = f"{prefix}/{name}" if prefix else name
                if stat.S_ISDIR(entry.mode):
                    visit(entry.sha, path)
                else:
                    result[path] = (entry.mode, entry.sha)

        visit(commit.tree, "")
        return result

    def _diff_entries(
        self,
        repository: Repo,
        before: Mapping[str, tuple[int, bytes]],
        after: Mapping[str, tuple[int, bytes]],
        selected: set[str] | None = None,
    ) -> tuple[FileChange, ...]:
        """根据 Tree 条目生成结构化文件变化和 unified diff。"""

        paths = set(before) | set(after)
        if selected is not None:
            paths &= selected
        changes: list[FileChange] = []
        for path in sorted(paths):
            before_entry = before.get(path)
            after_entry = after.get(path)
            if before_entry == after_entry:
                continue
            before_content = (
                self._load_blob(repository, before_entry[1])
                if before_entry is not None
                else None
            )
            after_content = (
                self._load_blob(repository, after_entry[1])
                if after_entry is not None
                else None
            )
            changes.append(
                _build_file_change(
                    path,
                    before_entry,
                    after_entry,
                    before_content,
                    after_content,
                )
            )
        return tuple(changes)

    def _create_commit(
        self,
        repository: Repo,
        *,
        tree_id: bytes,
        parent_id: str | None,
        message: str,
    ) -> str:
        """写入 Commit，并以 QHarness 专用引用做比较交换。"""

        timestamp = int(time.time())
        commit = Commit()
        commit.tree = tree_id
        commit.parents = [] if parent_id is None else [parent_id.encode("ascii")]
        commit.author = _AUTHOR
        commit.committer = _AUTHOR
        commit.author_time = timestamp
        commit.commit_time = timestamp
        commit.author_timezone = 0
        commit.commit_timezone = 0
        commit.encoding = b"UTF-8"
        commit.message = message.encode("utf-8")
        repository.object_store.add_object(commit)
        expected = None if parent_id is None else parent_id.encode("ascii")
        if not repository.refs.set_if_equals(_HEAD_REF, expected, commit.id):
            raise WorkspaceConflictError(
                "工作区私有历史 HEAD 并发更新失败，请重新执行本次修改。"
            )
        return commit.id.decode("ascii")

    @staticmethod
    def _head(repository: Repo) -> str | None:
        """读取专用引用；首次使用时返回 None。"""

        try:
            return repository.refs[_HEAD_REF].decode("ascii")
        except KeyError:
            return None

    @staticmethod
    def _load_commit(repository: Repo, commit_id: str) -> Commit:
        """读取并验证 Git Commit 对象。"""

        try:
            stored = repository[commit_id.encode("ascii")]
        except (KeyError, OSError, ValueError) as error:
            raise WorkspaceHistoryError(
                f"历史 Commit 不存在或已经损坏：{commit_id}"
            ) from error
        if not isinstance(stored, Commit):
            raise WorkspaceHistoryError(f"历史对象不是 Commit：{commit_id}")
        return stored

    @staticmethod
    def _load_blob(repository: Repo, object_id: bytes) -> bytes:
        """读取并验证 Git Blob 对象。"""

        try:
            stored = repository[object_id]
        except (KeyError, OSError, ValueError) as error:
            revision = object_id.decode("ascii", errors="replace")
            raise WorkspaceHistoryError(
                f"历史文件 Blob 不存在或已经损坏：{revision}"
            ) from error
        if not isinstance(stored, Blob):
            raise WorkspaceHistoryError("历史文件对象不是 Blob。")
        return bytes(stored.data)

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


def _validate_repository_path(path: str) -> str:
    """验证并规范化写入 Git Tree 的工作区相对路径。"""

    if not isinstance(path, str) or not path:
        raise WorkspaceHistoryError("历史文件路径必须是非空字符串。")
    candidate = PurePosixPath(path.replace("\\", "/"))
    if (
        "\x00" in path
        or ":" in path
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise WorkspaceHistoryError(f"历史文件路径不合法：{path}")
    if candidate.parts[0].casefold() in {".git", ".qharness"}:
        raise WorkspaceHistoryError(f"保留目录不能写入私有工作区历史：{path}")
    return candidate.as_posix()


def _entry_action(
    before: tuple[int, bytes] | None,
    after: tuple[int, bytes] | None,
) -> str:
    """根据文件条目是否存在返回 created、deleted 或 modified。"""

    if before is None:
        return "created"
    if after is None:
        return "deleted"
    return "modified"


def _git_mode(file_mode: int) -> int:
    """把操作系统普通权限位转换为 Git 文件模式。"""

    return _GIT_EXECUTABLE_MODE if file_mode & stat.S_IXUSR else _GIT_FILE_MODE


def _build_file_change(
    path: str,
    before_entry: tuple[int, bytes] | None,
    after_entry: tuple[int, bytes] | None,
    before_content: bytes | None,
    after_content: bytes | None,
) -> FileChange:
    """由两个 Blob 构造模型与回滚共用的文件变化对象。"""

    return FileChange(
        path=path,
        before_exists=before_entry is not None,
        after_exists=after_entry is not None,
        before_revision=(
            before_entry[1].decode("ascii") if before_entry is not None else None
        ),
        after_revision=(
            after_entry[1].decode("ascii") if after_entry is not None else None
        ),
        before_sha256=(
            hashlib.sha256(before_content).hexdigest()
            if before_content is not None
            else None
        ),
        after_sha256=(
            hashlib.sha256(after_content).hexdigest()
            if after_content is not None
            else None
        ),
        diff=_content_diff(path, before_content, after_content),
    )


def _content_diff(path: str, before: bytes | None, after: bytes | None) -> str:
    """为文本生成 unified diff；二进制变化返回明确摘要。"""

    import difflib

    try:
        if before is not None and b"\x00" in before[:8192]:
            raise UnicodeDecodeError("utf-8", before, 0, 1, "binary")
        if after is not None and b"\x00" in after[:8192]:
            raise UnicodeDecodeError("utf-8", after, 0, 1, "binary")
        before_text = None if before is None else before.decode("utf-8")
        after_text = None if after is None else after.decode("utf-8")
    except UnicodeDecodeError:
        return f"Binary files a/{path} and b/{path} differ\n"
    before_lines = [] if before_text is None else before_text.splitlines(keepends=True)
    after_lines = [] if after_text is None else after_text.splitlines(keepends=True)
    lines = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile="/dev/null" if before is None else f"a/{path}",
        tofile="/dev/null" if after is None else f"b/{path}",
        n=3,
    )
    return "".join(
        line if line.endswith(("\n", "\r")) else f"{line}\n" for line in lines
    )


def _version_store_lock(repository_path: Path) -> threading.RLock:
    """按规范化仓库路径复用 Dulwich 进程级访问锁。"""

    lock_key = os.path.normcase(str(repository_path.resolve(strict=False)))
    with _LOCKS_GUARD:
        lock = _VERSION_STORE_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.RLock()
            _VERSION_STORE_LOCKS[lock_key] = lock
        return lock
