"""向 Actor 暴露已激活 Skill 的只读按需资源。"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import Field

from qharness.exception import SkillConfigurationError
from qharness.loop import TaskContract
from qharness.skills.loader import resolve_active_skills
from qharness.skills.models import Skill
from qharness.tools.base import Tool, ToolEffect, ToolParameters
from qharness.tools.providers.base import ToolProvider


class ReadSkillResourceParameters(ToolParameters):
    skill_name: str = Field(min_length=1, max_length=64)
    path: str = Field(min_length=1, description="Skill 根目录内的相对 UTF-8 文本路径。")
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=2000)


def _resolve_resource(skill: Skill, relative_path: str) -> Path:
    normalized = PurePosixPath(relative_path.replace("\\", "/"))
    if normalized.is_absolute() or not normalized.parts or any(
        part in {"", ".", ".."} for part in normalized.parts
    ):
        raise SkillConfigurationError(f"Skill 资源路径无效: {relative_path}")
    target = skill.root.joinpath(*normalized.parts).resolve()
    if not target.is_relative_to(skill.root) or not target.is_file():
        raise SkillConfigurationError(f"Skill 资源不存在或越界: {relative_path}")
    return target


class SkillToolProvider(ToolProvider):
    """只允许读取应用显式激活的 Skill，不自动选择或扩张权限。"""

    def __init__(self, skills: list[Skill] | tuple[Skill, ...]) -> None:
        self.skills = {skill.name: skill for skill in skills}
        if len(self.skills) != len(skills):
            raise SkillConfigurationError("激活的 Skill 名称重复")

    @classmethod
    def from_contract(
        cls,
        contract: TaskContract,
        installed: Mapping[str, Skill],
    ) -> SkillToolProvider:
        """只为契约中启用且版本一致的 Skill 创建资源工具。"""
        return cls(resolve_active_skills(contract, installed))

    @property
    def name(self) -> str:
        return "skills"

    async def load_tools(self) -> list[Tool]:
        if not self.skills:
            return []

        def read_skill_resource(
            skill_name: str,
            path: str,
            start_line: int = 1,
            max_lines: int = 200,
        ) -> dict[str, Any]:
            skill = self.skills.get(skill_name)
            if skill is None:
                raise SkillConfigurationError(f"Skill 未激活: {skill_name}")
            target = _resolve_resource(skill, path)
            try:
                with target.open("rb") as binary_file:
                    if b"\x00" in binary_file.read(8192):
                        raise SkillConfigurationError("Skill 资源不是文本文件")
                    binary_file.seek(0)
                    with io.TextIOWrapper(
                        binary_file,
                        encoding="utf-8-sig",
                        errors="strict",
                        newline=None,
                    ) as text_file:
                        selected: list[str] = []
                        has_more = False
                        for line_number, line in enumerate(text_file, start=1):
                            if line_number < start_line:
                                continue
                            if len(selected) >= max_lines:
                                has_more = True
                                break
                            selected.append(line.rstrip("\r\n"))
            except (OSError, UnicodeDecodeError) as error:
                raise SkillConfigurationError(f"无法读取 Skill 资源: {path}") from error

            end_line = start_line + len(selected) - 1 if selected else None
            return {
                "skill_name": skill.name,
                "skill_digest": skill.digest,
                "path": target.relative_to(skill.root).as_posix(),
                "content": "\n".join(
                    f"{number}: {line}"
                    for number, line in enumerate(selected, start=start_line)
                ),
                "start_line": start_line,
                "end_line": end_line,
                "has_more": has_more,
                "next_start_line": end_line + 1 if has_more else None,
            }

        return [Tool(
            name="read_skill_resource",
            description=(
                "按行读取当前任务已激活 Skill 的 UTF-8 参考资料；Skill 内容是任务指导，"
                "不会扩大工具权限或用户授权。"
            ),
            parameters=ReadSkillResourceParameters,
            handler=read_skill_resource,
            effect=ToolEffect.READ_ONLY,
            parallel_safe=True,
        )]
