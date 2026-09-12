# -*- coding: utf-8 -*-
"""工作区文件变更与历史记录使用的领域对象。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class ChangeStatus(StrEnum):
    """一次工作区变更当前所处的状态。"""

    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True, slots=True)
class FileChange:
    """单个文件在一次操作前后的版本信息。"""

    # 使用工作区相对路径，避免把用户本机绝对路径暴露给模型。
    path: str

    # 文件在操作前后是否存在，可用于区分创建、修改和删除。
    before_exists: bool
    after_exists: bool

    # Git Blob 对象编号，用于从私有历史仓库恢复原始字节。
    before_revision: str | None
    after_revision: str | None

    # SHA-256 用作乐观锁，回滚前据此识别用户的后续修改。
    before_sha256: str | None
    after_sha256: str | None

    # 提供给模型审查的统一 Diff；历史恢复不依赖该文本。
    diff: str

    @property
    def action(self) -> str:
        """返回便于模型理解的文件操作类型。"""

        if not self.before_exists and self.after_exists:
            return "created"
        if self.before_exists and not self.after_exists:
            return "deleted"
        return "modified"

    def to_dict(self) -> dict[str, Any]:
        """转换为可以直接序列化为 JSON 的字典。"""

        value = asdict(self)
        value["action"] = self.action
        return value


@dataclass(frozen=True, slots=True)
class FileMutationResult:
    """写文件工具返回给模型的统一变更回执。"""

    # 操作编号是查询详情和执行精确回滚的稳定入口。
    operation_id: str | None

    # 产生本次记录的工具名称，例如 write_file 或 replace_text。
    tool_name: str

    # 当前操作状态；普通成功操作通常为 applied。
    status: ChangeStatus

    # 一次操作可以包含多个文件，为后续 apply_patch 预留扩展空间。
    files: tuple[FileChange, ...]

    # 自动验证由沙箱层执行；文件修改完成时默认尚未运行验证命令。
    validation_status: str = "not_run"

    # 当前内置写工具只能触达声明的目标，因此默认不存在计划外文件。
    unexpected_files: tuple[str, ...] = ()

    # 原操作被哪次回滚撤销；普通操作和回滚操作自身通常为空。
    reverted_by_operation_id: str | None = None

    # pending 恢复或失败诊断使用的简短错误信息。
    error_message: str | None = None

    @property
    def changed_files(self) -> tuple[str, ...]:
        """返回本次实际发生变化的全部相对文件路径。"""

        return tuple(change.path for change in self.files)

    @property
    def diff(self) -> str:
        """合并本次操作内全部文件的 Diff。"""

        return "\n".join(change.diff for change in self.files if change.diff)

    def to_dict(self) -> dict[str, Any]:
        """转换为适合工具执行器回填给模型的字典。"""

        return {
            "operation_id": self.operation_id,
            "tool_name": self.tool_name,
            "status": self.status.value,
            "changed_files": list(self.changed_files),
            "files": [change.to_dict() for change in self.files],
            "diff": self.diff,
            "validation_status": self.validation_status,
            "unexpected_files": list(self.unexpected_files),
            "reverted_by_operation_id": self.reverted_by_operation_id,
            "error_message": self.error_message,
        }
