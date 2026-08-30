# -*- coding: utf-8 -*-
"""使用 ripgrep 搜索工作区文本的只读工具。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from pydantic import Field

from qharness.tools.base import Tool, ToolParameters
from qharness.workspace import WorkspaceContext


class SearchTextParameters(ToolParameters):
    """文本搜索工具的参数。"""

    query: str = Field(
        min_length=1,
        max_length=500,
        description="要搜索的文本或正则表达式。",
    )
    path: str = Field(default=".", description="搜索起点，可以是文件或目录。")
    glob: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="ripgrep Glob 过滤条件，例如 *.py 或 !*.min.js。",
    )
    case_sensitive: bool = Field(default=False, description="是否区分大小写。")
    regex: bool = Field(
        default=False,
        description="是否把 query 当作正则表达式；默认按普通文本搜索。",
    )
    include_hidden: bool = Field(
        default=False,
        description="是否搜索名称以点开头的隐藏文件和目录。",
    )
    respect_git_ignore: bool = Field(
        default=True,
        description="是否遵循 .gitignore 等忽略规则。",
    )
    max_results: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="最多返回的匹配结果数量。",
    )


def create_search_text_tool(
    workspace: WorkspaceContext,
    ripgrep_path: str,
) -> Tool:
    """创建绑定到指定工作区和 ripgrep 可执行文件的搜索工具。"""

    async def search_text(
        query: str,
        path: str = ".",
        glob: list[str] | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        include_hidden: bool = False,
        respect_git_ignore: bool = True,
        max_results: int = 100,
    ) -> dict[str, Any]:
        """运行 ripgrep 并将 JSON 事件转换为稳定的搜索结果。"""

        search_root = workspace.resolve_path(path)
        relative_root = workspace.relative_path(search_root)
        command = [
            ripgrep_path,
            "--json",
            "--color=never",
            "--case-sensitive" if case_sensitive else "--ignore-case",
        ]
        if not regex:
            command.append("--fixed-strings")
        if include_hidden:
            command.append("--hidden")
        if not respect_git_ignore:
            command.append("--no-ignore")
        for pattern in glob or []:
            command.extend(["--glob", pattern])
        command.extend(["--", query, relative_root])

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workspace.root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=1024 * 1024,
            )
        except OSError as error:
            raise RuntimeError(f"无法启动 ripgrep：{error}") from error

        if process.stdout is None or process.stderr is None:
            process.kill()
            await process.wait()
            raise RuntimeError("ripgrep 子进程未提供标准输出或错误流。")

        stderr_task = asyncio.create_task(process.stderr.read())
        matches: list[dict[str, Any]] = []
        truncated = False
        try:
            async for output_line in process.stdout:
                event = _parse_json_event(output_line)
                if event is None or event.get("type") != "match":
                    continue
                match = _map_match(workspace, event)
                if match is None:
                    continue
                if len(matches) >= max_results:
                    truncated = True
                    if process.returncode is None:
                        process.terminate()
                    break
                matches.append(match)

            return_code = await process.wait()
            stderr_text = (await stderr_task).decode("utf-8", errors="replace").strip()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            raise
        except Exception:
            if process.returncode is None:
                process.kill()
            await process.wait()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
            raise

        # ripgrep 使用 0 表示找到匹配，1 表示没有匹配；其他状态才是错误。
        if not truncated and return_code not in {0, 1}:
            message = stderr_text or f"ripgrep 退出码：{return_code}"
            raise RuntimeError(f"搜索失败：{message}")

        return {
            "query": query,
            "path": relative_root,
            "matches": matches,
            "count": len(matches),
            "truncated": truncated,
        }

    return Tool(
        name="search_text",
        description=(
            "使用 ripgrep 在工作区文件中搜索文本或正则表达式，"
            "返回文件、行号、列号和匹配行。"
        ),
        parameters=SearchTextParameters,
        handler=search_text,
    )


def _parse_json_event(output_line: bytes) -> dict[str, Any] | None:
    """解析一行 ripgrep JSON 事件，忽略无法识别的非 JSON 输出。"""

    try:
        event = json.loads(output_line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return event if isinstance(event, dict) else None


def _map_match(
    workspace: WorkspaceContext,
    event: dict[str, Any],
) -> dict[str, Any] | None:
    """将 ripgrep match 事件转换为工作区相对搜索结果。"""

    data = event.get("data")
    if not isinstance(data, dict):
        return None
    path_data = data.get("path")
    lines_data = data.get("lines")
    if not isinstance(path_data, dict) or not isinstance(lines_data, dict):
        return None
    path_text = path_data.get("text")
    line_text = lines_data.get("text")
    line_number = data.get("line_number")
    if not isinstance(path_text, str) or not isinstance(line_text, str):
        return None
    if not isinstance(line_number, int):
        return None

    absolute_path = Path(path_text)
    if not absolute_path.is_absolute():
        absolute_path = workspace.root / absolute_path
    relative_path = workspace.relative_path(absolute_path)

    submatches = data.get("submatches")
    byte_offset = 0
    if isinstance(submatches, list) and submatches:
        first_submatch = submatches[0]
        if isinstance(first_submatch, dict):
            raw_offset = first_submatch.get("start", 0)
            if isinstance(raw_offset, int):
                byte_offset = raw_offset
    column = _byte_offset_to_column(line_text, byte_offset)
    return {
        "path": relative_path,
        "line": line_number,
        "column": column,
        "text": line_text.rstrip("\r\n"),
    }


def _byte_offset_to_column(text: str, byte_offset: int) -> int:
    """将 ripgrep 的 UTF-8 字节偏移转换为从 1 开始的字符列号。"""

    prefix_bytes = text.encode("utf-8")[:byte_offset]
    prefix = prefix_bytes.decode("utf-8", errors="ignore")
    return len(prefix) + 1
