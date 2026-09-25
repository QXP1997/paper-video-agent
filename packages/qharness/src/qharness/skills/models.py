"""Skill 的可信清单与可序列化激活内容。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qharness.loop import ActiveSkill


@dataclass(frozen=True, slots=True)
class Skill:
    """一个已校验的 Skill；root 仅用于读取其按需资源。"""

    name: str
    description: str
    instructions: str
    root: Path
    digest: str

    def activation(self) -> ActiveSkill:
        """生成可随 TaskContract 持久化的指令快照。"""
        return ActiveSkill(
            name=self.name,
            description=self.description,
            instructions=self.instructions,
            digest=self.digest,
        )
