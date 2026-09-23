from pathlib import Path

import pytest

from paper_video_agent.editor import (
    _normalize_edited_script,
    build_editor_cache_metadata,
    load_cached_edited_script,
    save_edited_script,
)
from paper_video_agent.models import (
    EditedScriptChapters,
    PaperScript,
    PaperScriptAudit,
)


def _paper_script(chapter_count: int = 5) -> PaperScript:
    planned_chapters = [
        {
            "chapter_id": f"chapter_{index:02d}",
            "title": f"章节{index}",
            "narrative_goal": "讲清楚问题",
            "guiding_question": f"问题{index}是什么？",
            "key_points": [f"关键点{index}"],
            "takeaway": f"结论{index}",
            "source_pages": [index, index + 10],
            "transition_goal": "引出下一部分",
        }
        for index in range(1, chapter_count + 1)
    ]
    return PaperScript.model_validate({
        "title": "测试视频",
        "plan": {
            "video_title": "测试视频",
            "core_message": "核心信息",
            "central_question": "核心问题",
            "story_spine": ["问题", "方法", "结论"],
            "opening_hook": "开场",
            "chapters": planned_chapters,
        },
        "chapters": [
            {
                "chapter_id": f"chapter_{index:02d}",
                "title": f"章节{index}",
                "segments": [{
                    "page": index,
                    "text": f"第{index}章原始口播。",
                }],
            }
            for index in range(1, chapter_count + 1)
        ],
    })


def _paper_script_audit(script: PaperScript) -> PaperScriptAudit:
    chapters = []
    for index, chapter in enumerate(script.chapters, start=1):
        chapters.append({
            "chapter_id": chapter.chapter_id,
            "source_pages": script.plan.chapters[index - 1].source_pages,
            "segments": [{
                "segment_index": 1,
                "claims": [{
                    "claim": f"第{index}章事实",
                    "verdict": "supported",
                    "evidence_type": "direct",
                    "evidence_pages": [index],
                    "evidence": "来源页直接支持。",
                    "issue": None,
                    "severity": "none",
                    "suggested_revision": None,
                }],
            }],
        })
    return PaperScriptAudit.model_validate({
        "script_title": script.title,
        "chapters": chapters,
        "summary": {
            "total_segments": len(chapters),
            "total_claims": len(chapters),
            "supported": len(chapters),
            "partially_supported": 0,
            "unsupported": 0,
            "conflicting": 0,
            "not_verifiable": 0,
            "high_severity_issues": 0,
        },
    })


def _edited_chapters(script: PaperScript) -> EditedScriptChapters:
    return EditedScriptChapters.model_validate({
        "chapters": [
            {
                "chapter_id": chapter.chapter_id,
                "title": "模型不应修改标题",
                "segments": [{
                    "page": chapter.segments[0].page,
                    "text": chapter.segments[0].text.replace("原始", "编辑后"),
                }],
            }
            for chapter in script.chapters
        ],
    })


def test_normalize_edited_script_preserves_metadata_and_allows_chapter_drop() -> None:
    raw_script = _paper_script()
    result = _edited_chapters(raw_script)
    result = result.model_copy(update={"chapters": result.chapters[:4]})

    edited = _normalize_edited_script(result, raw_script)

    assert edited.title == raw_script.title
    assert edited.plan.core_message == raw_script.plan.core_message
    assert [chapter.chapter_id for chapter in edited.chapters] == [
        "chapter_01",
        "chapter_02",
        "chapter_03",
        "chapter_04",
    ]
    assert [chapter.chapter_id for chapter in edited.plan.chapters] == [
        "chapter_01",
        "chapter_02",
        "chapter_03",
        "chapter_04",
    ]
    assert edited.chapters[0].title == "章节1"


def test_normalize_edited_script_rejects_reordered_chapters() -> None:
    raw_script = _paper_script()
    result = _edited_chapters(raw_script)
    result = result.model_copy(update={
        "chapters": [
            result.chapters[1],
            result.chapters[0],
            *result.chapters[2:],
        ],
    })

    with pytest.raises(ValueError, match="章节顺序"):
        _normalize_edited_script(result, raw_script)


def test_normalize_edited_script_rejects_unrelated_page() -> None:
    raw_script = _paper_script()
    result = _edited_chapters(raw_script)
    invalid_segment = result.chapters[0].segments[0].model_copy(update={
        "page": 99,
    })
    invalid_chapter = result.chapters[0].model_copy(update={
        "segments": [invalid_segment],
    })
    result = result.model_copy(update={
        "chapters": [invalid_chapter, *result.chapters[1:]],
    })

    with pytest.raises(ValueError, match="不允许的页码"):
        _normalize_edited_script(result, raw_script)


def test_editor_cache_tracks_script_and_audit() -> None:
    raw_script = _paper_script()
    audit = _paper_script_audit(raw_script)
    original = build_editor_cache_metadata(raw_script, audit)

    changed_script = raw_script.model_copy(update={"title": "另一个标题"})
    assert (
        build_editor_cache_metadata(changed_script, audit)["fingerprint"]
        != original["fingerprint"]
    )

    changed_summary = audit.summary.model_copy(update={"supported": 0})
    changed_audit = audit.model_copy(update={"summary": changed_summary})
    assert (
        build_editor_cache_metadata(raw_script, changed_audit)["fingerprint"]
        != original["fingerprint"]
    )


def test_edited_script_cache_round_trip(tmp_path: Path) -> None:
    raw_script = _paper_script()
    audit = _paper_script_audit(raw_script)
    edited = _normalize_edited_script(_edited_chapters(raw_script), raw_script)
    cache = build_editor_cache_metadata(raw_script, audit)
    script_path = tmp_path / "paper_script.edited.json"
    cache_path = tmp_path / "paper_script.edited.cache.json"

    save_edited_script(edited, script_path, cache_path, cache)

    assert load_cached_edited_script(script_path, cache_path, cache) == edited
    assert load_cached_edited_script(
        script_path,
        cache_path,
        {**cache, "fingerprint": "changed"},
    ) is None
