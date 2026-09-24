from typing import Literal

from pydantic import BaseModel, Field


class PaperVisual(BaseModel):
    """A figure, table, formula, or captioned code block extracted by MinerU."""

    id: str = Field(min_length=1, description="稳定的视觉元素 ID")
    type: Literal["image", "table", "formula", "code"] = Field(
        description="视觉元素类型"
    )
    page: int = Field(ge=1, description="视觉元素所在的 PDF 页码")
    caption: str = Field(default="", description="MinerU 提取的标题、脚注或公式内容")
    asset_path: str | None = Field(
        default=None,
        description="相对于 output/mineru 的图片文件路径；没有独立图片时为空",
    )
    bbox: list[float] | None = Field(
        default=None,
        min_length=4,
        max_length=4,
        description="视觉元素在 MinerU 页面坐标中的边界框",
    )


class ScriptSegment(BaseModel):
    """A narration segment rendered against one PDF page."""

    page: int = Field(
        ge=1,
        description=(
            "播放当前解说时最适合作为主要背景的 PDF 页码；"
            "口播可以综合论文其他页面的信息"
        ),
    )

    text: str = Field(
        min_length=1,
        description="当前这一段的视频中文解说词",
    )

    visual_id: str | None = Field(
        default=None,
        description=(
            "本段重点讲解的图、表、公式或代码视觉元素 ID；"
            "没有明确视觉焦点时为空"
        ),
    )


class VideoChapterPlan(BaseModel):
    """A narrative chapter planned for the video, not a paper section."""

    chapter_id: str = Field(
        min_length=1,
        description="稳定的章节 ID，例如 chapter_01",
    )

    title: str = Field(
        min_length=2,
        description=(
            "显示在视频进度条中的简短中文标题；"
            "过长标题会在业务逻辑中截断"
        ),
    )

    narrative_goal: str = Field(
        min_length=1,
        description="这一视频章节需要帮助观众理解什么",
    )

    guiding_question: str = Field(
        min_length=1,
        description="本章节要替观众回答的一个核心问题",
    )

    key_points: list[str] = Field(
        min_length=1,
        description="这一视频章节必须讲清楚的关键点",
    )

    takeaway: str = Field(
        min_length=1,
        description="本章节讲完后观众应该能用自己的话复述的一句话结论",
    )

    source_pages: list[int] = Field(
        min_length=1,
        description=(
            "支持本章节的主要论文页码，可以跨越论文原有章节；"
            "它们用于规划证据和画面，不限制口播只能引用这些页面"
        ),
    )

    transition_goal: str = Field(
        min_length=1,
        description="本章节结尾应如何自然引出下一个视频章节",
    )


class PaperPlan(BaseModel):
    """The global narrative plan produced before any chapter is written."""

    video_title: str = Field(
        min_length=1,
        description="适合作为视频标题的中文标题",
    )

    core_message: str = Field(
        min_length=1,
        description="整期视频希望观众最终记住的核心信息",
    )

    central_question: str = Field(
        min_length=1,
        description="论文试图回答的核心问题，用非论文式语言表达",
    )

    story_spine: list[str] = Field(
        min_length=3,
        max_length=7,
        description="从问题、困难、方案到证据与边界的因果解释链",
    )

    teaching_anchor: str | None = Field(
        default=None,
        description="帮助理解全文的贯穿案例、类比或思想实验；不适合时为空",
    )

    opening_hook: str = Field(
        min_length=1,
        description="第一章开头采用的叙事切入点",
    )

    chapters: list[VideoChapterPlan] = Field(
        min_length=4,
        max_length=8,
        description="根据视频叙事重新规划的章节，而不是论文原有目录",
    )


class VideoChapterScript(BaseModel):
    """All narration segments generated together for one video chapter."""

    chapter_id: str = Field(
        min_length=1,
        description="与视频章节规划一致的章节 ID",
    )

    title: str = Field(
        min_length=1,
        description="与视频章节规划一致的章节标题",
    )

    segments: list[ScriptSegment] = Field(
        min_length=1,
        description="当前视频章节内按播放顺序排列的解说段落",
    )


class PaperScript(BaseModel):
    """A chapter-aware video narration script at any pipeline stage."""

    title: str = Field(
        min_length=1,
        description="视频标题",
    )

    plan: PaperPlan = Field(
        description="写作前生成的完整视频叙事规划",
    )

    chapters: list[VideoChapterScript] = Field(
        min_length=1,
        description="按视频播放顺序排列的视频章节",
    )

    visuals: list[PaperVisual] = Field(
        default_factory=list,
        description="MinerU 提取并可供口播段落引用的结构化视觉元素目录",
    )


class ClaimFactAudit(BaseModel):
    """Evidence-bound review of one atomic claim in a narration segment."""

    claim: str = Field(
        min_length=1,
        description="从口播中拆出的一个可独立核验的原子陈述",
    )

    verdict: Literal[
        "supported",
        "partially_supported",
        "unsupported",
        "conflicting",
        "not_verifiable",
    ] = Field(
        description="仅根据本章 source_pages 中的证据得出的审核结论",
    )

    evidence_type: Literal[
        "direct",
        "derived",
        "contextual",
        "none",
    ] = Field(
        description="证据是原文直接陈述、可复算推导、上下文支持，还是不存在",
    )

    evidence_pages: list[int] = Field(
        default_factory=list,
        description="实际支持或反驳该陈述的 source_pages 子集",
    )

    evidence: str | None = Field(
        default=None,
        description="简短说明原文证据、推导依据或冲突位置",
    )

    issue: str | None = Field(
        default=None,
        description="事实错误、条件缺失、措辞过强或无法核验等具体问题",
    )

    severity: Literal["none", "low", "medium", "high"] = Field(
        description="该问题对观众正确理解论文的影响程度",
    )

    suggested_revision: str | None = Field(
        default=None,
        description="存在问题时给出的最小修正建议；审核通过时为空",
    )


class SegmentFactAudit(BaseModel):
    """Fact review for one narration segment."""

    segment_index: int = Field(
        ge=1,
        description="当前章节内从 1 开始的 segment 序号",
    )

    claims: list[ClaimFactAudit] = Field(
        default_factory=list,
        description="该 segment 中需要核验的论文事实或论文相关解释",
    )

    notes: str | None = Field(
        default=None,
        description="没有可核验陈述时说明原因，否则通常为空",
    )


class ChapterFactAudit(BaseModel):
    """Evidence review produced for a complete video chapter."""

    chapter_id: str = Field(
        min_length=1,
        description="与被审核视频章节一致的章节 ID",
    )

    source_pages: list[int] = Field(
        min_length=1,
        description="本次审核允许使用的全部论文页码",
    )

    segments: list[SegmentFactAudit] = Field(
        min_length=1,
        description="逐个覆盖本章所有口播 segment 的审核结果",
    )


class ScriptAuditSummary(BaseModel):
    """Deterministic aggregate counts for a paper-script audit."""

    total_segments: int = Field(ge=0)
    total_claims: int = Field(ge=0)
    supported: int = Field(ge=0)
    partially_supported: int = Field(ge=0)
    unsupported: int = Field(ge=0)
    conflicting: int = Field(ge=0)
    not_verifiable: int = Field(ge=0)
    high_severity_issues: int = Field(ge=0)


class PaperScriptAudit(BaseModel):
    """Fact-audit artifact for a complete paper narration script."""

    script_title: str = Field(
        min_length=1,
        description="被审核脚本的视频标题",
    )

    evidence_scope: Literal["chapter_source_pages"] = Field(
        default="chapter_source_pages",
        description="审核严格限定在每章规划的 source_pages",
    )

    chapters: list[ChapterFactAudit] = Field(
        min_length=1,
        description="按脚本顺序排列的章节审核结果",
    )

    summary: ScriptAuditSummary


class EditedScriptChapters(BaseModel):
    """Globally edited narration chapters returned by the editorial node."""

    chapters: list[VideoChapterScript] = Field(
        min_length=1,
        description=(
            "完成事实修正、去重、信息取舍和口语化后的全部视频章节"
        ),
    )


class ScriptValidationIssue(BaseModel):
    """One actionable problem found during final script validation."""

    code: str = Field(
        min_length=1,
        description="稳定、可用于程序判断的问题代码",
    )

    severity: Literal["low", "medium", "high"]

    message: str = Field(
        min_length=1,
        description="供返修节点和人工检查使用的具体问题说明",
    )

    chapter_id: str | None = None
    segment_index: int | None = Field(default=None, ge=1)


class ScriptValidationMetrics(BaseModel):
    """Deterministic quality metrics for one script candidate."""

    chapter_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)
    character_count: int = Field(ge=0)
    number_mentions: int = Field(ge=0)
    numeric_dense_segments: int = Field(ge=0)
    exact_duplicate_pairs: int = Field(ge=0)
    fuzzy_duplicate_pairs: int = Field(ge=0)


class ScriptValidationReport(BaseModel):
    """Final validation result, including any constrained repair rounds."""

    passed: bool
    repair_rounds: int = Field(ge=0)
    initial_metrics: ScriptValidationMetrics
    final_metrics: ScriptValidationMetrics
    initial_issues: list[ScriptValidationIssue] = Field(default_factory=list)
    final_issues: list[ScriptValidationIssue] = Field(default_factory=list)
