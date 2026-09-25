"""基于 QHarness 的演示文稿任务装配层。"""

from presentation_agent.contract import (
    PRESENTATION_SKILL_CODE,
    build_presentation_check_catalog,
    build_presentation_contract,
    install_presentation_skill,
    load_presentation_skill,
)

__all__ = [
    "PRESENTATION_SKILL_CODE",
    "build_presentation_check_catalog",
    "build_presentation_contract",
    "install_presentation_skill",
    "load_presentation_skill",
]
