# -*- coding: utf-8 -*-
"""解析并安全应用 QHarness 的结构化多文件文本补丁。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from qharness.exception import WorkspaceConflictError, WorkspaceMutationError


_SECTION_PATTERN = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")
_NUMBERED_HUNK_PATTERN = re.compile(
    r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(?:.*)?$"
)


class PatchAction(StrEnum):
    """补丁文件段支持的操作类型。"""

    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class PatchHunk:
    """Update File 中一块带上下文的行级变化。"""

    # 标准 unified diff 可携带从 1 开始的旧文件起始行；省略时为 None。
    old_start_line: int | None

    # 每项由前缀字符和不含换行符的正文组成。
    lines: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class PatchFile:
    """补丁内针对单个相对路径的操作。"""

    path: str
    action: PatchAction
    added_lines: tuple[str, ...] = ()
    hunks: tuple[PatchHunk, ...] = ()

    def apply(self, original: str | None) -> str | None:
        """把当前文件段应用到原文本，并返回目标文本或删除标记。"""

        if self.action is PatchAction.ADD:
            if original is not None:
                raise WorkspaceConflictError(
                    f"补丁要求新增文件，但目标已经存在：{self.path}"
                )
            return "\n".join(self.added_lines) + ("\n" if self.added_lines else "")
        if self.action is PatchAction.DELETE:
            if original is None:
                raise WorkspaceConflictError(
                    f"补丁要求删除文件，但目标不存在：{self.path}"
                )
            return None
        if original is None:
            raise WorkspaceConflictError(
                f"补丁要求更新文件，但目标不存在：{self.path}"
            )
        return _apply_hunks(self.path, original, self.hunks)


def parse_patch(patch_text: str) -> tuple[PatchFile, ...]:
    """解析 ``*** Begin Patch`` 格式，并拒绝歧义或重复目标。"""

    if not isinstance(patch_text, str) or not patch_text.strip():
        raise WorkspaceMutationError("patch 不能为空。")
    lines = patch_text.splitlines()
    if not lines or lines[0] != "*** Begin Patch":
        raise WorkspaceMutationError("补丁必须以 *** Begin Patch 开始。")
    if lines[-1] != "*** End Patch":
        raise WorkspaceMutationError("补丁必须以 *** End Patch 结束。")

    result: list[PatchFile] = []
    seen_paths: set[str] = set()
    index = 1
    while index < len(lines) - 1:
        match = _SECTION_PATTERN.fullmatch(lines[index])
        if match is None:
            raise WorkspaceMutationError(
                f"补丁第 {index + 1} 行应为文件操作头：{lines[index]}"
            )
        action = PatchAction(match.group(1).lower())
        path = match.group(2).strip().replace("\\", "/")
        if not path:
            raise WorkspaceMutationError("补丁文件路径不能为空。")
        if path in seen_paths:
            raise WorkspaceMutationError(f"同一补丁不能重复操作文件：{path}")
        seen_paths.add(path)
        index += 1
        body: list[str] = []
        while index < len(lines) - 1 and _SECTION_PATTERN.fullmatch(lines[index]) is None:
            body.append(lines[index])
            index += 1

        if action is PatchAction.ADD:
            result.append(_parse_add_file(path, body))
        elif action is PatchAction.DELETE:
            if body:
                raise WorkspaceMutationError(
                    f"Delete File 不应携带正文：{path}"
                )
            result.append(PatchFile(path=path, action=action))
        else:
            result.append(_parse_update_file(path, body))

    if not result:
        raise WorkspaceMutationError("补丁至少需要一个文件操作。")
    return tuple(result)


def _parse_add_file(path: str, body: list[str]) -> PatchFile:
    """解析 Add File 正文；每一行都必须显式使用加号。"""

    added: list[str] = []
    for line in body:
        if not line.startswith("+"):
            raise WorkspaceMutationError(
                f"新增文件 {path} 的每一行都必须以 + 开头。"
            )
        added.append(line[1:])
    return PatchFile(path=path, action=PatchAction.ADD, added_lines=tuple(added))


def _parse_update_file(path: str, body: list[str]) -> PatchFile:
    """解析一个或多个 Update File hunk。"""

    if not body:
        raise WorkspaceMutationError(f"Update File 缺少修改块：{path}")
    hunks: list[PatchHunk] = []
    index = 0
    while index < len(body):
        marker = body[index]
        if not marker.startswith("@@"):
            raise WorkspaceMutationError(
                f"更新文件 {path} 的修改块必须以 @@ 开始。"
            )
        numbered = _NUMBERED_HUNK_PATTERN.fullmatch(marker)
        old_start_line = int(numbered.group(1)) if numbered else None
        index += 1
        hunk_lines: list[tuple[str, str]] = []
        while index < len(body) and not body[index].startswith("@@"):
            line = body[index]
            if line == r"\ No newline at end of file":
                index += 1
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                raise WorkspaceMutationError(
                    f"更新文件 {path} 的 hunk 行必须以空格、+ 或 - 开头。"
                )
            hunk_lines.append((line[0], line[1:]))
            index += 1
        if not hunk_lines or not any(prefix in {"+", "-"} for prefix, _ in hunk_lines):
            raise WorkspaceMutationError(f"更新文件 {path} 的 hunk 没有实际变化。")
        hunks.append(PatchHunk(old_start_line, tuple(hunk_lines)))
    return PatchFile(path=path, action=PatchAction.UPDATE, hunks=tuple(hunks))


def _apply_hunks(path: str, original: str, hunks: tuple[PatchHunk, ...]) -> str:
    """顺序应用 hunk；上下文找不到或存在歧义时拒绝猜测。"""

    newline = "\r\n" if "\r\n" in original else "\n"
    had_final_newline = original.endswith(("\n", "\r"))
    current = original.splitlines()
    search_from = 0

    for hunk_index, hunk in enumerate(hunks, start=1):
        old_lines = [text for prefix, text in hunk.lines if prefix != "+"]
        new_lines = [text for prefix, text in hunk.lines if prefix != "-"]
        candidates = _find_subsequence(current, old_lines, search_from)
        preferred = (
            hunk.old_start_line - 1 if hunk.old_start_line is not None else None
        )
        if preferred is not None and preferred in candidates:
            position = preferred
        elif len(candidates) == 1:
            position = candidates[0]
        elif not candidates:
            raise WorkspaceConflictError(
                f"补丁无法应用到 {path} 的第 {hunk_index} 个 hunk："
                "原始上下文已经变化，请重新读取文件并生成补丁。"
            )
        else:
            raise WorkspaceConflictError(
                f"补丁在 {path} 的第 {hunk_index} 个 hunk 找到 "
                f"{len(candidates)} 个相同位置，无法安全判断目标。"
            )
        current[position : position + len(old_lines)] = new_lines
        search_from = position + len(new_lines)

    result = newline.join(current)
    if had_final_newline and current:
        result += newline
    return result


def _find_subsequence(
    content: list[str],
    expected: list[str],
    start: int,
) -> list[int]:
    """返回候选子序列的全部起始位置。"""

    if not expected:
        return list(range(start, len(content) + 1))
    last = len(content) - len(expected)
    return [
        index
        for index in range(start, last + 1)
        if content[index : index + len(expected)] == expected
    ]
