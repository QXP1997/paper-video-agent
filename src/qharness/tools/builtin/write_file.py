# -*- coding: utf-8 -*-
"""创建或完整覆盖 UTF-8 文本文件的内置工具。"""

from __future__ import annotations

from pydantic import Field

from qharness.tools.base import Tool, ToolParameters, ToolEffect, current_operation_id
from qharness.workspace import WorkspaceMutationService


class WriteFileParameters(ToolParameters):
    """写文件工具参数。"""

    path: str = Field(description="工作区相对文件路径；父目录必须已经存在。")
    content: str = Field(description="需要完整写入文件的 UTF-8 文本。")
    overwrite: bool = Field(
        default=False,
        description="目标已经存在时是否允许完整覆盖；默认拒绝覆盖。",
    )
    expected_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "可选的当前文件 SHA-256；覆盖前不一致则拒绝，"
            "用于防止基于过期内容修改。"
        ),
    )


def create_write_file_tool(service: WorkspaceMutationService) -> Tool:
    """创建绑定到当前 Run 变更服务的写文件工具。"""

    def write_file(
        path: str,
        content: str,
        overwrite: bool = False,
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        """通过统一变更服务写入文件，并返回 Diff 和回滚编号。"""

        return service.write_text(
            path,
            content,
            overwrite=overwrite,
            expected_sha256=expected_sha256,
            operation_id=current_operation_id(),
        ).to_dict()

    return Tool(
        name="write_file",
        description=(
            "创建或完整覆盖工作区内的 UTF-8 文本文件。成功后返回结构化 "
            "Diff、前后版本编号和 operation_id，可用于审查或精确回滚。"
        ),
        parameters=WriteFileParameters,
        handler=write_file,
        effect=ToolEffect.WORKSPACE_WRITE,
    )
