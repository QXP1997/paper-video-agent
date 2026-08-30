# -*- coding: utf-8 -*-
"""列出工作区目录内容的只读工具。"""

from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field

from qharness.exception import ToolExecutionError, WorkspacePathError
from qharness.tools.base import Tool, ToolErrorCode, ToolParameters
from qharness.tools.cursor import ExpiringCursorStore
from qharness.workspace import WorkspaceContext


_ORDERING_VERSION = "breadth_first_casefold_v1"
_DEFAULT_CURSOR_TTL_SECONDS = 30 * 60
_DEFAULT_MAX_CURSORS = 1024


@dataclass(frozen=True, slots=True)
class _DirectoryCursorState:
    """服务端保存的目录分页位置及其绑定的查询条件。"""

    query_fingerprint: str
    after_path: str


class ListDirectoryParameters(ToolParameters):
    """列出目录工具的参数。"""

    path: str = Field(default=".", description="工作区相对目录路径。")
    recursive: bool = Field(default=False, description="是否递归列出子目录。")
    max_depth: int = Field(
        default=2,
        ge=1,
        le=20,
        description="递归时允许进入的最大目录深度。",
    )
    page_size: int = Field(
        default=200,
        ge=1,
        le=1000,
        description="本页最多返回的文件和目录数量。",
    )
    cursor: str | None = Field(
        default=None,
        max_length=64,
        description="上一页返回的 next_cursor；第一页不传。",
    )
    include_hidden: bool = Field(
        default=False,
        description="是否包含名称以点开头的隐藏条目。",
    )


def _query_fingerprint(
    *,
    workspace_root: str,
    path: str,
    recursive: bool,
    max_depth: int,
    include_hidden: bool,
) -> str:
    """计算影响遍历结果的查询指纹，用于阻止游标跨查询复用。"""

    query = {
        "workspace_root": workspace_root,
        "path": path,
        "recursive": recursive,
        "max_depth": max_depth,
        "include_hidden": include_hidden,
        "ordering": _ORDERING_VERSION,
    }
    serialized = json.dumps(
        query,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _resolve_cursor(
    cursor_store: ExpiringCursorStore[_DirectoryCursorState],
    cursor: str,
    *,
    query_fingerprint: str,
) -> str:
    """读取短游标，校验查询绑定并返回上一页最后一个路径。"""

    state = cursor_store.get(cursor)
    if state is None:
        raise ToolExecutionError(
            "目录分页游标已过期、失效或应用已经重启，"
            "请从第一页重新读取。",
            code=ToolErrorCode.INVALID_ARGUMENTS,
        )
    if state.query_fingerprint != query_fingerprint:
        raise ToolExecutionError(
            "目录分页游标与当前查询条件不匹配，请从第一页重新读取。",
            code=ToolErrorCode.INVALID_ARGUMENTS,
        )
    return state.after_path


def create_list_directory_tool(
    workspace: WorkspaceContext,
    *,
    cursor_ttl_seconds: float = _DEFAULT_CURSOR_TTL_SECONDS,
    max_cursors: int = _DEFAULT_MAX_CURSORS,
) -> Tool:
    """创建绑定到指定工作区的列目录工具。"""

    cursor_store: ExpiringCursorStore[_DirectoryCursorState] = (
        ExpiringCursorStore(
            ttl_seconds=cursor_ttl_seconds,
            max_entries=max_cursors,
        )
    )

    def list_directory(
        path: str = ".",
        recursive: bool = False,
        max_depth: int = 2,
        page_size: int = 200,
        cursor: str | None = None,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        """分页列出目录内容，并保证不会递归进入符号链接目录。"""

        root_directory = workspace.resolve_directory(path)
        root_relative_path = workspace.relative_path(root_directory)
        query_fingerprint = _query_fingerprint(
            workspace_root=str(workspace.root),
            path=root_relative_path,
            recursive=recursive,
            max_depth=max_depth,
            include_hidden=include_hidden,
        )
        resume_after = (
            _resolve_cursor(
                cursor_store,
                cursor,
                query_fingerprint=query_fingerprint,
            )
            if cursor is not None
            else None
        )
        # 正在扫描的路径
        pending: deque[tuple[Path, int]] = deque([(root_directory, 0)])
        # 已经查看过的路径
        visited_directories = {root_directory}
        # 多读取一条记录，用于判断是否还存在下一页。
        page_candidates: list[dict[str, Any]] = []
        cursor_reached = resume_after is None

        while pending and len(page_candidates) <= page_size:
            current_directory, current_depth = pending.popleft()
            try:
                with os.scandir(current_directory) as iterator:
                    children = sorted(
                        iterator,
                        key=lambda item: (item.name.casefold(), item.name),
                    )
            except OSError as error:
                relative = workspace.relative_path(current_directory)
                raise RuntimeError(
                    f"无法读取工作区目录 {relative}：{error}"
                ) from error

            for child in children:
                if not include_hidden and child.name.startswith("."):
                    continue

                child_path = Path(child.path)
                relative_path = child_path.relative_to(workspace.root).as_posix()
                is_symlink = child.is_symlink()
                try:
                    resolved_child = workspace.resolve_path(child_path)
                except WorkspacePathError:
                    # 保留被阻止路径的名称，方便 Agent 理解目录结构；但绝不
                    # 返回链接目标或继续访问，常见原因是链接指向工作区外。
                    entry = {
                        "path": relative_path,
                        "type": "blocked_symlink",
                    }
                    resolved_child = None
                    entry_type = "blocked_symlink"
                else:
                    if is_symlink:
                        entry_type = "symlink"
                        size: int | None = None
                    elif resolved_child.is_dir():
                        entry_type = "directory"
                        size = None
                    elif resolved_child.is_file():
                        entry_type = "file"
                        try:
                            size = resolved_child.stat().st_size
                        except OSError:
                            size = None
                    else:
                        entry_type = "other"
                        size = None

                    entry = {
                        "path": relative_path,
                        "type": entry_type,
                    }
                    if size is not None:
                        entry["size_bytes"] = size

                can_descend = (
                    recursive
                    and entry_type == "directory"
                    and current_depth < max_depth - 1
                )
                if can_descend and resolved_child is not None:
                    if resolved_child not in visited_directories:
                        visited_directories.add(resolved_child)
                        pending.append((resolved_child, current_depth + 1))

                if not cursor_reached:
                    if relative_path == resume_after:
                        cursor_reached = True
                    continue

                page_candidates.append(entry)
                if len(page_candidates) > page_size:
                    pending.clear()
                    break

        if not cursor_reached:
            raise ToolExecutionError(
                "目录内容可能已发生变化，分页游标位置不存在，"
                "请从第一页重新读取。",
                code=ToolErrorCode.INVALID_ARGUMENTS,
            )

        has_more = len(page_candidates) > page_size
        entries = page_candidates[:page_size]
        next_cursor = None
        if has_more and entries:
            next_cursor = cursor_store.create(
                _DirectoryCursorState(
                    query_fingerprint=query_fingerprint,
                    after_path=str(entries[-1]["path"]),
                )
            )

        return {
            "path": root_relative_path,
            "entries": entries,
            "count": len(entries),
            "page_size": page_size,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    return Tool(
        name="list_directory",
        description=(
            "分页列出工作区内指定目录的文件和子目录，可选择有限深度递归；"
            "存在下一页时，使用返回的 next_cursor 继续读取。"
        ),
        parameters=ListDirectoryParameters,
        handler=list_directory,
    )
