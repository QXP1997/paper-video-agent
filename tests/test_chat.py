from paper_video_agent.chat import _truncate_chapter_title
from paper_video_agent.models import PaperVisual, ScriptSegment, VideoChapterPlan


def test_chapter_plan_accepts_long_title() -> None:
    chapter = VideoChapterPlan(
        chapter_id="chapter_01",
        title="shell 为什么不好用",
        narrative_goal="理解问题",
        guiding_question="为什么不好用？",
        key_points=["工具接口复杂"],
        takeaway="接口设计会影响任务成功率",
        source_pages=[1],
        transition_goal="引出改进方法",
    )

    assert chapter.title == "shell 为什么不好用"


def test_long_chapter_title_is_truncated_for_display() -> None:
    assert _truncate_chapter_title("shell 为什么不好用") == "shell..."
    assert _truncate_chapter_title("核心方法") == "核心方法"


def test_visual_catalog_and_segment_reference_models() -> None:
    visual = PaperVisual(
        id="page_002_image_01",
        type="image",
        page=2,
        caption="Figure 1: Architecture",
        asset_path="images/figure.jpg",
        bbox=[1, 2, 3, 4],
    )
    segment = ScriptSegment(
        page=2,
        text="图 1 展示了整体架构。",
        visual_id=visual.id,
    )

    assert segment.visual_id == "page_002_image_01"
    assert visual.caption == "Figure 1: Architecture"
