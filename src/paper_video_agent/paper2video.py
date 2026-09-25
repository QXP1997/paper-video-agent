import argparse
import asyncio
import json
import math
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from paper_video_agent import __version__
from paper_video_agent.audit import (
    build_audit_cache_metadata,
    generate_script_fact_audit,
    load_cached_script_audit,
    save_script_audit,
)
from paper_video_agent.chat import generate_paper_script, script_generation_cache_material
from paper_video_agent.editor import (
    build_editor_cache_metadata,
    generate_edited_script,
    load_cached_edited_script,
    save_edited_script,
)
from paper_video_agent.finalizer import (
    ScriptFinalizationError,
    build_final_cache_metadata,
    finalize_script,
    load_cached_finalization,
    save_finalization,
    validate_final_script,
)
from paper_video_agent.models import PaperScript
from paper_video_agent.pdf_util import parse_pdf
from paper_video_agent.tts import generate_tts, get_tts_config
from paper_video_agent.visual import (
    align_visual_review,
    generate_visual_review,
    prepare_visual_assets,
)
from research_agent_core.artifacts import (
    canonical_sha256 as _canonical_sha256,
)
from research_agent_core.artifacts import (
    sha256_file as _sha256_file,
)
from research_agent_core.artifacts import (
    write_json_atomic as _write_json_atomic,
)
from research_video_core.subtitles import (
    build_subtitles,
    format_srt_time,
    generate_segment_srts,
    write_segment_srt,
)
from research_video_core.timeline import enrich_manifest_timeline

__all__ = [
    "build_subtitles",
    "enrich_manifest_timeline",
    "format_srt_time",
    "generate_segment_srts",
    "write_segment_srt",
]

SCRIPT_CACHE_VERSION = 2
SEGMENT_VIDEO_CACHE_VERSION = 2
SEGMENT_VIDEO_RENDER_SETTINGS = {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "video_codec": "libx264",
    "preset": "medium",
    "crf": 20,
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "audio_bitrate": "192k",
    "focus_horizontal_margin": 32,
    "focus_top_margin": 88,
    "focus_bottom_margin": 280,
}


class MissingDeepSeekAPIKeyError(RuntimeError):
    """Raised only when an uncached LLM stage needs DeepSeek."""


def require_deepseek_api_key(stage: str) -> None:
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        raise MissingDeepSeekAPIKeyError(
            f"{stage}没有可复用缓存，需要配置 DEEPSEEK_API_KEY；"
            "请复制 .env.example 为 .env 后填写"
        )


def build_script_cache_metadata(pdf_path: str | Path) -> dict:
    """Describe only the inputs that can change ``paper_script.json``."""
    pdf_path = Path(pdf_path)
    generation_material = script_generation_cache_material()
    inputs = {
        "pdf_sha256": _sha256_file(pdf_path),
        "script_generation_sha256": _canonical_sha256(generation_material),
    }
    return {
        "version": SCRIPT_CACHE_VERSION,
        "fingerprint": _canonical_sha256(inputs),
        "inputs": inputs,
    }


def load_cached_paper_script(
    script_path: str | Path,
    cache_path: str | Path,
    expected_cache: dict,
) -> PaperScript | None:
    script_path = Path(script_path)
    cache_path = Path(cache_path)
    if not script_path.is_file() or not cache_path.is_file():
        return None

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            cache.get("version") != SCRIPT_CACHE_VERSION
            or cache.get("fingerprint") != expected_cache["fingerprint"]
        ):
            return None
        return PaperScript.model_validate_json(script_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def save_paper_script(
    _script: PaperScript,
    output_path: str | Path,
    cache_path: str | Path | None = None,
    cache_metadata: dict | None = None,
) -> None:
    output_path = Path(output_path)
    _write_json_atomic(output_path, _script.model_dump())
    if cache_path is not None and cache_metadata is not None:
        _write_json_atomic(Path(cache_path), cache_metadata)

# 生成视频segment.mp3以及audio_manifest.json
async def generate_script_audio(
    _script: PaperScript,
    output_dir: str | Path,
    tts_concurrency: int = 4,
):
    output_dir = Path(output_dir)

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest_path = output_dir / "audio_manifest.json"
    existing_manifest = None

    if manifest_path.exists():
        try:
            existing_manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            existing_manifest = None

    tts_config = get_tts_config()
    existing_tts_config = (existing_manifest or {}).get("tts")
    # Manifests created before the backend metadata was added are known to
    # have used the original Edge defaults. Do not reuse them after switching
    # to another provider or voice.
    legacy_edge_config = {
        "backend": "edge",
        "voice": "zh-CN-XiaoxiaoNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
    }
    can_reuse_tts = (
        existing_tts_config == tts_config
        or (
            existing_tts_config is None
            and tts_config == legacy_edge_config
        )
    )

    reusable_segments = {
        int(segment["index"]): segment
        for segment in (existing_manifest or {}).get("segments", [])
    }

    manifest = {
        "title": _script.title,
        "tts": tts_config,
        "chapters": [],
        "segments": [],
    }

    total_segments = sum(
        len(chapter.segments)
        for chapter in _script.chapters
    )
    segment_index = 0
    chapter_count = len(_script.chapters)

    chapter_segment_cursor = 1
    for chapter_index, chapter in enumerate(
        _script.chapters,
        start=1,
    ):
        chapter_segment_count = len(chapter.segments)
        manifest["chapters"].append({
            "chapter_id": chapter.chapter_id,
            "index": chapter_index,
            "title": chapter.title,
            "segment_start": chapter_segment_cursor,
            "segment_end": (
                chapter_segment_cursor
                + chapter_segment_count
                - 1
            ),
        })
        chapter_segment_cursor += chapter_segment_count

    segment_specs = []
    for chapter_index, chapter in enumerate(
        _script.chapters,
        start=1,
    ):
        chapter_segment_count = len(chapter.segments)
        for chapter_segment_index, segment in enumerate(
            chapter.segments,
            start=1,
        ):
            segment_index += 1
            audio_name = f"segment_{segment_index:03d}.mp3"
            audio_path = output_dir / audio_name
            reusable = reusable_segments.get(segment_index)
            segment_specs.append({
                "index": segment_index,
                "chapter": chapter,
                "chapter_index": chapter_index,
                "chapter_segment_index": chapter_segment_index,
                "chapter_segment_count": chapter_segment_count,
                "segment": segment,
                "audio_name": audio_name,
                "audio_path": audio_path,
                "reusable": reusable,
            })

    semaphore = asyncio.Semaphore(max(1, int(tts_concurrency)))

    async def generate_one(spec: dict) -> dict:
        index = spec["index"]
        chapter = spec["chapter"]
        segment = spec["segment"]
        _audio_path = spec["audio_path"]
        _reusable = spec["reusable"]
        can_reuse = (
            can_reuse_tts
            and _reusable is not None
            and _reusable.get("text") == segment.text
            and int(_reusable.get("page", -1)) == segment.page
            and _reusable.get("words")
            and _audio_path.exists()
            and _audio_path.stat().st_size > 0
        )

        if can_reuse:
            print(
                f"复用 TTS: {index}/{total_segments} "
                f"[{chapter.title} "
                f"{spec['chapter_segment_index']}/"
                f"{spec['chapter_segment_count']}]"
            )
            return {
                **_reusable,
                "index": index,
                "chapter_id": chapter.chapter_id,
                "chapter_index": spec["chapter_index"],
                "chapter_count": chapter_count,
                "chapter_title": chapter.title,
                "chapter_segment_index": spec["chapter_segment_index"],
                "chapter_segment_count": spec["chapter_segment_count"],
                "page": segment.page,
                "visual_id": segment.visual_id,
                "text": segment.text,
                "tts": tts_config,
                "audio_file": spec["audio_name"],
            }

        async with semaphore:
            print(
                f"生成 TTS: {index}/{total_segments} "
                f"[{chapter.title} "
                f"{spec['chapter_segment_index']}/"
                f"{spec['chapter_segment_count']}]"
            )
            words = await generate_tts(
                text=segment.text,
                output_mp3=_audio_path,
                backend=tts_config["backend"],
                voice=tts_config["voice"],
                rate=tts_config["rate"],
                pitch=tts_config["pitch"],
            )

        return {
            "index": index,
            "chapter_id": chapter.chapter_id,
            "chapter_index": spec["chapter_index"],
            "chapter_count": chapter_count,
            "chapter_title": chapter.title,
            "chapter_segment_index": spec["chapter_segment_index"],
            "chapter_segment_count": spec["chapter_segment_count"],
            "page": segment.page,
            "visual_id": segment.visual_id,
            "text": segment.text,
            "tts": tts_config,
            "audio_file": spec["audio_name"],
            "words": words,
        }

    batch_size = max(1, int(tts_concurrency))
    for batch_start in range(0, len(segment_specs), batch_size):
        batch = segment_specs[batch_start:batch_start + batch_size]
        batch_results = await asyncio.gather(
            *(generate_one(spec) for spec in batch)
        )
        manifest["segments"].extend(batch_results)
        enrich_manifest_timeline(manifest)
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return manifest


#########################合成segment中srt字幕##########################
def run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
):
    print("[RUN]", " ".join(cmd))

    subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
    )


def escape_drawtext_text(text: str) -> str:
    """Escape user/model-generated text for FFmpeg's drawtext filter."""
    return (
        text
        .replace("\\", r"\\")
        .replace("'", r"\'")
        .replace(":", r"\:")
        .replace("%", r"\%")
    )


def get_video_font() -> tuple[Path | None, str]:
    """Resolve a configurable CJK font for FFmpeg filters."""
    configured_path = os.getenv("PAPER_VIDEO_FONT_PATH", "").strip()
    configured_name = os.getenv("PAPER_VIDEO_FONT_NAME", "").strip()

    if configured_path:
        font_path = Path(configured_path).expanduser()
        if not font_path.is_file():
            raise FileNotFoundError(
                f"PAPER_VIDEO_FONT_PATH 指向的字体不存在: {font_path}"
            )
        return font_path.resolve(), configured_name or font_path.stem

    windows_font = Path("C:/Windows/Fonts/msyh.ttc")
    if windows_font.is_file():
        return windows_font, configured_name or "Microsoft YaHei"

    return None, configured_name or "Noto Sans CJK SC"


def build_chapter_navigation_filters(
    chapters: list[dict],
    current_chapter_index: int,
    video_elapsed: float,
    segment_duration: float,
    video_duration: float,
    width: int,
) -> list[str]:
    """Build a chapter progress bar that advances while the segment plays."""
    if not chapters:
        return []

    margin_x = 0
    navigation_width = width - margin_x * 2
    cell_width = navigation_width / len(chapters)
    longest_title = max(
        len(str(chapter["title"]))
        for chapter in chapters
    )
    font_size = max(
        14,
        min(22, int((cell_width - 12) / max(longest_title, 1))),
    )
    font_path, font_name = get_video_font()
    font_option = (
        f"fontfile='{escape_drawtext_text(font_path.as_posix())}'"
        if font_path is not None
        else f"font='{escape_drawtext_text(font_name)}'"
    )

    panel_y = 0
    panel_height = 88
    filters = [
        (
            f"drawbox=x=0:y={panel_y}:w={width}:h={panel_height}:"
            "color=0x232522@0.96:t=fill"
        ),
    ]

    # The light gray region is the progress itself. It fills the complete bar
    # according to elapsed time in the whole video; chapters are only markers.
    safe_video_duration = max(video_duration, 0.001)
    start_progress = min(
        1.0,
        max(0.0, video_elapsed / safe_video_duration),
    )
    end_progress = min(
        1.0,
        max(
            start_progress,
            (video_elapsed + segment_duration) / safe_video_duration,
        ),
    )
    static_end_x = margin_x + int(navigation_width * start_progress)
    dynamic_end_x = margin_x + round(navigation_width * end_progress)

    if static_end_x > margin_x:
        filters.append(
            f"drawbox=x={margin_x}:y={panel_y}:"
            f"w={static_end_x - margin_x}:h={panel_height}:"
            "color=0x747672@0.96:t=fill"
        )

    for pixel_x in range(static_end_x, dynamic_end_x):
        pixel_progress = (
            (pixel_x + 0.5 - margin_x)
            / navigation_width
        )
        activation_time = max(
            0.0,
            pixel_progress * safe_video_duration - video_elapsed,
        )
        filters.append(
            f"drawbox=x={pixel_x}:y={panel_y}:w=1:h={panel_height}:"
            "color=0x747672@0.96:t=fill:"
            f"enable='gte(t,{activation_time:.3f})'"
        )

    # Short dividers preserve the single-strip appearance from the reference.
    for boundary_index in range(1, len(chapters)):
        boundary_x = margin_x + round(boundary_index * cell_width)
        filters.append(
            f"drawbox=x={boundary_x}:y=20:w=2:h=48:"
            "color=0xA3A5A1@0.72:t=fill"
        )

    for chapter in chapters:
        chapter_index = int(chapter["index"])
        title = escape_drawtext_text(str(chapter["title"]))
        cell_x = margin_x + round((chapter_index - 1) * cell_width)
        next_cell_x = margin_x + round(chapter_index * cell_width)
        actual_cell_width = next_cell_x - cell_x

        if chapter_index < current_chapter_index:
            text_color = "0xD1D5DB"
        elif chapter_index == current_chapter_index:
            text_color = "white"
        else:
            text_color = "0xD1D5DB"

        filters.append(
            (
                "drawtext="
                f"{font_option}:"
                f"text='{title}':"
                f"fontcolor={text_color}:"
                f"fontsize={font_size}:"
                f"x={cell_x + actual_cell_width / 2}-text_w/2:"
                f"y={panel_y + panel_height / 2}-text_h/2"
            )
        )

    return filters


def build_segment_video(
    image_path: Path,
    audio_path: Path,
    subtitle_path: Path,
    output_path: Path,
    chapters: list[dict],
    current_chapter_index: int,
    video_elapsed: float,
    segment_duration: float,
    video_duration: float,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    focus_cues: list[dict] | None = None,
):
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Windows 下 subtitles filter 使用绝对路径比较麻烦，
    # 所以直接把工作目录切到 srt 所在目录
    subtitle_name = subtitle_path.name
    _, font_name = get_video_font()

    filters = [
        (
            f"scale={width}:{height}:"
            "force_original_aspect_ratio=decrease,setsar=1"
        ),
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:white",
        (
            f"subtitles={subtitle_name}:"
            "force_style='"
            f"FontName={font_name},"
            "FontSize=8,"
            "PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,"
            "BorderStyle=1,"
            "Outline=1,"
            "Shadow=0,"
            "Alignment=2,"
            "MarginV=24"
            "'"
        ),
    ]
    filters.extend(build_chapter_navigation_filters(
        chapters=chapters,
        current_chapter_index=current_chapter_index,
        video_elapsed=video_elapsed,
        segment_duration=segment_duration,
        video_duration=video_duration,
        width=width,
    ))
    vf = ",".join(filters)

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner", "-loglevel", "warning",

        # 静态 PDF 页面
        "-loop", "1",
        "-framerate", str(fps),
        "-i", str(image_path.resolve()),

        # TTS
        "-i", str(audio_path.resolve()),
    ]
    if focus_cues:
        # Each focus image becomes a complete white-backed frame. Navigation
        # and subtitles are rendered once after all timed switches.
        focus_margin_x = int(
            SEGMENT_VIDEO_RENDER_SETTINGS["focus_horizontal_margin"]
        )
        focus_top = int(SEGMENT_VIDEO_RENDER_SETTINGS["focus_top_margin"])
        focus_bottom = int(
            SEGMENT_VIDEO_RENDER_SETTINGS["focus_bottom_margin"]
        )
        content_height = height - focus_top - focus_bottom
        graph = [f"[0:v]{','.join(filters[:2])},setsar=1[page]"]
        previous = "page"
        for index, cue in enumerate(focus_cues):
            focus_path = Path(cue["image_path"])
            if not focus_path.is_file():
                raise FileNotFoundError(f"视觉聚焦素材不存在: {focus_path}")
            start = float(cue["start"])
            end = float(cue["end"])
            if not 0 <= start < end <= segment_duration + 0.001:
                raise ValueError(f"视觉聚焦时间范围无效: {start}, {end}")
            cmd.extend([
                "-loop", "1",
                "-framerate", str(fps),
                "-i", str(focus_path.resolve()),
            ])
            graph.append(
                f"[{index + 2}:v]scale={width - focus_margin_x * 2}:"
                f"{content_height}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:"
                f"{focus_top}+({content_height}-ih)/2:white,"
                f"setsar=1[asset{index}]"
            )
            label = f"view{index}"
            graph.append(
                f"[{previous}][asset{index}]overlay=0:0:"
                f"enable='gte(t,{start:.3f})*lt(t,{end:.3f})'[{label}]"
            )
            previous = label
        graph.append(f"[{previous}]{','.join(filters[2:])}[video]")
        cmd.extend([
            "-filter_complex_threads", "1",
            "-filter_complex", ";".join(graph),
            "-map", "[video]",
            "-map", "1:a:0",
        ])
    else:
        cmd.extend(["-vf", vf, "-map", "0:v:0", "-map", "1:a:0"])
    cmd.extend([
        "-r", str(fps),

        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",

        "-c:a", "aac",
        "-b:a", "192k",
        "-af", "apad",

        # 音频结束，当前 segment 就结束
        "-shortest",
        "-t", f"{segment_duration:.6f}",

        str(output_path.resolve()),
    ])

    run_cmd(
        cmd,
        cwd=subtitle_path.parent,
    )

def load_audio_timeline(manifest_path: str | Path, audio_dir: str | Path) -> dict:
    """Use real MP3 durations so segment switches/progress share one clock."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    def duration(segment):
        path = Path(audio_dir) / f"segment_{int(segment['index']):03d}.mp3"
        value = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ], text=True)
        seconds = float(value.strip())
        if not 0 < seconds < 86400:
            raise ValueError(f"无效的音频时长: {path}")
        # Round up to a complete frame for concat and progress consistency.
        return math.ceil(seconds * 30) / 30
    with ThreadPoolExecutor(max_workers=4) as executor:
        durations = list(executor.map(duration, manifest["segments"]))
    for segment, seconds in zip(manifest["segments"], durations):
        segment["duration"] = seconds
    return enrich_manifest_timeline(manifest)


def build_segment_video_cache_metadata(
    *,
    image_path: Path,
    audio_path: Path,
    subtitle_path: Path,
    chapters: list[dict],
    current_chapter_index: int,
    video_elapsed: float,
    segment_duration: float,
    video_duration: float,
    focus_cues: list[dict] | None = None,
) -> dict:
    """Fingerprint every input that can affect a rendered segment."""
    inputs = {
        "image_sha256": _sha256_file(image_path),
        "audio_sha256": _sha256_file(audio_path),
        "subtitle_sha256": _sha256_file(subtitle_path),
        "chapters": chapters,
        "current_chapter_index": current_chapter_index,
        "video_elapsed": video_elapsed,
        "segment_duration": segment_duration,
        "video_duration": video_duration,
        "focus_cues": [
            {
                **cue,
                "image_sha256": _sha256_file(Path(cue["image_path"])),
            }
            for cue in (focus_cues or [])
        ],
        "render_settings": SEGMENT_VIDEO_RENDER_SETTINGS,
    }
    return {
        "version": SEGMENT_VIDEO_CACHE_VERSION,
        "fingerprint": _canonical_sha256(inputs),
        "inputs": inputs,
    }


def is_reusable_segment_video(
    video_path: Path,
    cache_path: Path,
    expected_cache: dict,
) -> bool:
    if not video_path.is_file() or not cache_path.is_file():
        return False
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        output_size = video_path.stat().st_size
        return (
            cache.get("version") == SEGMENT_VIDEO_CACHE_VERSION
            and cache.get("fingerprint") == expected_cache["fingerprint"]
            and cache.get("output", {}).get("size") == output_size
            and output_size > 0
        )
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return False


def build_all_segment_videos(
    manifest_path: str | Path,
    audio_dir: str | Path,
    image_dir: str | Path,
    subtitle_dir: str | Path,
    output_dir: str | Path,
    video_concurrency: int = 2,
    segment_indices: list[int] | None = None,
    visual_timeline: dict | None = None,
) -> list[Path]:

    manifest_path = Path(manifest_path)
    audio_dir = Path(audio_dir)
    image_dir = Path(image_dir)
    subtitle_dir = Path(subtitle_dir)
    output_dir = Path(output_dir)

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest = load_audio_timeline(manifest_path, audio_dir)
    visual_by_index = {
        int(item["segment_index"]): item.get("cues", [])
        for item in (visual_timeline or {}).get("segments", [])
    }

    def build_one(segment: dict) -> Path:
        index = segment["index"]
        page = segment["page"]

        audio_path = (
            audio_dir
            / f"segment_{index:03d}.mp3"
        )

        subtitle_path = (
            subtitle_dir
            / f"segment_{index:03d}.srt"
        )

        image_path = (
            image_dir
            / f"page_{page:03d}.png"
        )

        video_path = (
            output_dir
            / f"segment_{index:03d}.mp4"
        )

        cache_path = video_path.with_suffix(video_path.suffix + ".cache.json")

        if not audio_path.exists():
            raise FileNotFoundError(
                f"音频不存在: {audio_path}"
            )

        if not subtitle_path.exists():
            raise FileNotFoundError(
                f"字幕不存在: {subtitle_path}"
            )

        if not image_path.exists():
            raise FileNotFoundError(
                f"PDF 页面不存在: {image_path}"
            )

        cache_metadata = build_segment_video_cache_metadata(
            image_path=image_path,
            audio_path=audio_path,
            subtitle_path=subtitle_path,
            chapters=manifest["chapters"],
            current_chapter_index=segment["chapter_index"],
            video_elapsed=float(segment["start_time"]),
            segment_duration=float(segment["duration"]),
            video_duration=float(manifest["duration"]),
            focus_cues=visual_by_index.get(index, []),
        )
        if is_reusable_segment_video(video_path, cache_path, cache_metadata):
            print(
                f"\n[{index}/{len(manifest['segments'])}] "
                f"复用分段视频: {video_path}"
            )
            return video_path

        print(
            f"\n[{index}/{len(manifest['segments'])}] "
            f"page={page}"
        )

        temporary_video_path = video_path.with_name(
            f"{video_path.stem}.tmp{video_path.suffix}"
        )
        try:
            build_segment_video(
                image_path=image_path,
                audio_path=audio_path,
                subtitle_path=subtitle_path,
                output_path=temporary_video_path,
                chapters=manifest["chapters"],
                current_chapter_index=segment["chapter_index"],
                video_elapsed=float(segment["start_time"]),
                segment_duration=float(segment["duration"]),
                video_duration=float(manifest["duration"]),
                focus_cues=visual_by_index.get(index, []),
            )
            temporary_video_path.replace(video_path)
            cache_metadata["output"] = {"size": video_path.stat().st_size}
            _write_json_atomic(cache_path, cache_metadata)
        finally:
            if temporary_video_path.exists():
                temporary_video_path.unlink()

        return video_path

    with ThreadPoolExecutor(
        max_workers=max(1, int(video_concurrency))
    ) as executor:
        # map preserves manifest order while FFmpeg jobs run concurrently.
        selected = manifest["segments"]
        if segment_indices is not None:
            requested = set(segment_indices)
            available = {s["index"] for s in selected}
            if not requested or requested - available:
                raise ValueError(f"无效的预览 segment 编号: {segment_indices}")
            selected = [s for s in selected if s["index"] in requested]
        return list(executor.map(build_one, selected))
#########################合成segment中srt字幕##########################

# 合成视频
def concat_segment_videos(
    video_files: list[Path],
    output_path: str | Path,
):
    output_path = Path(output_path)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    concat_file = (
        output_path.parent
        / f"{output_path.stem}_concat.txt"
    )

    lines = []

    def video_duration(path):
        return float(subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ], text=True).strip())

    with ThreadPoolExecutor(max_workers=4) as executor:
        durations = list(executor.map(video_duration, video_files))

    for video_path, duration in zip(video_files, durations):
        path = (
            str(video_path.resolve())
            .replace("\\", "/")
            .replace("'", r"'\''")
        )

        lines.append(
            f"file '{path}'"
        )
        # AAC padding must not accumulate between clips and shift the timeline.
        lines.append(f"duration {duration:.6f}")

    concat_file.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    cmd = [
        "ffmpeg",
        "-y",

        "-f", "concat",
        "-safe", "0",

        "-i", str(concat_file),

        # Keep video frames intact; normalize AAC timestamps across clip joins.
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "96k",
        "-af", "aresample=async=1:first_pts=0",

        "-movflags", "+faststart",

        str(output_path),
    ]

    run_cmd(cmd)


def compress_video_for_social(
    input_path: str | Path,
    output_path: str | Path,
    fps: int = 15,
    crf: int = 27,
    audio_bitrate: str = "64k",
):
    """Create a compact H.264 copy suited to mostly-static social video."""
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.is_file():
        raise FileNotFoundError(f"待压缩视频不存在: {input_path}")

    if input_path.resolve() == output_path.resolve():
        raise ValueError("压缩版输出路径不能覆盖高质量原片")

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_path.resolve()),

        # 论文页基本静止，15fps 足以保留字幕和进度条动画，
        # 同时显著减少重复画面所占空间。
        "-vf", f"fps={fps}",
        "-c:v", "libx264",
        "-preset", "medium",
        "-tune", "stillimage",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",

        # 原始 TTS 是单声道语音，不需要高码率立体声音轨。
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-ac", "1",
        "-ar", "24000",

        "-movflags", "+faststart",
        str(output_path.resolve()),
    ]

    run_cmd(cmd)


def build_video(
    paper_dir: str | Path,
    pdf_path: str | Path,
    preview_segments: list[int] | None = None,
):
    paper_dir = Path(paper_dir).expanduser().resolve()
    pdf_path = Path(pdf_path).expanduser().resolve()

    # pdf转文本和图片
    pages, _page_images = parse_pdf(
        pdf_path=pdf_path,
        output_dir=paper_dir / "images",
        zoom=2.0,
    )

    script_path = Path(paper_dir) / "output" / "paper_script.json"
    script_cache_path = Path(paper_dir) / "output" / "paper_script.cache.json"
    expected_script_cache = build_script_cache_metadata(pdf_path)

    # Only reuse a script generated from the same PDF, prompts, schemas and
    # LLM settings. TTS and rendering settings intentionally do not belong to
    # this stage's fingerprint.
    script = load_cached_paper_script(
        script_path,
        script_cache_path,
        expected_script_cache,
    )
    if script is not None:
        print(f"复用已有论文脚本: {script_path}")
    else:
        if script_path.exists():
            print("论文、模型或脚本生成规则已变化，重新生成论文脚本")
        require_deepseek_api_key("论文脚本")
        script = generate_paper_script(pages["pages"])
        save_paper_script(
            script,
            script_path,
            cache_path=script_cache_path,
            cache_metadata=expected_script_cache,
        )

    audit_path = Path(paper_dir) / "output" / "script_audit.json"
    audit_cache_path = Path(paper_dir) / "output" / "script_audit.cache.json"
    expected_audit_cache = build_audit_cache_metadata(pages["pages"], script)
    audit = load_cached_script_audit(
        audit_path,
        audit_cache_path,
        expected_audit_cache,
    )
    if audit is not None:
        print(f"复用已有事实审核: {audit_path}")
    else:
        if audit_path.exists():
            print("论文来源页、脚本或审核规则已变化，重新审核论文脚本")
        require_deepseek_api_key("事实审核")
        audit = generate_script_fact_audit(pages["pages"], script)
        save_script_audit(
            audit,
            audit_path,
            cache_path=audit_cache_path,
            cache_metadata=expected_audit_cache,
        )

    summary = audit.summary
    issue_count = (
        summary.partially_supported
        + summary.unsupported
        + summary.conflicting
        + summary.not_verifiable
    )
    print(
        f"事实审核完成: {summary.total_claims} 条陈述，"
        f"{issue_count} 条需要关注，"
        f"{summary.high_severity_issues} 条高风险"
    )

    edited_script_path = Path(paper_dir) / "output" / "paper_script.edited.json"
    editor_cache_path = (
        Path(paper_dir) / "output" / "paper_script.edited.cache.json"
    )
    expected_editor_cache = build_editor_cache_metadata(script, audit)
    edited_script = load_cached_edited_script(
        edited_script_path,
        editor_cache_path,
        expected_editor_cache,
    )
    if edited_script is not None:
        print(f"复用已有编辑稿: {edited_script_path}")
    else:
        if edited_script_path.exists():
            print("原始脚本、事实审核或编辑规则已变化，重新编辑论文脚本")
        require_deepseek_api_key("论文口播编辑")
        edited_script = generate_edited_script(script, audit)
        save_edited_script(
            edited_script,
            edited_script_path,
            cache_path=editor_cache_path,
            cache_metadata=expected_editor_cache,
        )

    raw_segment_count = sum(len(chapter.segments) for chapter in script.chapters)
    edited_segment_count = sum(
        len(chapter.segments) for chapter in edited_script.chapters
    )
    raw_character_count = sum(
        len(segment.text)
        for chapter in script.chapters
        for segment in chapter.segments
    )
    edited_character_count = sum(
        len(segment.text)
        for chapter in edited_script.chapters
        for segment in chapter.segments
    )
    print(
        f"论文口播编辑完成: {raw_segment_count} → {edited_segment_count} 段，"
        f"{raw_character_count} → {edited_character_count} 字"
    )

    final_script_path = Path(paper_dir) / "output" / "paper_script.final.json"
    validation_report_path = Path(paper_dir) / "output" / "script_validation.json"
    final_cache_path = (
        Path(paper_dir) / "output" / "paper_script.final.cache.json"
    )
    expected_final_cache = build_final_cache_metadata(
        script,
        audit,
        edited_script,
    )
    cached_finalization = load_cached_finalization(
        final_script_path,
        validation_report_path,
        final_cache_path,
        expected_final_cache,
    )
    if cached_finalization is not None:
        final_script, validation_report = cached_finalization
        print(f"复用已通过校验的最终稿: {final_script_path}")
    else:
        print("正在执行最终脚本校验...")
        initial_issues, _initial_metrics = validate_final_script(
            edited_script,
            script,
            audit,
        )
        if any(issue.severity == "high" for issue in initial_issues):
            require_deepseek_api_key("最终脚本返修")
        try:
            final_script, validation_report = finalize_script(
                edited_script,
                script,
                audit,
            )
        except ScriptFinalizationError as exc:
            save_finalization(
                None,
                exc.report,
                final_script_path,
                validation_report_path,
            )
            raise
        save_finalization(
            final_script,
            validation_report,
            final_script_path,
            validation_report_path,
            cache_path=final_cache_path,
            cache_metadata=expected_final_cache,
        )

    final_metrics = validation_report.final_metrics
    warning_count = sum(
        issue.severity != "high"
        for issue in validation_report.final_issues
    )
    print(
        f"最终脚本校验通过: {final_metrics.chapter_count} 章，"
        f"{final_metrics.segment_count} 段，"
        f"{final_metrics.character_count} 字，"
        f"返修 {validation_report.repair_rounds} 轮，"
        f"保留 {warning_count} 条非阻断提醒"
    )
    script = final_script

    # Independently review when a visual should replace the full PDF page.
    # MinerU images are used directly; bbox-only formula/code elements are
    # rendered from the source PDF into stable local focus assets.
    visual_assets = prepare_visual_assets(
        script,
        mineru_dir=paper_dir / "output" / "mineru",
        pdf_path=pdf_path,
        output_dir=paper_dir / "output" / "focus_assets",
    )
    visual_review = generate_visual_review(
        script,
        pages["pages"],
        visual_assets,
        paper_dir / "output" / "visual_review.json",
        before_model_call=require_deepseek_api_key,
    )

    # 保存segment视频片段
    tts_concurrency = max(
        1,
        int(os.getenv("PAPER_VIDEO_TTS_CONCURRENCY", "4")),
    )
    asyncio.run(
        generate_script_audio(
            script,
            paper_dir / "audio",
            tts_concurrency=tts_concurrency,
        )
    )

    audio_timeline = load_audio_timeline(
        paper_dir / "audio" / "audio_manifest.json",
        paper_dir / "audio",
    )
    visual_timeline = align_visual_review(
        visual_review,
        audio_timeline,
        visual_assets,
        paper_dir / "output" / "visual_timeline.json",
        same_visual_merge_gap_seconds=float(
            os.getenv("PAPER_VIDEO_VISUAL_MERGE_GAP_SECONDS", "5")
        ),
    )

    # 生成srt字幕
    generate_segment_srts(
        manifest_path=paper_dir / "audio" / "audio_manifest.json",
        output_dir=paper_dir / "subtitles",
        max_chars=18,
    )

    # 合成segment中srt字幕
    video_files = build_all_segment_videos(
        manifest_path=paper_dir / "audio" / "audio_manifest.json",
        audio_dir=paper_dir / "audio",
        image_dir=paper_dir / "images",
        subtitle_dir=paper_dir / "subtitles",
        output_dir=Path(paper_dir) / "output" / ("preview_segments" if preview_segments else "segments"),
        segment_indices=preview_segments,
        visual_timeline=visual_timeline,
        video_concurrency=max(
            1,
            int(os.getenv("PAPER_VIDEO_VIDEO_CONCURRENCY", "3")),
        ),
    )

    # 合成高质量视频
    final_path = Path(paper_dir) / "output" / ("preview.mp4" if preview_segments else "final.mp4")
    concat_segment_videos(
        video_files=video_files,
        output_path=final_path,
    )
    if preview_segments:
        print(f"片段预览已生成: {final_path}")
        return

    # 另外生成一个体积更小、兼容性较好的社交平台发布版
    compress_video_for_social(
        input_path=final_path,
        output_path=Path(paper_dir) / "output" / "final_social.mp4",
    )

def default_output_dir(pdf_path: Path) -> Path:
    """Return the default work directory without overwriting the input PDF."""
    return pdf_path.parent / f"{pdf_path.stem}_output"


def missing_external_tools() -> list[str]:
    """Return required command-line tools that are not available on PATH."""
    return [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]


def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将论文 PDF 转换为带配音、字幕和章节进度的竖屏讲解视频",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        required=True,
        help="输入论文 PDF 的路径",
    )
    parser.add_argument(
        "--paper-dir",
        type=Path,
        help="工作目录；默认在 PDF 旁创建 <文件名>_output",
    )
    parser.add_argument(
        "--preview-segments",
        type=int,
        nargs="+",
        help="只合成指定 segment 的预览视频",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = create_argument_parser()
    arguments = parser.parse_args(argv)
    pdf_path = arguments.pdf.expanduser().resolve()
    if not pdf_path.is_file():
        parser.error(f"PDF 文件不存在: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        parser.error(f"输入文件不是 PDF: {pdf_path}")

    missing_tools = missing_external_tools()
    if missing_tools:
        parser.error(
            "缺少外部依赖，请安装并加入 PATH: " + ", ".join(missing_tools)
        )
    paper_dir = (
        arguments.paper_dir.expanduser().resolve()
        if arguments.paper_dir
        else default_output_dir(pdf_path)
    )
    try:
        build_video(
            paper_dir=paper_dir,
            pdf_path=pdf_path,
            preview_segments=arguments.preview_segments,
        )
    except MissingDeepSeekAPIKeyError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
