import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from paper_video_agent.models import (
    ChapterVisualReview,
    PaperScript,
    PaperVisual,
)
from paper_video_agent.visual import (
    _merge_nearby_same_visual_cues,
    _validate_review,
    align_visual_review,
    generate_visual_review,
    prepare_visual_assets,
)


def _asset(image_path: Path) -> dict:
    return {
        "id": "page_002_table_01",
        "type": "table",
        "page": 2,
        "caption": "Table 1",
        "asset_path": "images/table.png",
        "bbox": [100, 100, 900, 500],
        "image_path": str(image_path),
        "image_sha256": "sha256",
    }


def _cue(start: str, end: str = "") -> dict:
    return {
        "visual_id": "page_002_table_01",
        "start_quote": start,
        "end_quote": end,
        "reason": "解释表格比较",
    }


def _align(tmp_path: Path, text: str, words: list[dict], cues: list[dict]) -> dict:
    image_path = tmp_path / "table.png"
    image_path.write_bytes(b"image")
    return align_visual_review(
        {"segments": [{"segment_index": 1, "cues": cues}]},
        {"segments": [{
            "index": 1,
            "text": text,
            "words": words,
            "duration": 12,
        }]},
        {"page_002_table_01": _asset(image_path)},
        tmp_path / "timeline.json",
    )


def test_visual_timeline_uses_quote_anchors_and_word_timestamps(
    tmp_path: Path,
) -> None:
    result = _align(
        tmp_path,
        "先看。GPT-5：结果很好。然后总结。",
        [
            {"text": "先看", "start": 0, "end": 1},
            {"text": "GPT", "start": 2, "end": 3},
            {"text": "5", "start": 3, "end": 4},
            {"text": "结果很好", "start": 4, "end": 7},
            {"text": "然后总结", "start": 8, "end": 10},
        ],
        [_cue("GPT-5", "结果很好")],
    )

    cue = result["segments"][0]["cues"][0]
    assert (cue["start"], cue["end"]) == (2, 7)
    assert cue["page"] == 2
    assert result["warnings"] == []


def test_visual_timeline_rejects_ambiguous_or_short_focus(
    tmp_path: Path,
) -> None:
    ambiguous = _align(
        tmp_path,
        "结果很好，结果很好",
        [{"text": "结果很好结果很好", "start": 0, "end": 10}],
        [_cue("结果很好")],
    )
    assert ambiguous["segments"][0]["cues"] == []

    too_short = _align(
        tmp_path,
        "现在看表，然后总结",
        [
            {"text": "现在看表", "start": 0, "end": 1},
            {"text": "然后总结", "start": 1, "end": 3},
        ],
        [_cue("现在看表", "现在看表")],
    )
    assert too_short["segments"][0]["cues"] == []


def test_visual_timeline_merges_nearby_cues_for_same_visual(
    tmp_path: Path,
) -> None:
    result = _align(
        tmp_path,
        "先讲表格甲，然后短暂补充，再讲表格乙，最后总结",
        [
            {"text": "先讲表格甲", "start": 0, "end": 3},
            {"text": "然后短暂补充", "start": 3, "end": 6},
            {"text": "再讲表格乙", "start": 6, "end": 9},
            {"text": "最后总结", "start": 9, "end": 12},
        ],
        [
            _cue("先讲表格甲", "先讲表格甲"),
            _cue("再讲表格乙", "再讲表格乙"),
        ],
    )

    assert result["segments"][0]["cues"] == [{
        **_cue("先讲表格甲", "再讲表格乙"),
        "start": 0,
        "end": 9,
        "image_path": str(tmp_path / "table.png"),
        "page": 2,
        "merged_cue_count": 2,
    }]


def test_same_visual_merge_bridges_segment_boundary() -> None:
    cue = {
        **_cue("开始"),
        "image_path": "table.png",
        "page": 2,
    }
    segments = [
        {
            "segment_index": 1,
            "start_time": 0,
            "duration": 6,
            "cues": [{**cue, "start": 1, "end": 5}],
        },
        {
            "segment_index": 2,
            "start_time": 6,
            "duration": 6,
            "cues": [{**cue, "start": 2, "end": 5}],
        },
    ]

    _merge_nearby_same_visual_cues(segments, max_gap_seconds=5)

    assert [(item["start"], item["end"]) for item in segments[0]["cues"]] == [(1, 6)]
    assert [(item["start"], item["end"]) for item in segments[1]["cues"]] == [(0, 5)]
    assert segments[0]["cues"][0]["merged_cue_count"] == 2
    assert segments[1]["cues"][0]["merged_cue_count"] == 2


def test_same_visual_merge_keeps_long_gaps_separate() -> None:
    cue = {
        **_cue("开始"),
        "image_path": "table.png",
        "page": 2,
    }
    segments = [{
        "segment_index": 1,
        "start_time": 0,
        "duration": 15,
        "cues": [
            {**cue, "start": 0, "end": 3},
            {**cue, "start": 9, "end": 12},
        ],
    }]

    _merge_nearby_same_visual_cues(segments, max_gap_seconds=5)

    assert [(item["start"], item["end"]) for item in segments[0]["cues"]] == [
        (0, 3),
        (9, 12),
    ]


def test_same_visual_merge_does_not_project_single_cue_across_rounding_boundary() -> None:
    cue = {
        **_cue("开始"),
        "image_path": "table.png",
        "page": 2,
        "start": 1,
        "end": 6,
    }
    segments = [
        {"segment_index": 1, "start_time": 0, "duration": 6, "cues": [cue]},
        {"segment_index": 2, "start_time": 5.999, "duration": 6, "cues": []},
    ]

    _merge_nearby_same_visual_cues(segments, max_gap_seconds=5)

    assert segments[0]["cues"] == [cue]
    assert segments[1]["cues"] == []


def test_visual_review_rejects_unknown_overlap_and_duplicate_segment() -> None:
    segments = [{"index": 1, "text": "甲乙丙丁戊己庚辛"}]
    allowed = {"page_002_table_01"}
    reviews = [
        [{
            "segment_index": 1,
            "cues": [{**_cue("甲乙"), "visual_id": "unknown"}],
        }],
        [{
            "segment_index": 1,
            "cues": [_cue("甲乙", "戊己"), _cue("丙丁")],
        }],
        [
            {"segment_index": 1, "cues": []},
            {"segment_index": 1, "cues": []},
        ],
    ]

    for items in reviews:
        with pytest.raises(ValueError):
            _validate_review(
                ChapterVisualReview.model_validate({"segments": items}),
                segments,
                allowed,
            )


def test_prepare_visual_assets_uses_mineru_image_and_bbox_crop(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((120, 220), "E = mc^2", fontsize=30)
    document.save(pdf_path)
    page_pixmap = page.get_pixmap(matrix=pymupdf.Matrix(1, 1), alpha=False)
    document.close()

    mineru_dir = tmp_path / "mineru"
    (mineru_dir / "images").mkdir(parents=True)
    table_path = mineru_dir / "images" / "table.png"
    page_pixmap.save(table_path)
    script = SimpleNamespace(visuals=[
        PaperVisual(
            id="page_001_table_01",
            type="table",
            page=1,
            caption="Table 1",
            asset_path="images/table.png",
            bbox=[100, 100, 900, 500],
        ),
        PaperVisual(
            id="page_001_formula_01",
            type="formula",
            page=1,
            caption="E = mc^2",
            bbox=[150, 200, 500, 350],
        ),
    ])

    assets = prepare_visual_assets(
        script,
        mineru_dir,
        pdf_path,
        tmp_path / "focus_assets",
    )

    assert set(assets) == {"page_001_table_01", "page_001_formula_01"}
    formula_path = Path(assets["page_001_formula_01"]["image_path"])
    assert formula_path.is_file()
    formula_pixmap = pymupdf.Pixmap(formula_path)
    assert formula_pixmap.width > 100
    assert formula_pixmap.height > 100


def _script_with_visual(image_path: Path) -> tuple[PaperScript, dict[str, dict]]:
    planned_chapters = [
        {
            "chapter_id": f"chapter_{index:02d}",
            "title": f"章节{index}",
            "narrative_goal": "讲清楚问题",
            "guiding_question": "为什么？",
            "key_points": ["关键点"],
            "takeaway": "核心结论",
            "source_pages": [2],
            "transition_goal": "引出下一部分",
        }
        for index in range(1, 5)
    ]
    script = PaperScript.model_validate({
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
            "segments": [{
                "page": 2,
                "text": "现在看表格中的结果，然后回到结论。",
                "visual_id": "page_002_table_01",
            }],
        }],
        "visuals": [{
            "id": "page_002_table_01",
            "type": "table",
            "page": 2,
            "caption": "Table 1",
            "asset_path": "images/table.png",
        }],
    })
    return script, {"page_002_table_01": _asset(image_path)}


def test_visual_review_reuses_saved_user_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "table.png"
    image_path.write_bytes(b"image")
    script, assets = _script_with_visual(image_path)
    model_review = ChapterVisualReview.model_validate({
        "segments": [{
            "segment_index": 1,
            "cues": [_cue("现在看表格", "表格中的结果")],
        }],
    })
    monkeypatch.setattr(
        "paper_video_agent.visual._get_visual_review_chain",
        lambda: object(),
    )
    calls = 0

    def invoke(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return model_review

    monkeypatch.setattr(
        "paper_video_agent.visual.invoke_structured_with_retry",
        invoke,
    )
    output_path = tmp_path / "visual_review.json"
    pages = [{"page": 2, "text": "Table 1 results"}]

    generate_visual_review(script, pages, assets, output_path)
    assert calls == 1
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    saved["segments"][0]["cues"] = []
    output_path.write_text(json.dumps(saved), encoding="utf-8")

    result = generate_visual_review(script, pages, assets, output_path)
    assert calls == 1
    assert result["segments"][0]["cues"] == []
