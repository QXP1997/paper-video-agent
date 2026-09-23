from pathlib import Path

import pytest

from paper_video_agent.audit import (
    _normalize_chapter_audit,
    build_audit_cache_metadata,
    generate_script_fact_audit,
    load_cached_script_audit,
    save_script_audit,
    select_source_page_content,
)
from paper_video_agent.models import (
    ChapterFactAudit,
    PaperScript,
    PaperScriptAudit,
)


def _paper_script() -> PaperScript:
    planned_chapters = [
        {
            "chapter_id": f"chapter_{index:02d}",
            "title": f"章节{index}",
            "narrative_goal": "讲清楚问题",
            "guiding_question": "为什么？",
            "key_points": ["关键点"],
            "takeaway": "核心结论",
            "source_pages": [2, 4] if index == 1 else [1],
            "transition_goal": "引出下一部分",
        }
        for index in range(1, 5)
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
        "chapters": [{
            "chapter_id": "chapter_01",
            "title": "章节1",
            "segments": [
                {"page": 2, "text": "方法在测试集上达到百分之八十。"},
                {"page": 4, "text": "下面看看它为什么有效。"},
            ],
        }],
    })


def _chapter_audit() -> ChapterFactAudit:
    return ChapterFactAudit.model_validate({
        "chapter_id": "chapter_01",
        "source_pages": [2, 4],
        "segments": [
            {
                "segment_index": 1,
                "claims": [{
                    "claim": "方法在测试集上达到百分之八十",
                    "verdict": "supported",
                    "evidence_type": "direct",
                    "evidence_pages": [2],
                    "evidence": "第2页报告该结果。",
                    "issue": None,
                    "severity": "none",
                    "suggested_revision": None,
                }],
            },
            {
                "segment_index": 2,
                "claims": [],
                "notes": "过渡句，没有论文事实。",
            },
        ],
    })


def test_select_source_page_content_uses_only_requested_pages() -> None:
    pages = [
        {"page": 1, "text": "第一页"},
        {"page": 2, "text": "第二页"},
        {"page": 3, "text": "第三页"},
        {"page": 4, "text": "第四页"},
    ]

    assert select_source_page_content(pages, [4, 2]) == [
        {"page": 4, "text": "第四页"},
        {"page": 2, "text": "第二页"},
    ]


def test_select_source_page_content_rejects_missing_page() -> None:
    with pytest.raises(ValueError, match="审核来源页不存在"):
        select_source_page_content([{"page": 1, "text": "第一页"}], [2])


def test_chapter_audit_must_cover_every_segment_in_order() -> None:
    script = _paper_script()
    incomplete = _chapter_audit().model_copy(update={
        "segments": _chapter_audit().segments[:1],
    })

    with pytest.raises(ValueError, match="完整覆盖"):
        _normalize_chapter_audit(
            incomplete,
            script.chapters[0],
            script.plan.chapters[0],
        )


def test_chapter_audit_rejects_evidence_outside_source_pages() -> None:
    script = _paper_script()
    audit = _chapter_audit()
    invalid_claim = audit.segments[0].claims[0].model_copy(update={
        "evidence_pages": [3],
    })
    invalid_segment = audit.segments[0].model_copy(update={
        "claims": [invalid_claim],
    })
    invalid_audit = audit.model_copy(update={
        "segments": [invalid_segment, audit.segments[1]],
    })

    with pytest.raises(ValueError, match="source_pages 之外"):
        _normalize_chapter_audit(
            invalid_audit,
            script.chapters[0],
            script.plan.chapters[0],
        )


def test_generate_script_fact_audit_builds_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _paper_script()
    received_evidence = []

    def fake_chapter_audit(**kwargs) -> ChapterFactAudit:
        received_evidence.extend(kwargs["evidence_pages"])
        return _chapter_audit()

    monkeypatch.setattr(
        "paper_video_agent.audit.generate_chapter_fact_audit",
        fake_chapter_audit,
    )
    result = generate_script_fact_audit(
        [
            {"page": 1, "text": "不能发送给第一章审核"},
            {"page": 2, "text": "实验结果"},
            {"page": 3, "text": "也不能发送给第一章审核"},
            {"page": 4, "text": "方法说明"},
        ],
        script,
    )

    assert [page["page"] for page in received_evidence] == [2, 4]
    assert result.summary.total_segments == 2
    assert result.summary.total_claims == 1
    assert result.summary.supported == 1
    assert result.summary.unsupported == 0


def test_audit_cache_tracks_only_script_and_selected_source_pages() -> None:
    script = _paper_script()
    pages = [
        {"page": 1, "text": "未选中的页面"},
        {"page": 2, "text": "实验结果"},
        {"page": 3, "text": "另一个未选中的页面"},
        {"page": 4, "text": "方法说明"},
    ]
    original = build_audit_cache_metadata(pages, script)

    changed_unselected = [dict(page) for page in pages]
    changed_unselected[2]["text"] = "未选中的页面发生变化"
    assert build_audit_cache_metadata(changed_unselected, script) == original

    changed_selected = [dict(page) for page in pages]
    changed_selected[1]["text"] = "来源页发生变化"
    assert (
        build_audit_cache_metadata(changed_selected, script)["fingerprint"]
        != original["fingerprint"]
    )


def test_script_audit_cache_round_trip(tmp_path: Path) -> None:
    script = _paper_script()
    pages = [
        {"page": 1, "text": "第一页"},
        {"page": 2, "text": "实验结果"},
        {"page": 4, "text": "方法说明"},
    ]
    chapter_audit = _chapter_audit()
    audit = PaperScriptAudit.model_validate({
        "script_title": script.title,
        "chapters": [chapter_audit.model_dump()],
        "summary": {
            "total_segments": 2,
            "total_claims": 1,
            "supported": 1,
            "partially_supported": 0,
            "unsupported": 0,
            "conflicting": 0,
            "not_verifiable": 0,
            "high_severity_issues": 0,
        },
    })
    cache = build_audit_cache_metadata(pages, script)
    audit_path = tmp_path / "script_audit.json"
    cache_path = tmp_path / "script_audit.cache.json"

    save_script_audit(audit, audit_path, cache_path, cache)

    assert load_cached_script_audit(audit_path, cache_path, cache) == audit
    assert load_cached_script_audit(
        audit_path,
        cache_path,
        {**cache, "fingerprint": "changed"},
    ) is None
