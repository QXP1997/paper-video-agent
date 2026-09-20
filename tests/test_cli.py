from pathlib import Path

import pytest

from paper_video_agent.paper2video import (
    build_subtitles,
    create_argument_parser,
    default_output_dir,
    enrich_manifest_timeline,
    escape_drawtext_text,
    format_srt_time,
    get_video_font,
    missing_external_tools,
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

