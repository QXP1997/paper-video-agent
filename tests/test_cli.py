from pathlib import Path

import pytest

from paper_video_agent.models import PaperScript
from paper_video_agent.paper2video import (
    build_script_cache_metadata,
    build_subtitles,
    create_argument_parser,
    default_output_dir,
    enrich_manifest_timeline,
    escape_drawtext_text,
    format_srt_time,
    get_video_font,
    load_cached_paper_script,
    missing_external_tools,
    save_paper_script,
    write_segment_srt,
)


def test_default_output_dir_is_next_to_pdf() -> None:
    pdf_path = Path("papers") / "example.pdf"

    assert default_output_dir(pdf_path) == Path("papers") / "example_output"


def test_pdf_argument_is_required() -> None:
    parser = create_argument_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "00:00:00,000"),
        (1.234, "00:00:01,234"),
        (3661.005, "01:01:01,005"),
    ],
)
def test_format_srt_time(seconds: float, expected: str) -> None:
    assert format_srt_time(seconds) == expected


def test_escape_drawtext_text() -> None:
    assert escape_drawtext_text("50%: it's") == r"50\%\: it\'s"


def test_configured_font_path_must_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_VIDEO_FONT_PATH", "missing-font-file.ttf")

    with pytest.raises(FileNotFoundError, match="字体不存在"):
        get_video_font()


def test_missing_external_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "paper_video_agent.paper2video.shutil.which",
        lambda name: "ffmpeg.exe" if name == "ffmpeg" else None,
    )

    assert missing_external_tools() == ["ffprobe"]


def test_enrich_manifest_timeline() -> None:
    manifest = {
        "chapters": [
            {"index": 1, "title": "开场"},
            {"index": 2, "title": "结论"},
        ],
        "segments": [
            {
                "chapter_index": 1,
                "words": [{"text": "一", "start": 0.0, "end": 1.25}],
            },
            {
                "chapter_index": 2,
                "duration": 2.5,
                "words": [],
            },
        ],
    }

    result = enrich_manifest_timeline(manifest)

    assert result["duration"] == 3.75
    assert result["segments"][1]["start_time"] == 1.25
    assert result["chapters"][1] == {
        "index": 2,
        "title": "结论",
        "start_time": 1.25,
        "duration": 2.5,
        "end_time": 3.75,
    }


def test_build_subtitles_preserves_timing_and_wraps() -> None:
    words = [
        {"text": char, "start": index * 0.1, "end": (index + 1) * 0.1}
        for index, char in enumerate("这是一个字幕测试")
    ]

    subtitles = build_subtitles(words, max_chars=4, min_chars=3)

    assert subtitles[0]["start"] == 0.0
    assert subtitles[-1]["end"] == pytest.approx(0.8)
    assert all(
        len(line) <= 4
        for subtitle in subtitles
        for line in subtitle["text"].splitlines()
    )


def test_write_segment_srt(tmp_path: Path) -> None:
    output_path = tmp_path / "segment.srt"
    segment = {
        "text": "你好。",
        "words": [
            {"text": "你", "start": 0.0, "end": 0.2},
            {"text": "好。", "start": 0.2, "end": 0.5},
        ],
    }

    write_segment_srt(segment, output_path)

    content = output_path.read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:00,500" in content
    assert "你好。" in content


def _paper_script() -> PaperScript:
    planned_chapters = [
        {
            "chapter_id": f"chapter_{index:02d}",
            "title": f"章节{index}",
            "narrative_goal": "讲清楚问题",
            "guiding_question": "为什么？",
            "key_points": ["关键点"],
            "takeaway": "核心结论",
            "source_pages": [1],
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
            "segments": [{"page": 1, "text": "测试口播"}],
        }],
    })


def test_script_cache_tracks_pdf_but_ignores_downstream_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"first revision")

    original = build_script_cache_metadata(pdf_path)
    monkeypatch.setenv("PAPER_VIDEO_TTS_VOICE", "another-voice")
    monkeypatch.setenv("PAPER_VIDEO_FONT_NAME", "another-font")

    assert build_script_cache_metadata(pdf_path) == original

    pdf_path.write_bytes(b"second revision")
    assert build_script_cache_metadata(pdf_path)["fingerprint"] != original["fingerprint"]


def test_paper_script_is_reused_only_with_matching_fingerprint(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"paper")
    script_path = tmp_path / "paper_script.json"
    cache_path = tmp_path / "paper_script.cache.json"
    cache = build_script_cache_metadata(pdf_path)
    script = _paper_script()

    save_paper_script(script, script_path, cache_path, cache)

    assert load_cached_paper_script(script_path, cache_path, cache) == script

    changed_cache = {**cache, "fingerprint": "different"}
    assert load_cached_paper_script(script_path, cache_path, changed_cache) is None
