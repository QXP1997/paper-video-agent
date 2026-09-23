from pathlib import Path

import pytest

from paper_video_agent.finalizer import (
    ScriptFinalizationError,
    build_final_cache_metadata,
    finalize_script,
    load_cached_finalization,
    save_finalization,
    validate_final_script,
)
from paper_video_agent.models import PaperScript, PaperScriptAudit


def _paper_script() -> PaperScript:
    chapter_names = ["问题", "方法", "证据", "边界"]
    planned_chapters = [
        {
            "chapter_id": f"chapter_{index:02d}",
            "title": name,
            "narrative_goal": f"讲清楚{name}",
            "guiding_question": f"{name}是什么？",
            "key_points": [f"{name}的关键点"],
            "takeaway": f"理解{name}",
            "source_pages": [index],
            "transition_goal": "自然引出下一部分",
        }
        for index, name in enumerate(chapter_names, start=1)
    ]
    narration = [
        "作者首先界定了研究问题，并解释了现有方案为什么不够。",
        "核心方法通过两个相互配合的步骤处理这个问题。",
        "代表性实验支持了论文的主要主张，但不能说明所有场景。",
        "最后需要注意适用条件，这项工作仍然存在明确边界。",
    ]
    return PaperScript.model_validate({
        "title": "测试视频",
        "plan": {
            "video_title": "测试视频",
            "core_message": "论文提出方法并用实验验证，同时存在边界。",
            "central_question": "怎样解决这个研究问题？",
            "story_spine": ["问题", "方法", "证据", "边界"],
            "opening_hook": "从实际问题开始",
            "chapters": planned_chapters,
        },
        "chapters": [
            {
                "chapter_id": f"chapter_{index:02d}",
                "title": name,
                "segments": [{"page": index, "text": narration[index - 1]}],
            }
            for index, name in enumerate(chapter_names, start=1)
        ],
    })


def _paper_script_audit(
    script: PaperScript,
    unsupported_first_claim: bool = False,
) -> PaperScriptAudit:
    chapters = []
    for index, chapter in enumerate(script.chapters, start=1):
        if index == 1 and unsupported_first_claim:
            claim = {
                "claim": chapter.segments[0].text,
                "verdict": "unsupported",
                "evidence_type": "none",
                "evidence_pages": [],
                "evidence": None,
                "issue": "指定来源页没有支持这一说法。",
                "severity": "high",
                "suggested_revision": None,
            }
        else:
            claim = {
                "claim": chapter.segments[0].text,
                "verdict": "supported",
                "evidence_type": "direct",
                "evidence_pages": [index],
                "evidence": "来源页直接支持。",
                "issue": None,
                "severity": "none",
                "suggested_revision": None,
            }
        chapters.append({
            "chapter_id": chapter.chapter_id,
            "source_pages": [index],
            "segments": [{"segment_index": 1, "claims": [claim]}],
        })

    unsupported = 1 if unsupported_first_claim else 0
    return PaperScriptAudit.model_validate({
        "script_title": script.title,
        "chapters": chapters,
        "summary": {
            "total_segments": 4,
            "total_claims": 4,
            "supported": 4 - unsupported,
            "partially_supported": 0,
            "unsupported": unsupported,
            "conflicting": 0,
            "not_verifiable": 0,
            "high_severity_issues": unsupported,
        },
    })


def _replace_segment_text(
    script: PaperScript,
    chapter_index: int,
    text: str,
) -> PaperScript:
    chapters = list(script.chapters)
    chapter = chapters[chapter_index]
    segment = chapter.segments[0].model_copy(update={"text": text})
    chapters[chapter_index] = chapter.model_copy(update={"segments": [segment]})
    return script.model_copy(update={"chapters": chapters})


def test_clean_script_passes_without_issues() -> None:
    script = _paper_script()

    issues, metrics = validate_final_script(
        script,
        script,
        _paper_script_audit(script),
    )

    assert issues == []
    assert metrics.chapter_count == 4
    assert metrics.segment_count == 4
    assert metrics.numeric_dense_segments == 0


def test_validation_detects_duplicates_and_numeric_density() -> None:
    raw_script = _paper_script()
    duplicate_text = raw_script.chapters[0].segments[0].text
    candidate = _replace_segment_text(raw_script, 1, duplicate_text)
    candidate = _replace_segment_text(
        candidate,
        2,
        "四组结果分别是 10%、20%、30% 和 40%。",
    )

    issues, metrics = validate_final_script(
        candidate,
        raw_script,
        _paper_script_audit(raw_script),
    )
    codes = {issue.code for issue in issues}

    assert "exact_duplicate" in codes
    assert "numeric_density" in codes
    assert "unapproved_number" in codes
    assert metrics.exact_duplicate_pairs == 1
    assert metrics.numeric_dense_segments == 1


def test_validation_detects_audited_problem_left_verbatim() -> None:
    script = _paper_script()

    issues, _ = validate_final_script(
        script,
        script,
        _paper_script_audit(script, unsupported_first_claim=True),
    )

    assert any(issue.code == "audited_claim_retained" for issue in issues)


def test_finalize_does_not_call_repair_for_clean_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _paper_script()

    def unexpected_repair(*args, **kwargs):
        raise AssertionError("clean script must not call the LLM repair node")

    monkeypatch.setattr(
        "paper_video_agent.finalizer.generate_repaired_script",
        unexpected_repair,
    )
    final_script, report = finalize_script(
        script,
        script,
        _paper_script_audit(script),
    )

    assert final_script == script
    assert report.passed is True
    assert report.repair_rounds == 0


def test_finalize_repairs_only_after_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_script = _paper_script()
    invalid_candidate = _replace_segment_text(
        raw_script,
        0,
        raw_script.chapters[0].segments[0].text + "结果达到 99%。",
    )
    calls = []

    def fake_repair(candidate, raw_script, audit, issues):
        calls.append([issue.code for issue in issues])
        return raw_script

    monkeypatch.setattr(
        "paper_video_agent.finalizer.generate_repaired_script",
        fake_repair,
    )
    final_script, report = finalize_script(
        invalid_candidate,
        raw_script,
        _paper_script_audit(raw_script),
    )

    assert final_script == raw_script
    assert calls == [["unapproved_number"]]
    assert report.passed is True
    assert report.repair_rounds == 1
    assert report.initial_issues[0].code == "unapproved_number"


def test_finalize_stops_when_issues_remain() -> None:
    raw_script = _paper_script()
    invalid_candidate = _replace_segment_text(
        raw_script,
        0,
        raw_script.chapters[0].segments[0].text + "结果达到 99%。",
    )

    with pytest.raises(ScriptFinalizationError) as raised:
        finalize_script(
            invalid_candidate,
            raw_script,
            _paper_script_audit(raw_script),
            max_repair_rounds=0,
        )

    assert raised.value.report.passed is False
    assert raised.value.report.final_issues[0].code == "unapproved_number"


def test_medium_warnings_do_not_repair_or_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_script = _paper_script()
    raw_script = _replace_segment_text(
        raw_script,
        2,
        "四组结果分别是 10%、20%、30% 和 40%。",
    )
    candidate = raw_script

    def unexpected_repair(*args, **kwargs):
        raise AssertionError("medium warnings must not call the repair node")

    monkeypatch.setattr(
        "paper_video_agent.finalizer.generate_repaired_script",
        unexpected_repair,
    )
    final_script, report = finalize_script(
        candidate,
        raw_script,
        _paper_script_audit(raw_script),
    )

    assert final_script == candidate
    assert report.passed is True
    assert report.repair_rounds == 0
    assert [issue.code for issue in report.final_issues] == ["numeric_density"]


def test_finalization_cache_round_trip(tmp_path: Path) -> None:
    script = _paper_script()
    audit = _paper_script_audit(script)
    final_script, report = finalize_script(script, script, audit)
    cache = build_final_cache_metadata(script, audit, script)
    script_path = tmp_path / "paper_script.final.json"
    report_path = tmp_path / "script_validation.json"
    cache_path = tmp_path / "paper_script.final.cache.json"

    save_finalization(
        final_script,
        report,
        script_path,
        report_path,
        cache_path,
        cache,
    )

    assert load_cached_finalization(
        script_path,
        report_path,
        cache_path,
        cache,
    ) == (final_script, report)
    assert load_cached_finalization(
        script_path,
        report_path,
        cache_path,
        {**cache, "fingerprint": "changed"},
    ) is None
