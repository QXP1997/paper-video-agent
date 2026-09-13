# -*- coding: utf-8 -*-
"""对 UTF-8 文本文件执行精确片段替换的内置工具。"""

from __future__ import annotations

from pydantic import Field

from qharness.tools.base import Tool, ToolParameters, current_operation_id
from qharness.workspace import WorkspaceMutationService


class ReplaceTextParameters(ToolParameters):
    """精确文本替换工具参数。"""

    path: str = Field(description="工作区内已经存在的 UTF-8 文本文件。")
    old_text: str = Field(
        min_length=1,
        description="需要被替换的完整原始文本，必须精确匹配。",
    )
    new_text: str = Field(description="用于替换 old_text 的新文本。")
    expected_replacements: int = Field(
        default=1,
        ge=1,
        le=10000,
        description=(
            "old_text 预期出现次数；实际次数不一致时拒绝修改，默认必须唯一。"
        ),
    )
    expected_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="可选的当前文件 SHA-256，用于防止覆盖较新的文件版本。",
    )


def create_replace_text_tool(service: WorkspaceMutationService) -> Tool:
    """创建绑定到当前 Run 变更服务的精确文本替换工具。"""

    def replace_text(
        path: str,
        old_text: str,
        new_text: str,
        expected_replacements: int = 1,
        expected_sha256: str | None = None,
    ) -> dict[str, object]:
        """执行精确替换，并返回模型可以直接审查的变化回执。"""

        return service.replace_text(
            path,
            old_text,
            new_text,
            expected_replacements=expected_replacements,
            expected_sha256=expected_sha256,
            operation_id=current_operation_id(),
        ).to_dict()

    return Tool(
        name="replace_text",
        description=(
            "在一个 UTF-8 文本文件中精确替换指定片段。实际匹配次数必须与"
            "预期一致；匹配数量冲突时，根据错误提示选择设置实际次数以全部"
            "替换，或扩大 old_text 上下文以只修改一处。成功后返回 Diff、"
            "版本编号和可回滚 operation_id。"
        ),
        parameters=ReplaceTextParameters,
        handler=replace_text,
    )
