# -*- coding: utf-8 -*-
"""按行读取工作区 UTF-8 文本文件的只读工具。"""

from __future__ import annotations

import io
from typing import Any

from pydantic import Field

from qharness.tools.base import Tool, ToolParameters
from qharness.workspace import WorkspaceContext


_BINARY_SAMPLE_BYTES = 8192


class ReadFileParameters(ToolParameters):
    """读取文件工具的参数。"""

    path: str = Field(description="工作区相对文件路径。")
    start_line: int = Field(
        default=1,
        ge=1,
        description="开始读取的行号，从 1 开始。",
    )
    max_lines: int = Field(
        default=200,
        ge=1,
        le=2000,
        description="本次最多返回的行数。",
    )


def create_read_file_tool(workspace: WorkspaceContext) -> Tool:
    """创建绑定到指定工作区的文件读取工具。"""

    def read_file(
        path: str,
        start_line: int = 1,
        max_lines: int = 200,
    ) -> dict[str, Any]:
        """流式读取 UTF-8 文本文件，并返回带行号的指定范围。"""

        file_path = workspace.resolve_file(path)
        try:
            with file_path.open("rb") as binary_file:
                sample = binary_file.read(_BINARY_SAMPLE_BYTES)
                if b"\x00" in sample:
                    raise RuntimeError(
                        f"文件可能是二进制文件，拒绝读取：{path}"
                    )

                # 重置到文件开头，再由 TextIOWrapper 增量解码 UTF-8；
                # utf-8-sig 会自动去掉文件开头可能存在的 BOM。
                binary_file.seek(0)
                with io.TextIOWrapper(
                    binary_file,
                    encoding="utf-8-sig",
                    errors="strict",
                    newline=None,
                ) as text_file:
                    selected_lines: list[str] = []
                    has_more = False
                    for line_number, line in enumerate(text_file, start=1):
                        if line_number < start_line:
                            continue
                        if len(selected_lines) >= max_lines:
                            # 多读取一行只为确认后面仍有内容，不把它加入本页。
                            has_more = True
                            break
                        selected_lines.append(line.rstrip("\r\n"))
        except UnicodeDecodeError as error:
            raise RuntimeError(f"文件不是有效的 UTF-8 文本：{path}") from error
        except OSError as error:
            raise RuntimeError(f"无法读取工作区文件 {path}：{error}") from error

        numbered_content = "\n".join(
            f"{line_number}: {line}"
            for line_number, line in enumerate(selected_lines, start=start_line)
        )
        end_line = (
            start_line + len(selected_lines) - 1 if selected_lines else None
        )
        return {
            "path": workspace.relative_path(file_path),
            "content": numbered_content,
            "start_line": start_line,
            "end_line": end_line,
            "has_more": has_more,
            "next_start_line": end_line + 1 if has_more else None,
        }

    return Tool(
        name="read_file",
        description=(
            "流式按行读取工作区内的 UTF-8 文本文件；存在后续内容时，"
            "使用返回的 next_start_line 继续读取。"
        ),
        parameters=ReadFileParameters,
        handler=read_file,
    )
