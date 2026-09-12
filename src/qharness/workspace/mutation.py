# -*- coding: utf-8 -*-
"""带快照、Diff 和安全回滚的工作区文件修改服务。"""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from qharness.exception import (
    WorkspaceConflictError,
    WorkspaceMutationError,
)
from qharness.workspace.context import WorkspaceContext
from qharness.workspace.history import WorkspaceHistoryRepository
from qharness.workspace.models import ChangeStatus, FileChange, FileMutationResult
from qharness.workspace.version import FileVersionStore


_CTX_DIFF_LINES = 3
_LOCKS_GUARD = threading.Lock()
_MUTATION_LOCKS: dict[str, threading.RLock] = {}


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    """文件某个时刻的原始内容及校验信息。"""

    # 文件在该时刻是否存在。
    exists: bool

    # 文件的原始字节；不存在时为 None。
    content: bytes | None

    # Dulwich 私有对象库中的 Git Blob 编号。
    revision: str | None

    # 用于冲突检查和内容完整性校验的 SHA-256。
    sha256: str | None


class WorkspaceMutationService:
    """所有内置写文件工具共享的安全变更入口。

    本服务保证写入采用同目录临时文件加原子替换；每次修改先持久化修改前后
    Blob，再建立 pending 记录，最后更新状态。回滚使用 SHA-256 乐观校验，
    不会覆盖操作完成后由用户或其他 Run 产生的新修改。
    """

    def __init__(
        self,
        workspace: WorkspaceContext,
        history_repository: WorkspaceHistoryRepository,
        version_store: FileVersionStore,
        *,
        run_id: str,
        max_file_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        """绑定当前 Run、工作区和私有历史存储。"""

        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int):
            raise ValueError("max_file_bytes 必须是整数。")
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes 必须大于 0。")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 必须是非空字符串。")
        for local_root in (
            history_repository.local_root,
            version_store.local_root,
        ):
            if local_root is not None and local_root.is_relative_to(workspace.root):
                raise ValueError(
                    "私有历史目录不能放在 Agent 可访问的工作区内部。"
                )

        # 当前 Run 唯一可触达的工作区路径边界。
        self.workspace = workspace

        # 保存操作状态、租户关联和事务语义的数据库仓储。
        self.history_repository = history_repository

        # 保存修改前后文件原始字节的可替换版本存储。
        self.version_store = version_store

        # 所有新操作关联到的 Agent Run 标识。
        self.run_id = run_id

        # 单文件读取、写入和历史快照允许的最大字节数。
        self.max_file_bytes = max_file_bytes
        self.history_repository.bind_workspace_root(self.workspace.root)

        # 修改锁属于业务服务，不依赖 SQLite 或 Dulwich 具体实现。
        self._mutation_lock = _workspace_mutation_lock(self.workspace.root)

    def write_text(
        self,
        path: str,
        content: str,
        *,
        overwrite: bool = False,
        expected_sha256: str | None = None,
    ) -> FileMutationResult:
        """创建或完整覆盖一个 UTF-8 文件，并返回可回滚变更回执。"""

        if not isinstance(content, str):
            raise WorkspaceMutationError("content 必须是字符串。")
        encoded_content = content.encode("utf-8")
        self._validate_content_size(encoded_content)

        with self._mutation_lock:
            file_path, relative_path = self._resolve_writable_file(path)
            before = self._capture_snapshot(file_path)
            if before.exists and not overwrite:
                raise WorkspaceMutationError(
                    f"文件已经存在，如需覆盖请显式设置 overwrite=true：{relative_path}"
                )
            self._validate_expected_hash(
                relative_path,
                before.sha256,
                expected_sha256,
            )
            after = self._snapshot_content(encoded_content)
            return self._apply_change(
                tool_name="write_file",
                file_path=file_path,
                relative_path=relative_path,
                before=before,
                after=after,
            )

    def replace_text(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        expected_replacements: int = 1,
        expected_sha256: str | None = None,
    ) -> FileMutationResult:
        """按精确出现次数替换文本，避免模糊定位造成意外修改。"""

        if not old_text:
            raise WorkspaceMutationError("old_text 不能为空。")
        if isinstance(expected_replacements, bool) or not isinstance(
            expected_replacements,
            int,
        ):
            raise WorkspaceMutationError("expected_replacements 必须是整数。")
        if expected_replacements <= 0:
            raise WorkspaceMutationError("expected_replacements 必须大于 0。")

        with self._mutation_lock:
            file_path = self.workspace.resolve_file(path)
            relative_path = self.workspace.relative_path(file_path)
            before = self._capture_snapshot(file_path)
            self._validate_expected_hash(
                relative_path,
                before.sha256,
                expected_sha256,
            )
            original_text = self._decode_utf8(before, relative_path)
            actual_replacements = original_text.count(old_text)
            if actual_replacements != expected_replacements:
                raise WorkspaceConflictError(
                    f"文件 {relative_path} 中 old_text 实际出现 "
                    f"{actual_replacements} 次，预期 {expected_replacements} 次；"
                    "本次没有修改文件。若要替换全部匹配，请重新调用并设置 "
                    f"expected_replacements={actual_replacements}；"
                    "若只想修改其中一处，请在 old_text 中加入更多相邻内容，"
                    "使它只匹配目标位置。"
                )
            encoded_content = original_text.replace(old_text, new_text).encode(
                "utf-8"
            )
            self._validate_content_size(encoded_content)
            after = self._snapshot_content(encoded_content)
            return self._apply_change(
                tool_name="replace_text",
                file_path=file_path,
                relative_path=relative_path,
                before=before,
                after=after,
            )

    def rollback(self, operation_id: str) -> FileMutationResult:
        """恢复一次已应用操作，并拒绝覆盖操作之后出现的新文件内容。"""

        with self._mutation_lock:
            operation = self.history_repository.get_operation(operation_id)
            if operation.status is not ChangeStatus.APPLIED:
                raise WorkspaceMutationError(
                    f"只有 applied 状态可以回滚；操作 {operation_id} 当前为 "
                    f"{operation.status.value}。"
                )
            if len(operation.files) != 1:
                raise WorkspaceMutationError(
                    "当前回滚实现只支持单文件操作；多文件原子回滚将在 "
                    "apply_patch 阶段接入。"
                )

            original_change = operation.files[0]
            file_path = self.workspace.resolve_path(
                original_change.path,
                must_exist=False,
            )
            current = self._capture_snapshot(file_path)
            if (
                current.exists != original_change.after_exists
                or current.sha256 != original_change.after_sha256
            ):
                raise WorkspaceConflictError(
                    f"文件 {original_change.path} 在操作完成后又发生了变化，"
                    "为避免覆盖用户或其他 Run 的修改，已拒绝回滚。"
                )

            restored = self._snapshot_from_history(
                exists=original_change.before_exists,
                revision=original_change.before_revision,
                sha256=original_change.before_sha256,
            )
            rollback_change = self._build_file_change(
                original_change.path,
                current,
                restored,
            )
            rollback_operation_id = self.history_repository.begin_operation(
                run_id=self.run_id,
                tool_name="rollback_file_change",
                files=(rollback_change,),
            )
            try:
                self._write_snapshot(file_path, restored, previous=current)
            except Exception as error:
                self._record_failure(rollback_operation_id, error)
                raise
            # 文件已经恢复后若 SQLite 暂时不可写，保留 pending 供恢复流程
            # 对照前后摘要确认，不能错误标记为 failed。
            self.history_repository.complete_rollback(
                rollback_operation_id,
                operation_id,
            )

            return FileMutationResult(
                operation_id=rollback_operation_id,
                tool_name="rollback_file_change",
                status=ChangeStatus.APPLIED,
                files=(rollback_change,),
            )

    def inspect(self, operation_id: str) -> FileMutationResult:
        """读取某次变更的 Diff 和版本信息，不访问用户文件内容。"""

        operation = self.history_repository.get_operation(operation_id)
        return FileMutationResult(
            operation_id=operation.operation_id,
            tool_name=operation.tool_name,
            status=operation.status,
            files=operation.files,
            reverted_by_operation_id=operation.reverted_by_operation_id,
            error_message=operation.error_message,
        )

    def _apply_change(
        self,
        *,
        tool_name: str,
        file_path: Path,
        relative_path: str,
        before: _FileSnapshot,
        after: _FileSnapshot,
    ) -> FileMutationResult:
        """持久化版本、写入文件并确认操作状态。"""

        if before.exists == after.exists and before.sha256 == after.sha256:
            return FileMutationResult(
                operation_id=None,
                tool_name=tool_name,
                status=ChangeStatus.APPLIED,
                files=(),
            )

        change = self._build_file_change(relative_path, before, after)
        operation_id = self.history_repository.begin_operation(
            run_id=self.run_id,
            tool_name=tool_name,
            files=(change,),
        )
        try:
            self._write_snapshot(file_path, after, previous=before)
        except Exception as error:
            self._record_failure(operation_id, error)
            raise
        # 文件已经原子落盘后再确认数据库状态。确认失败时保持 pending，
        # 后续可根据当前摘要判断写入究竟是否完成。
        self.history_repository.complete_operation(operation_id)

        return FileMutationResult(
            operation_id=operation_id,
            tool_name=tool_name,
            status=ChangeStatus.APPLIED,
            files=(change,),
        )

    def _capture_snapshot(self, file_path: Path) -> _FileSnapshot:
        """读取当前普通文件，并把原始字节保存到历史对象库。"""

        if not file_path.exists():
            return _FileSnapshot(False, None, None, None)
        if not file_path.is_file():
            raise WorkspaceMutationError(f"目标路径不是普通文件：{file_path}")
        try:
            content = file_path.read_bytes()
        except OSError as error:
            raise WorkspaceMutationError(f"无法读取目标文件：{file_path}") from error
        self._validate_content_size(content)
        return self._snapshot_content(content)

    def _snapshot_content(self, content: bytes) -> _FileSnapshot:
        """为给定字节计算校验值并保存 Git Blob。"""

        revision = self.version_store.save(content)
        sha256 = hashlib.sha256(content).hexdigest()
        return _FileSnapshot(True, content, revision, sha256)

    def _snapshot_from_history(
        self,
        *,
        exists: bool,
        revision: str | None,
        sha256: str | None,
    ) -> _FileSnapshot:
        """从私有对象仓库还原历史快照，并验证内容完整性。"""

        if not exists:
            return _FileSnapshot(False, None, None, None)
        if revision is None or sha256 is None:
            raise WorkspaceMutationError("历史记录缺少文件版本，无法回滚。")
        content = self.version_store.load(revision)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != sha256:
            raise WorkspaceMutationError("历史文件摘要校验失败，拒绝回滚。")
        return _FileSnapshot(True, content, revision, sha256)

    def _build_file_change(
        self,
        relative_path: str,
        before: _FileSnapshot,
        after: _FileSnapshot,
    ) -> FileChange:
        """构造持久化与模型审查共用的单文件变化对象。"""

        before_text = self._decode_utf8(before, relative_path)
        after_text = self._decode_utf8(after, relative_path)
        return FileChange(
            path=relative_path,
            before_exists=before.exists,
            after_exists=after.exists,
            before_revision=before.revision,
            after_revision=after.revision,
            before_sha256=before.sha256,
            after_sha256=after.sha256,
            diff=_unified_diff(
                relative_path,
                before_text if before.exists else None,
                after_text if after.exists else None,
            ),
        )

    def _resolve_writable_file(self, path: str) -> tuple[Path, str]:
        """解析待创建路径，并要求父目录已经存在且位于工作区。"""

        file_path = self.workspace.resolve_path(path, must_exist=False)
        self.workspace.resolve_directory(file_path.parent)
        if file_path.exists() and not file_path.is_file():
            raise WorkspaceMutationError(f"目标路径不是普通文件：{path}")
        relative_path = self.workspace.relative_path(file_path, must_exist=False)
        return file_path, relative_path

    def _write_snapshot(
        self,
        file_path: Path,
        snapshot: _FileSnapshot,
        *,
        previous: _FileSnapshot,
    ) -> None:
        """使用原子替换写入快照，或在回滚创建操作时安全删除文件。"""

        if not snapshot.exists:
            try:
                file_path.unlink()
            except FileNotFoundError:
                return
            except OSError as error:
                raise WorkspaceMutationError(f"无法删除文件：{file_path}") from error
            return

        if snapshot.content is None:
            raise WorkspaceMutationError("待写入快照缺少文件内容。")

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{file_path.name}.",
                suffix=".qharness.tmp",
                dir=file_path.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(snapshot.content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            if previous.exists:
                current_mode = stat.S_IMODE(file_path.stat().st_mode)
                os.chmod(temporary_path, current_mode)
            os.replace(temporary_path, file_path)
            temporary_path = None
        except OSError as error:
            raise WorkspaceMutationError(f"无法写入文件：{file_path}") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _validate_content_size(self, content: bytes) -> None:
        """限制单文件历史和写入大小，避免异常参数耗尽内存或磁盘。"""

        if len(content) > self.max_file_bytes:
            raise WorkspaceMutationError(
                f"文件大小超过上限 {self.max_file_bytes} 字节。"
            )

    @staticmethod
    def _validate_expected_hash(
        path: str,
        actual_sha256: str | None,
        expected_sha256: str | None,
    ) -> None:
        """使用可选摘要阻止模型基于过期文件内容继续修改。"""

        if expected_sha256 is not None and expected_sha256 != actual_sha256:
            raise WorkspaceConflictError(
                f"文件 {path} 当前摘要与 expected_sha256 不一致，"
                "请重新读取后再修改。"
            )

    @staticmethod
    def _decode_utf8(snapshot: _FileSnapshot, path: str) -> str:
        """把存在的历史字节严格解码为 UTF-8 文本。"""

        if not snapshot.exists:
            return ""
        if snapshot.content is None:
            raise WorkspaceMutationError(f"文件快照缺少内容：{path}")
        if b"\x00" in snapshot.content[:8192]:
            raise WorkspaceMutationError(f"拒绝修改疑似二进制文件：{path}")
        try:
            # 使用 utf-8 而不是 utf-8-sig，使已有 BOM 也作为原始内容保留，
            # 避免一次普通文本替换顺带改变文件编码形式。
            return snapshot.content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkspaceMutationError(
                f"文件不是有效的 UTF-8 文本，拒绝修改：{path}"
            ) from error

    def _record_failure(self, operation_id: str, error: Exception) -> None:
        """尽力记录失败；原始文件异常始终优先返回给调用方。"""

        try:
            self.history_repository.fail_operation(operation_id, str(error))
        except Exception:
            # 历史存储本身可能正是失败来源，不能用二次异常覆盖根因。
            pass


def _unified_diff(path: str, before: str | None, after: str | None) -> str:
    """生成适合模型审查的标准 unified diff。"""

    before_lines = [] if before is None else before.splitlines(keepends=True)
    after_lines = [] if after is None else after.splitlines(keepends=True)
    from_file = "/dev/null" if before is None else f"a/{path}"
    to_file = "/dev/null" if after is None else f"b/{path}"
    diff_lines = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=from_file,
        tofile=to_file,
        n=_CTX_DIFF_LINES,
    )
    # difflib 会让“文件末尾没有换行”的数据行也不带换行，直接 join 会把
    # 相邻的删除行和新增行粘在一起。Diff 仅供审查，补换行不影响 Blob 恢复。
    return "".join(
        line if line.endswith(("\n", "\r")) else f"{line}\n"
        for line in diff_lines
    )


def _workspace_mutation_lock(workspace_root: Path) -> threading.RLock:
    """按规范化工作区根目录复用进程内修改锁。"""

    lock_key = os.path.normcase(str(workspace_root.resolve(strict=True)))
    with _LOCKS_GUARD:
        lock = _MUTATION_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.RLock()
            _MUTATION_LOCKS[lock_key] = lock
        return lock
