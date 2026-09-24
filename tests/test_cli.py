import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from paper_video_agent.models import PaperScript
from paper_video_agent.paper2video import (
    build_all_segment_videos,
    build_script_cache_metadata,
    build_segment_video,
    build_subtitles,
    create_argument_parser,
    default_output_dir,
    enrich_manifest_timeline,
    escape_drawtext_text,
    format_srt_time,
    generate_script_audio,
    get_video_font,
    load_cached_paper_script,
    main,
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
    "module",
    ["paper_video_agent", "paper_video_agent.social_metadata"],
)
def test_help_does_not_require_api_key(module: str, tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["DEEPSEEK_API_KEY"] = ""
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("OPENAI_ADMIN_KEY", None)

    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


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


def test_main_allows_cached_run_without_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"paper")
    called = {}

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("paper_video_agent.paper2video.missing_external_tools", lambda: [])
    monkeypatch.setattr(
        "paper_video_agent.paper2video.build_video",
        lambda **kwargs: called.update(kwargs),
    )

    main(["--pdf", str(pdf_path)])

    assert called["pdf_path"] == pdf_path.resolve()


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


def test_segment_video_uses_only_full_pdf_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def capture_command(command: list[str], cwd: Path | None = None) -> None:
        captured["command"] = command
        captured["cwd"] = cwd

    monkeypatch.setattr("paper_video_agent.paper2video.run_cmd", capture_command)
    build_segment_video(
        image_path=tmp_path / "page_001.png",
        audio_path=tmp_path / "segment_001.mp3",
        subtitle_path=tmp_path / "segment_001.srt",
        output_path=tmp_path / "segment_001.mp4",
        chapters=[{"index": 1, "title": "开场"}],
        current_chapter_index=1,
        video_elapsed=0,
        segment_duration=3,
        video_duration=3,
    )

    command = captured["command"]
    assert command.count("-i") == 2
    assert "-vf" in command
    assert "-filter_complex" not in command
    assert str((tmp_path / "page_001.png").resolve()) in command


def test_segment_video_switches_to_focus_asset_and_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    focus_path = tmp_path / "figure.png"
    focus_path.write_bytes(b"image")

    def capture_command(command: list[str], cwd: Path | None = None) -> None:
        captured["command"] = command
        captured["cwd"] = cwd

    monkeypatch.setattr("paper_video_agent.paper2video.run_cmd", capture_command)
    build_segment_video(
        image_path=tmp_path / "page_001.png",
        audio_path=tmp_path / "segment_001.mp3",
        subtitle_path=tmp_path / "segment_001.srt",
        output_path=tmp_path / "segment_001.mp4",
        chapters=[{"index": 1, "title": "开场"}],
        current_chapter_index=1,
        video_elapsed=0,
        segment_duration=8,
        video_duration=8,
        focus_cues=[{
            "visual_id": "page_001_image_01",
            "start": 2,
            "end": 6,
            "image_path": str(focus_path),
        }],
    )

    command = captured["command"]
    assert command.count("-i") == 3
    assert "-filter_complex" in command
    graph = command[command.index("-filter_complex") + 1]
    assert "overlay=0:0" in graph
    assert "gte(t,2.000)*lt(t,6.000)" in graph
    assert str(focus_path.resolve()) in command


def test_segment_videos_resume_after_partial_ffmpeg_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_dir = tmp_path / "audio"
    image_dir = tmp_path / "images"
    subtitle_dir = tmp_path / "subtitles"
    output_dir = tmp_path / "output"
    for directory in (audio_dir, image_dir, subtitle_dir):
        directory.mkdir()
    for index in (1, 2):
        (audio_dir / f"segment_{index:03d}.mp3").write_bytes(f"audio {index}".encode())
        (subtitle_dir / f"segment_{index:03d}.srt").write_text(
            f"subtitle {index}",
            encoding="utf-8",
        )
    (image_dir / "page_001.png").write_bytes(b"image")

    manifest = {
        "duration": 2.0,
        "chapters": [{"index": 1, "title": "chapter"}],
        "segments": [
            {
                "index": index,
                "page": 1,
                "chapter_index": 1,
                "start_time": float(index - 1),
                "duration": 1.0,
            }
            for index in (1, 2)
        ],
    }
    monkeypatch.setattr(
        "paper_video_agent.paper2video.load_audio_timeline",
        lambda *_args: manifest,
    )

    first_attempt = []

    def fail_second_segment(**kwargs) -> None:
        index = int(kwargs["audio_path"].stem.rsplit("_", 1)[1])
        first_attempt.append(index)
        if index == 2:
            raise RuntimeError("ffmpeg failed")
        kwargs["output_path"].write_bytes(b"video 1")

    monkeypatch.setattr(
        "paper_video_agent.paper2video.build_segment_video",
        fail_second_segment,
    )
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        build_all_segment_videos(
            tmp_path / "manifest.json",
            audio_dir,
            image_dir,
            subtitle_dir,
            output_dir,
            video_concurrency=1,
        )

    assert first_attempt == [1, 2]

    resumed = []

    def finish_render(**kwargs) -> None:
        index = int(kwargs["audio_path"].stem.rsplit("_", 1)[1])
        resumed.append(index)
        kwargs["output_path"].write_bytes(f"video {index}".encode())

    monkeypatch.setattr(
        "paper_video_agent.paper2video.build_segment_video",
        finish_render,
    )
    build_all_segment_videos(
        tmp_path / "manifest.json",
        audio_dir,
        image_dir,
        subtitle_dir,
        output_dir,
        video_concurrency=1,
    )

    assert resumed == [2]

    resumed.clear()
    (subtitle_dir / "segment_002.srt").write_text("changed", encoding="utf-8")
    build_all_segment_videos(
        tmp_path / "manifest.json",
        audio_dir,
        image_dir,
        subtitle_dir,
        output_dir,
        video_concurrency=1,
    )

    assert resumed == [2]


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
            "segments": [{
                "page": 1,
                "text": "测试口播",
                "visual_id": "page_001_image_01",
            }],
        }],
        "visuals": [{
            "id": "page_001_image_01",
            "type": "image",
            "page": 1,
            "caption": "Figure 1",
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


def test_reused_tts_refreshes_chapter_metadata(tmp_path: Path) -> None:
    script = _paper_script()
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "segment_001.mp3").write_bytes(b"existing audio")
    (audio_dir / "audio_manifest.json").write_text(
        json.dumps({
            "tts": {
                "backend": "edge",
                "voice": "zh-CN-XiaoxiaoNeural",
                "rate": "+25%",
                "pitch": "+0Hz",
            },
            "segments": [{
                "index": 1,
                "chapter_id": "old_chapter",
                "chapter_index": 9,
                "chapter_count": 9,
                "chapter_title": "旧标题",
                "chapter_segment_index": 9,
                "chapter_segment_count": 9,
                "page": 1,
                "text": "测试口播",
                "audio_file": "segment_001.mp3",
                "words": [{"text": "测试", "start": 0.0, "end": 1.0}],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    manifest = asyncio.run(generate_script_audio(script, audio_dir))

    assert manifest["segments"][0]["chapter_id"] == "chapter_01"
    assert manifest["segments"][0]["chapter_index"] == 1
    assert manifest["segments"][0]["chapter_count"] == 1
    assert manifest["segments"][0]["chapter_title"] == "章节1"
    assert manifest["segments"][0]["visual_id"] == "page_001_image_01"
