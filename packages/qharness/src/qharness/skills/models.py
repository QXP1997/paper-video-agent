"""Skill 的可信清单与可序列化激活内容。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from qharness.loop import ActiveSkill


@dataclass(frozen=True, slots=True)
class Skill:
    """一个已校验的 Skill；数据库只需保存 skill_path。"""

    name: str
    display_name: str
    description: str
    instructions: str
    skill_path: Path
    digest: str
    metadata: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def code(self) -> str:
        """返回稳定机器标识；name 属性作为向后兼容别名保留。"""
        return self.name

    @property
    def root(self) -> Path:
        """其他资源始终相对 SKILL.md 的父目录解析。"""
        return self.skill_path.parent

    def activation(self) -> ActiveSkill:
        """生成可随 TaskContract 持久化的指令快照。"""
        return ActiveSkill(
            name=self.name,
            description=self.description,
            instructions=self.instructions,
            digest=self.digest,
        )
