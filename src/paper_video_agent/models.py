from pydantic import BaseModel, Field


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
    """The final chapter-aware video narration script."""

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
