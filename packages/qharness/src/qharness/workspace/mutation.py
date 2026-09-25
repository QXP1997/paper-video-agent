# -*- coding: utf-8 -*-
"""带私有 Git Commit、Diff、补丁和安全回滚的工作区修改服务。"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from qharness.exception import WorkspaceConflictError, WorkspaceMutationError
from qharness.workspace.context import WorkspaceContext
from qharness.workspace.history import WorkspaceHistoryRepository
from qharness.workspace.models import ChangeStatus, FileChange, FileMutationResult
from qharness.workspace.patch import parse_patch
from qharness.workspace.version import (
    FileHistoryEntry,
    FileVersionStore,
    WorkspaceStatus,
)


_LOCKS_GUARD = threading.Lock()
_MUTATION_LOCKS: dict[str, threading.RLock] = {}
_ACTIVE_EXTERNAL_OPERATIONS: dict[str, str] = {}


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    """文件某个时刻的原始内容和本地权限。"""

    # 文件在该时刻是否存在。
    exists: bool

    # 文件原始字节；不存在时为 None。
    content: bytes | None

    # 用于乐观冲突检查的 SHA-256。
    sha256: str | None

    # 普通权限位；Windows 上通常为 0o666，创建文件默认使用 0o644。
    mode: int | None


@dataclass(frozen=True, slots=True)
class _PlannedFileChange:
    """一次逻辑操作内单个文件的落盘计划。"""

    path: str
    file_path: Path
    before: _FileSnapshot
    after: _FileSnapshot


@dataclass(frozen=True, slots=True)
class ExternalMutationToken:
    """标识一个正在沙箱中运行、可能修改工作区的外部命令。"""

    # 同时作为命令产生文件变更时的 operation_id。
    operation_id: str

    # 记录操作来源的模型工具名称，当前通常为 run_command。
    tool_name: str

    # 命令启动前已经保存好的私有历史 HEAD。
    base_commit_id: str


class WorkspaceMutationService:
    """所有内置写文件工具共享的生产级修改入口。

    服务会先把用户在工具之外产生的修改保存成外部检查点，再执行写入。每次
    逻辑操作只产生一个数据库 operation 和一个 Dulwich Commit；多文件写入
    中途失败时会补偿恢复已经落盘的文件。
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
        """绑定当前 Run、工作区、操作台账和私有 Git 仓库。"""

        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int):
            raise ValueError("max_file_bytes 必须是整数。")
        if max_file_bytes <= 0:
            raise ValueError("max_file_bytes 必须大于 0。")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id 必须是非空字符串。")
        for local_root in (history_repository.local_root, version_store.local_root):
            if local_root is not None and local_root.is_relative_to(workspace.root):
                raise ValueError("私有历史目录不能放在 Agent 可访问的工作区内部。")

        self.workspace = workspace
        self.history_repository = history_repository
        self.version_store = version_store
        self.run_id = run_id
        self.max_file_bytes = max_file_bytes
        self.history_repository.bind_workspace_root(self.workspace.root)
        self._mutation_key = _workspace_mutation_key(self.workspace.root)
        self._mutation_lock = _workspace_mutation_lock(self.workspace.root)

    def write_text(
        self,
        path: str,
        content: str,
        *,
        overwrite: bool = False,
        expected_sha256: str | None = None,
        operation_id: str | None = None,
    ) -> FileMutationResult:
        """创建或完整覆盖一个 UTF-8 文件，并生成独立 Commit。"""

        if not isinstance(content, str):
            raise WorkspaceMutationError("content 必须是字符串。")
        encoded_content = content.encode("utf-8")
        self._validate_content_size(encoded_content)

        with self._mutation_lock:
            self._ensure_no_external_operation()
            base_commit_id = self._checkpoint_external_changes()
            file_path, relative_path = self._resolve_writable_file(path)
            self._validate_trackable_path(relative_path)
            before = self._capture_snapshot(file_path)
            if before.exists and not overwrite:
                raise WorkspaceMutationError(
                    f"文件已经存在，如需覆盖请显式设置 overwrite=true：{relative_path}"
                )
            self._validate_expected_hash(relative_path, before.sha256, expected_sha256)
            after = self._snapshot_content(
                encoded_content,
                mode=before.mode if before.exists else None,
            )
            return self._apply_plans(
                tool_name="write_file",
                operation_id=operation_id,
                plans=(_PlannedFileChange(relative_path, file_path, before, after),),
                base_commit_id=base_commit_id,
            )

    def replace_text(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        expected_replacements: int = 1,
        expected_sha256: str | None = None,
        operation_id: str | None = None,
    ) -> FileMutationResult:
        """按精确出现次数替换文本，避免模糊定位造成意外修改。"""

        if not old_text:
            raise WorkspaceMutationError("old_text 不能为空。")
        if isinstance(expected_replacements, bool) or not isinstance(
            expected_replacements, int
        ):
            raise WorkspaceMutationError("expected_replacements 必须是整数。")
        if expected_replacements <= 0:
            raise WorkspaceMutationError("expected_replacements 必须大于 0。")

        with self._mutation_lock:
            self._ensure_no_external_operation()
            base_commit_id = self._checkpoint_external_changes()
            file_path = self.workspace.resolve_file(path)
            relative_path = self.workspace.relative_path(file_path)
            self._validate_trackable_path(relative_path)
            before = self._capture_snapshot(file_path)
            self._validate_expected_hash(relative_path, before.sha256, expected_sha256)
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
            encoded_content = original_text.replace(old_text, new_text).encode("utf-8")
            self._validate_content_size(encoded_content)
            after = self._snapshot_content(encoded_content, mode=before.mode)
            return self._apply_plans(
                tool_name="replace_text",
                operation_id=operation_id,
                plans=(_PlannedFileChange(relative_path, file_path, before, after),),
                base_commit_id=base_commit_id,
            )

    def apply_patch(self, patch_text: str, *, operation_id: str | None = None) -> FileMutationResult:
        """一次性应用结构化多文件补丁，并生成一个原子业务操作。"""

        patch_files = parse_patch(patch_text)
        with self._mutation_lock:
            self._ensure_no_external_operation()
            base_commit_id = self._checkpoint_external_changes()
            plans: list[_PlannedFileChange] = []
            resolved_paths: set[str] = set()
            for patch_file in patch_files:
                file_path = self.workspace.resolve_path(
                    patch_file.path,
                    must_exist=False,
                )
                relative_path = self.workspace.relative_path(
                    file_path,
                    must_exist=False,
                )
                self._validate_trackable_path(relative_path)
                if relative_path in resolved_paths:
                    raise WorkspaceMutationError(
                        f"补丁中的多个路径解析到了同一个文件：{relative_path}"
                    )
                resolved_paths.add(relative_path)
                if patch_file.action.value == "add":
                    self.workspace.resolve_directory(file_path.parent)
                before = self._capture_snapshot(file_path)
                original = (
                    self._decode_utf8(before, relative_path) if before.exists else None
                )
                target = patch_file.apply(original)
                if target is None:
                    after = _FileSnapshot(False, None, None, None)
                else:
                    encoded = target.encode("utf-8")
                    self._validate_content_size(encoded)
                    after = self._snapshot_content(encoded, mode=before.mode)
                plans.append(
                    _PlannedFileChange(relative_path, file_path, before, after)
                )
            return self._apply_plans(
                tool_name="apply_patch",
                operation_id=operation_id,
                plans=tuple(plans),
                base_commit_id=base_commit_id,
            )

    def inspect(self, operation_id: str) -> FileMutationResult:
        """根据数据库 Commit 关联实时计算操作 Diff。"""

        operation = self.history_repository.get_operation(operation_id)
        files: tuple[FileChange, ...] = ()
        error_message = operation.error_message
        if operation.commit_id is not None:
            files = self.version_store.diff_commits(
                operation.base_commit_id,
                operation.commit_id,
                paths=operation.paths,
            )
        elif operation.origin == "legacy" and error_message is None:
            error_message = (
                "该操作来自旧版 Blob-only 历史，没有 Tree/Commit，"
                "只能查看操作摘要，不能动态计算 Diff 或安全回滚。"
            )
        return FileMutationResult(
            operation_id=operation.operation_id,
            tool_name=operation.tool_name,
            status=operation.status,
            files=files,
            base_commit_id=operation.base_commit_id,
            commit_id=operation.commit_id,
            origin=operation.origin,
            reverted_by_operation_id=operation.reverted_by_operation_id,
            error_message=error_message,
        )

    def file_history(self, path: str, *, limit: int = 20) -> tuple[FileHistoryEntry, ...]:
        """直接从 Dulwich Commit 链读取指定文件的历史。"""

        file_path = self.workspace.resolve_path(path, must_exist=False)
        relative_path = self.workspace.relative_path(file_path, must_exist=False)
        with self._mutation_lock:
            return self.version_store.file_history(relative_path, limit=limit)

    def workspace_status(self) -> WorkspaceStatus:
        """读取当前磁盘相对于私有 HEAD 的未提交变化，不创建检查点。"""

        with self._mutation_lock:
            return self.version_store.workspace_status(self.workspace.root)

    def begin_external_operation(self, tool_name: str, *, operation_id: str | None = None) -> ExternalMutationToken:
        """保存命令执行前基线，并阻止其他 Harness 写操作并发进入。"""

        with self._mutation_lock:
            self._ensure_no_external_operation()
            base_commit_id = self._checkpoint_external_changes()
            token = ExternalMutationToken(
                operation_id=operation_id or uuid.uuid4().hex,
                tool_name=tool_name,
                base_commit_id=base_commit_id,
            )
            _ACTIVE_EXTERNAL_OPERATIONS[self._mutation_key] = token.operation_id
            return token

    def finish_external_operation(
        self,
        token: ExternalMutationToken,
        *,
        command_succeeded: bool,
    ) -> FileMutationResult:
        """把沙箱命令落盘的全部可跟踪变化保存为一个 Commit。"""

        with self._mutation_lock:
            active_id = _ACTIVE_EXTERNAL_OPERATIONS.get(self._mutation_key)
            if active_id != token.operation_id:
                raise WorkspaceConflictError("外部命令操作令牌已经失效。")
            try:
                current_head = self.version_store.head_commit_id()
                if current_head != token.base_commit_id:
                    raise WorkspaceConflictError(
                        "命令执行期间私有历史 HEAD 被其他操作推进，"
                        "无法安全归属本次文件变化。"
                    )
                commit_result = self.version_store.checkpoint_workspace(
                    self.workspace.root,
                    message=(
                        f"QHarness {token.tool_name} "
                        f"operation={token.operation_id}"
                    ),
                )
                validation_status = (
                    "passed" if command_succeeded else "failed"
                )
                if not commit_result.created or not commit_result.files:
                    return FileMutationResult(
                        operation_id=None,
                        tool_name=token.tool_name,
                        status=ChangeStatus.APPLIED,
                        files=(),
                        base_commit_id=token.base_commit_id,
                        commit_id=token.base_commit_id,
                        validation_status=validation_status,
                    )

                operation_id = self.history_repository.begin_operation(
                    operation_id=token.operation_id,
                    run_id=self.run_id,
                    tool_name=token.tool_name,
                    origin="agent",
                    paths=tuple(change.path for change in commit_result.files),
                    base_commit_id=token.base_commit_id,
                )
                self.history_repository.complete_operation(
                    operation_id,
                    commit_result.commit_id,
                )
                return FileMutationResult(
                    operation_id=operation_id,
                    tool_name=token.tool_name,
                    status=ChangeStatus.APPLIED,
                    files=commit_result.files,
                    base_commit_id=token.base_commit_id,
                    commit_id=commit_result.commit_id,
                    validation_status=validation_status,
                )
            finally:
                if (
                    _ACTIVE_EXTERNAL_OPERATIONS.get(self._mutation_key)
                    == token.operation_id
                ):
                    del _ACTIVE_EXTERNAL_OPERATIONS[self._mutation_key]

    def rollback(self, operation_id: str, *, rollback_operation_id: str | None = None) -> FileMutationResult:
        """反向应用某次 Commit 的文件变化，保留之后的无关修改。"""

        with self._mutation_lock:
            self._ensure_no_external_operation()
            current_head = self._checkpoint_external_changes()
            operation = self.history_repository.get_operation(operation_id)
            if operation.status is not ChangeStatus.APPLIED:
                raise WorkspaceMutationError(
                    f"只有 applied 状态可以回滚；操作 {operation_id} 当前为 "
                    f"{operation.status.value}。"
                )
            if operation.commit_id is None:
                raise WorkspaceMutationError("历史操作缺少 Commit，无法执行安全回滚。")

            original_changes = self.version_store.diff_commits(
                operation.base_commit_id,
                operation.commit_id,
                paths=operation.paths,
            )
            plans: list[_PlannedFileChange] = []
            for change in original_changes:
                file_path = self.workspace.resolve_path(change.path, must_exist=False)
                self._validate_trackable_path(change.path)
                current = self._capture_snapshot(file_path)
                if (
                    current.exists != change.after_exists
                    or current.sha256 != change.after_sha256
                ):
                    raise WorkspaceConflictError(
                        f"文件 {change.path} 在操作完成后又发生了变化，"
                        "为避免覆盖用户或其他 Run 的修改，已拒绝回滚。"
                    )
                restored_state = (
                    None
                    if operation.base_commit_id is None
                    else self.version_store.read_file_state(
                        operation.base_commit_id,
                        change.path,
                    )
                )
                if restored_state is None:
                    restored = _FileSnapshot(False, None, None, None)
                else:
                    restored_content, restored_mode = restored_state
                    restored = self._snapshot_content(
                        restored_content,
                        mode=restored_mode,
                    )
                plans.append(_PlannedFileChange(change.path, file_path, current, restored))

            return self._apply_plans(
                tool_name="rollback_file_change",
                plans=tuple(plans),
                base_commit_id=current_head,
                origin="rollback",
                original_operation_id=operation_id,
                operation_id=rollback_operation_id,
            )

    def _checkpoint_external_changes(self) -> str:
        """确保磁盘状态有基线；外部变化单独生成 Commit 和操作台账。"""

        result = self.version_store.checkpoint_workspace(
            self.workspace.root,
            message="QHarness workspace baseline or external checkpoint",
        )
        if result.created and result.parent_commit_id is not None and result.files:
            operation_id = self.history_repository.begin_operation(
                run_id=self.run_id,
                tool_name="external_checkpoint",
                origin="external",
                paths=tuple(change.path for change in result.files),
                base_commit_id=result.parent_commit_id,
            )
            self.history_repository.complete_operation(operation_id, result.commit_id)
        return result.commit_id

    def _ensure_no_external_operation(self) -> None:
        """拒绝与正在执行的沙箱命令交错修改同一个工作区。"""

        if self._mutation_key in _ACTIVE_EXTERNAL_OPERATIONS:
            raise WorkspaceConflictError(
                "当前工作区有沙箱命令正在执行，请等待命令结束后再修改文件。"
            )

    def _apply_plans(
        self,
        *,
        tool_name: str,
        plans: tuple[_PlannedFileChange, ...],
        base_commit_id: str,
        origin: str = "agent",
        original_operation_id: str | None = None,
        operation_id: str | None = None,
    ) -> FileMutationResult:
        """补偿式落盘全部文件，创建 Commit，最后确认数据库台账。"""

        changed = tuple(
            plan
            for plan in plans
            if plan.before.exists != plan.after.exists
            or plan.before.sha256 != plan.after.sha256
        )
        if not changed:
            return FileMutationResult(
                operation_id=None,
                tool_name=tool_name,
                status=ChangeStatus.APPLIED,
                files=(),
                base_commit_id=base_commit_id,
                commit_id=base_commit_id,
                origin=origin,
            )
        operation_id = self.history_repository.begin_operation(
            operation_id=operation_id,
            run_id=self.run_id,
            tool_name=tool_name,
            origin=origin,
            paths=tuple(plan.path for plan in changed),
            base_commit_id=base_commit_id,
        )
        written: list[_PlannedFileChange] = []
        try:
            for plan in changed:
                self._write_snapshot(plan.file_path, plan.after)
                written.append(plan)
            commit_result = self.version_store.commit_changes(
                base_commit_id,
                {
                    plan.path: (
                        None
                        if not plan.after.exists
                        else (plan.after.content or b"", plan.after.mode or 0o644)
                    )
                    for plan in changed
                },
                message=f"QHarness {tool_name} operation={operation_id}",
            )
        except Exception as error:
            restore_error = self._restore_written_files(written)
            message = str(error)
            if restore_error is not None:
                message += f"；补偿恢复也失败：{restore_error}"
            self._record_failure(operation_id, RuntimeError(message))
            if restore_error is not None:
                raise WorkspaceMutationError(message) from error
            raise

        if original_operation_id is None:
            self.history_repository.complete_operation(
                operation_id,
                commit_result.commit_id,
            )
        else:
            self.history_repository.complete_rollback(
                operation_id,
                original_operation_id,
                commit_result.commit_id,
            )
        return FileMutationResult(
            operation_id=operation_id,
            tool_name=tool_name,
            status=ChangeStatus.APPLIED,
            files=commit_result.files,
            base_commit_id=base_commit_id,
            commit_id=commit_result.commit_id,
            origin=origin,
        )

    def _restore_written_files(
        self,
        written: list[_PlannedFileChange],
    ) -> Exception | None:
        """逆序补偿已经落盘的文件，并返回首个恢复异常。"""

        first_error: Exception | None = None
        for plan in reversed(written):
            try:
                self._write_snapshot(plan.file_path, plan.before)
            except Exception as error:
                if first_error is None:
                    first_error = error
        return first_error

    def _capture_snapshot(self, file_path: Path) -> _FileSnapshot:
        """读取当前普通文件，并计算校验摘要。"""

        if not file_path.exists():
            return _FileSnapshot(False, None, None, None)
        if not file_path.is_file():
            raise WorkspaceMutationError(f"目标路径不是普通文件：{file_path}")
        try:
            content = file_path.read_bytes()
            mode = stat.S_IMODE(file_path.stat().st_mode)
        except OSError as error:
            raise WorkspaceMutationError(f"无法读取目标文件：{file_path}") from error
        self._validate_content_size(content)
        return self._snapshot_content(content, mode=mode)

    def _snapshot_content(
        self,
        content: bytes,
        *,
        mode: int | None,
    ) -> _FileSnapshot:
        """为给定字节计算校验值并补充默认权限。"""

        return _FileSnapshot(
            True,
            content,
            hashlib.sha256(content).hexdigest(),
            0o644 if mode is None else mode,
        )

    def _resolve_writable_file(self, path: str) -> tuple[Path, str]:
        """解析待创建路径，并要求父目录已存在且位于工作区。"""

        file_path = self.workspace.resolve_path(path, must_exist=False)
        self.workspace.resolve_directory(file_path.parent)
        if file_path.exists() and not file_path.is_file():
            raise WorkspaceMutationError(f"目标路径不是普通文件：{path}")
        relative_path = self.workspace.relative_path(file_path, must_exist=False)
        return file_path, relative_path

    @staticmethod
    def _validate_trackable_path(path: str) -> None:
        """拒绝修改不会进入私有历史的保留元数据目录。"""

        first_part = path.replace("\\", "/").split("/", 1)[0].casefold()
        if first_part in {".git", ".qharness"}:
            raise WorkspaceMutationError(
                f"QHarness 保留目录不能通过文件工具修改：{path}"
            )

    def _write_snapshot(self, file_path: Path, snapshot: _FileSnapshot) -> None:
        """使用同目录临时文件原子替换，或安全删除目标文件。"""

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
            os.chmod(temporary_path, snapshot.mode or 0o644)
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
        """限制单文件修改大小，避免异常参数耗尽内存或磁盘。"""

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
        """使用可选摘要阻止模型基于过期文件继续修改。"""

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
            pass


def _workspace_mutation_lock(workspace_root: Path) -> threading.RLock:
    """按规范化工作区根目录复用进程内修改锁。"""

    lock_key = _workspace_mutation_key(workspace_root)
    with _LOCKS_GUARD:
        lock = _MUTATION_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.RLock()
            _MUTATION_LOCKS[lock_key] = lock
        return lock


def _workspace_mutation_key(workspace_root: Path) -> str:
    """返回同一进程识别工作区并发边界的规范化键。"""

    return os.path.normcase(str(workspace_root.resolve(strict=True)))
