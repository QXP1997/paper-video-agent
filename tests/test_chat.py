from paper_video_agent.chat import _truncate_chapter_title
from paper_video_agent.models import VideoChapterPlan


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
