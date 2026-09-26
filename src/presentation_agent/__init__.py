"""基于 QHarness 的演示文稿任务装配层。"""

from presentation_agent.contract import (
    PPTX_SKILL_CODE,
    PRESENTATION_SKILL_CODE,
    build_presentation_check_catalog,
    build_presentation_contract,
    install_presentation_skill,
    install_presentation_skills,
    load_pptx_skill,
    load_presentation_skill,
    load_presentation_skills,
)

__all__ = [
    "PRESENTATION_SKILL_CODE",
    "PPTX_SKILL_CODE",
    "build_presentation_check_catalog",
    "build_presentation_contract",
    "install_presentation_skill",
    "install_presentation_skills",
    "load_presentation_skill",
    "load_presentation_skills",
    "load_pptx_skill",
    "PptxRenderError",
    "render_pptx",
]


def __getattr__(name: str):
    """延迟加载渲染器，避免执行 ``python -m presentation_agent.pptx`` 时重复导入。"""
    if name in {"PptxRenderError", "render_pptx"}:
        from presentation_agent.pptx import PptxRenderError, render_pptx

        return {"PptxRenderError": PptxRenderError, "render_pptx": render_pptx}[name]
    raise AttributeError(name)
