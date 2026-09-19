import asyncio
import copy
import json
import math
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from paper_video_agent.chat import generate_paper_script
from paper_video_agent.models import PaperScript
from paper_video_agent.pdf_util import parse_pdf
from paper_video_agent.tts import generate_tts, get_tts_config
from paper_video_agent.visual import generate_visual_plan, align_visual_plan


def save_paper_script(
    _script: PaperScript,
    output_path: str | Path,
):
    output_path = Path(output_path)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_path.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            _script.model_dump(),
            f,
            ensure_ascii=False,
            indent=2,
        )

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
            return _reusable

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


def enrich_manifest_timeline(manifest: dict) -> dict:
    """Add timeline fields used by the animated chapter progress bar.

    Older manifests did not contain durations, so derive them from the final
    word timestamp. This keeps existing TTS output reusable when the video
    renderer changes.
    """
    chapters_by_index = {
        int(chapter["index"]): chapter
        for chapter in manifest.get("chapters", [])
    }
    chapter_elapsed: dict[int, float] = {
        chapter_index: 0.0
        for chapter_index in chapters_by_index
    }
    total_elapsed = 0.0

    for segment in manifest.get("segments", []):
        words = segment.get("words") or []
        inferred_duration = (
            float(words[-1].get("end", 0.0))
            if words
            else 0.0
        )
        duration = max(
            0.001,
            float(segment.get("duration") or inferred_duration),
        )
        chapter_index = int(segment["chapter_index"])
        elapsed_in_chapter = chapter_elapsed.get(chapter_index, 0.0)

        segment["duration"] = round(duration, 3)
        segment["start_time"] = round(total_elapsed, 3)
        segment["chapter_elapsed"] = round(elapsed_in_chapter, 3)

        total_elapsed += duration
        chapter_elapsed[chapter_index] = elapsed_in_chapter + duration

    chapter_start = 0.0
    for chapter in manifest.get("chapters", []):
        chapter_index = int(chapter["index"])
        duration = chapter_elapsed.get(chapter_index, 0.0)
        chapter["start_time"] = round(chapter_start, 3)
        chapter["duration"] = round(duration, 3)
        chapter["end_time"] = round(chapter_start + duration, 3)
        chapter_start += duration

    manifest["duration"] = round(total_elapsed, 3)
    return manifest

#########################生产srt字幕##############################
PUNCTUATIONS = "，。！？；：,.!?;:"

def format_srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)

    h = ms // 3_600_000
    ms %= 3_600_000

    m = ms // 60_000
    ms %= 60_000

    s = ms // 1000
    ms %= 1000

    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_with_spaced_punctuation(
    words: list[dict],
    source_text: str | None,
) -> list[dict]:
    """Replace source punctuation with visual spaces in subtitle words."""
    if not source_text:
        return words

    punctuation = set(PUNCTUATIONS + "、，。！？；：‘’“”《》（）【】—…·-–—/")
    source_cursor = 0
    spaced_words = []

    for word in words:
        token = str(word.get("text", ""))
        if not token:
            continue

        found_at = source_text.find(token, source_cursor)
        if found_at < 0:
            # TTS occasionally normalizes a token (for example a dash or a
            # version suffix). Keep the timing, but do not invent a gap.
            gap = ""
        else:
            gap = source_text[source_cursor:found_at]
            source_cursor = found_at + len(token)

        needs_space = any(
            char in punctuation or char.isspace()
            for char in gap
        )
        spaced_word = copy.copy(word)
        spaced_word["text"] = (
            (" " if needs_space else "")
            + token
        )
        spaced_words.append(spaced_word)

    return spaced_words


def build_subtitles(
    words: list[dict],
    source_text: str | None = None,
    max_chars: int = 18,
    min_chars: int = 8,
    max_lines: int = 2,
    short_tail_chars: int = 4,
) -> list[dict]:
    """
    根据 WordBoundary 生成字幕段。

    规则：
    1. 每行尽量不超过 max_chars，单条字幕最多 max_lines 行
    2. 达到 min_chars 后，遇到标点优先切
    3. 由于时间直接使用真实 WordBoundary，短尾词（例如单独的“它”）
       会尽量并入下一条字幕，避免上一条字幕只剩一个语义上属于下一句的词
    """

    words = _words_with_spaced_punctuation(words, source_text)
    subtitles = []
    max_lines = max(1, int(max_lines))
    max_chars = max(1, int(max_chars))
    max_total_chars = max_chars * max_lines

    current_words = []
    current_text = ""

    def flush():
        nonlocal current_words, current_text

        if not current_words:
            return

        subtitles.append({
            "text": current_text.strip(),
            "start": current_words[0]["start"],
            "end": current_words[-1]["end"],
            "_words": current_words.copy(),
        })

        current_words = []
        current_text = ""

    for word_index, word in enumerate(words):
        text = word["text"]

        if not text:
            continue

        joins_identifier = (
            bool(current_text)
            and current_text[-1].isascii()
            and current_text[-1].isalnum()
            and text[0].isascii()
            and text[0].isalnum()
        )
        decimal_identifier_continues = (
            bool(current_text)
            and current_text.endswith(".")
            and current_text[-2:-1].isdigit()
            and text[0].isdigit()
        )

        # 加上当前 word 会超长，先把上一条字幕提交。英文数字组成的
        # 连续标识符允许略微超长，避免把 pass3、GPT5 等拆成两条。
        if (
            current_words
            and len(current_text) + len(text) > max_total_chars
            and not joins_identifier
            and not decimal_identifier_continues
        ):
            flush()

        current_words.append(word)
        current_text += text

        next_text = (
            str(words[word_index + 1].get("text", ""))
            if word_index + 1 < len(words)
            else ""
        )
        normalized_text = text.lstrip()
        decimal_continues = (
            normalized_text.endswith(".")
            and normalized_text[:-1].isdigit()
            and next_text[:1].isdigit()
        )

        # 长度差不多了，而且遇到了自然标点。版本号和小数中的
        # 点不是句子边界，例如 Occamy-1.0 不能在 “1.” 后切开。
        if (
            len(current_text) >= min_chars
            and current_text[-1] in PUNCTUATIONS
            and not decimal_continues
        ):
            flush()

    flush()

    # A chunk created by the character limit can end with a very short word
    # that actually starts the next spoken phrase (for example ``...运行的 它``).
    # Move that tail to the following chunk when there is room. This keeps the
    # word-level timing intact while making the visual subtitle read naturally.
    sentence_punctuation = set(PUNCTUATIONS + "、，。！？；：‘’“”《》（）【】—…·-–—/")

    def raw_text(items: list[dict]) -> str:
        return "".join(str(item.get("text", "")) for item in items)

    def refresh(item: dict):
        item["text"] = raw_text(item["_words"]).strip()
        item["start"] = item["_words"][0]["start"]
        item["end"] = item["_words"][-1]["end"]

    for index in range(len(subtitles) - 1):
        current = subtitles[index]
        following = subtitles[index + 1]
        current_words = current["_words"]
        following_words = following["_words"]

        if len(current_words) < 2 or not following_words:
            continue

        # Usually one Edge-TTS word is enough. If it was split into two very
        # short tokens, move the smallest trailing run (up to four visible
        # characters) as one unit.
        tail_words = []
        tail_length = 0
        cursor = len(current_words) - 1
        while cursor >= 1:
            token = str(current_words[cursor].get("text", "")).strip()
            if (
                not token
                or all(char in sentence_punctuation for char in token)
                or token[-1] in sentence_punctuation
            ):
                break
            if (
                len(token) > short_tail_chars
                or tail_length + len(token) > short_tail_chars
            ):
                break
            tail_words.insert(0, current_words[cursor])
            tail_length += len(token)
            cursor -= 1

        if not tail_words:
            continue

        prefix_words = current_words[:cursor + 1]
        prefix_text = raw_text(prefix_words).strip()
        following_text = raw_text(following_words).strip()
        if len(prefix_text) < min_chars or len(following_text) + tail_length > max_total_chars:
            continue

        # Avoid carrying punctuation-introduced leading whitespace into the
        # first word of the next subtitle.
        moved_words = []
        for word in tail_words:
            moved = copy.copy(word)
            moved["text"] = str(moved.get("text", "")).lstrip()
            moved_words.append(moved)

        current["_words"] = prefix_words
        following["_words"] = moved_words + following_words
        refresh(current)
        refresh(following)

    def wrap_text(text: str) -> str:
        """Wrap a subtitle into at most two balanced, readable lines."""
        text = " ".join(text.strip().split())
        if len(text) <= max_chars or max_lines == 1:
            return text

        # Prefer breaking at a visual space, but fall back to a character
        # boundary for Chinese text where spaces are not normally present.
        split_at = text.rfind(" ", 0, max_chars + 1)
        if (
            split_at < max(1, max_chars // 2)
            or len(text) - split_at > max_chars
        ):
            split_at = max_chars
        first = text[:split_at].rstrip()
        second = text[split_at:].strip()
        return f"{first}\n{second}"

    result = []
    for subtitle in subtitles:
        refresh(subtitle)
        result.append({
            "text": wrap_text(subtitle["text"]),
            "start": subtitle["start"],
            "end": subtitle["end"],
        })
    return result


def write_segment_srt(
    segment: dict,
    output_path: str | Path,
    max_chars: int = 18,
):
    output_path = Path(output_path)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    subtitles = build_subtitles(
        words=segment["words"],
        source_text=segment.get("text"),
        max_chars=max_chars,
    )

    blocks = []

    for index, subtitle in enumerate(
        subtitles,
        start=1,
    ):
        blocks.append(
            f"{index}\n"
            f"{format_srt_time(subtitle['start'])} --> "
            f"{format_srt_time(subtitle['end'])}\n"
            f"{subtitle['text']}\n"
        )

    output_path.write_text(
        "\n".join(blocks),
        encoding="utf-8",
    )

def generate_segment_srts(
    manifest_path: str | Path,
    output_dir: str | Path,
    max_chars: int = 18,
):
    manifest_path = Path(manifest_path)
    output_dir = Path(output_dir)

    manifest = json.loads(
        manifest_path.read_text(
            encoding="utf-8"
        )
    )

    for segment in manifest["segments"]:
        index = segment["index"]

        output_path = (
            output_dir
            / f"segment_{index:03d}.srt"
        )

        write_segment_srt(
            segment=segment,
            output_path=output_path,
            max_chars=max_chars,
        )

        print(
            f"字幕生成完成: {output_path}"
        )
#########################生产srt字幕##############################

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
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font_option = (
        f"fontfile='{escape_drawtext_text(font_path.as_posix())}'"
        if font_path.is_file()
        else "font='Microsoft YaHei'"
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

    filters = [
        (
            f"scale={width}:{height}:"
            "force_original_aspect_ratio=decrease,setsar=1"
        ),
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:white",
        (
            f"subtitles={subtitle_name}:"
            "force_style='"
            "FontName=Microsoft YaHei,"
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
        # Each screenshot becomes a complete white-backed frame. This replaces
        # the PDF during its interval without cropping or stretching the asset.
        graph = [f"[0:v]{','.join(filters[:2])},setsar=1[page]"]
        previous = "page"
        for index, cue in enumerate(focus_cues):
            screenshot = Path(cue["image_path"])
            if not screenshot.is_file():
                raise FileNotFoundError(f"聚焦素材不存在: {screenshot}")
            start, end = float(cue["start"]), float(cue["end"])
            if not 0 <= start < end <= segment_duration + 0.001:
                raise ValueError(f"聚焦时间范围无效: {start}, {end}")
            cmd.extend(["-loop", "1", "-framerate", str(fps), "-i", str(screenshot.resolve())])
            # Reserve the top 88 px for navigation and bottom 280 for subtitles.
            content_height = height - 88 - 280
            graph.append(
                f"[{index + 2}:v]scale={width - 64}:{content_height}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:88+({content_height}-ih)/2:white,"
                f"setsar=1[asset{index}]"
            )
            label = f"view{index}"
            graph.append(
                f"[{previous}][asset{index}]overlay=0:0:"
                f"enable='gte(t,{start:.3f})*lt(t,{end:.3f})'[{label}]"
            )
            previous = label
        # Subtitles and navigation are rendered once, after all image switches.
        graph.append(f"[{previous}]{','.join(filters[2:])}[video]")
        cmd.extend(["-filter_complex_threads", "1", "-filter_complex", ";".join(graph),
                    "-map", "[video]", "-map", "1:a:0"])
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


def build_all_segment_videos(
    manifest_path: str | Path,
    audio_dir: str | Path,
    image_dir: str | Path,
    subtitle_dir: str | Path,
    output_dir: str | Path,
    video_concurrency: int = 2,
    visual_timeline: dict | None = None,
    segment_indices: list[int] | None = None,
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
        item["segment_index"]: item["cues"]
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

        print(
            f"\n[{index}/{len(manifest['segments'])}] "
            f"page={page}"
        )

        build_segment_video(
            image_path=image_path,
            audio_path=audio_path,
            subtitle_path=subtitle_path,
            output_path=video_path,
            chapters=manifest["chapters"],
            current_chapter_index=segment["chapter_index"],
            video_elapsed=float(segment["start_time"]),
            segment_duration=float(segment["duration"]),
            video_duration=float(manifest["duration"]),
            focus_cues=visual_by_index.get(index, []),
        )

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


def build_video(paper_dir: str | Path, pdf_path: str | Path, preview_segments: list[int] | None = None):
    # pdf转文本和图片
    pages, page_images = parse_pdf(
        pdf_path=pdf_path,
        output_dir=fr"{paper_dir}\images",
        zoom=2.0,
        metadata_dir=fr"{paper_dir}\metadata",
    )

    script_path = Path(paper_dir) / "output" / "paper_script.json"

    # 任务中断后优先复用已经生成完成的文案，避免重复调用模型。
    if script_path.exists():
        print(f"复用已有论文脚本: {script_path}")
        script = PaperScript.model_validate_json(
            script_path.read_text(encoding="utf-8")
        )
    else:
        script = generate_paper_script(pages["pages"])
        save_paper_script(script, script_path)

    visual_plan = generate_visual_plan(
        script, Path(paper_dir) / "metadata",
        Path(paper_dir) / "output" / "visual_plan.json", pages["pages"],
    )

    # 保存segment视频片段
    tts_concurrency = max(
        1,
        int(os.getenv("PAPER_VIDEO_TTS_CONCURRENCY", "4")),
    )
    asyncio.run(
        generate_script_audio(
            script,
            fr"{paper_dir}\audio",
            tts_concurrency=tts_concurrency,
        )
    )

    visual_timeline = align_visual_plan(
        visual_plan,
        load_audio_timeline(Path(paper_dir) / "audio" / "audio_manifest.json", Path(paper_dir) / "audio"),
        Path(paper_dir) / "metadata",
        Path(paper_dir) / "output" / "visual_timeline.json",
    )

    # 生成srt字幕
    generate_segment_srts(
        manifest_path=fr"{paper_dir}\audio\audio_manifest.json",
        output_dir=fr"{paper_dir}\subtitles",
        max_chars=18,
    )

    # 合成segment中srt字幕
    video_files = build_all_segment_videos(
        manifest_path=fr"{paper_dir}\audio\audio_manifest.json",
        audio_dir=fr"{paper_dir}\audio",
        image_dir=fr"{paper_dir}\images",
        subtitle_dir=fr"{paper_dir}\subtitles",
        output_dir=Path(paper_dir) / "output" / ("focus_preview_segments" if preview_segments else "segments"),
        visual_timeline=visual_timeline,
        segment_indices=preview_segments,
        video_concurrency=max(
            1,
            int(os.getenv("PAPER_VIDEO_VIDEO_CONCURRENCY", "3")),
        ),
    )

    # 合成高质量视频
    final_path = Path(paper_dir) / "output" / ("focus_preview.mp4" if preview_segments else "final.mp4")
    concat_segment_videos(
        video_files=video_files,
        output_path=final_path,
    )
    if preview_segments:
        print(f"图表聚焦预览已生成: {final_path}")
        return

    # 另外生成一个体积更小、兼容性较好的社交平台发布版
    compress_video_for_social(
        input_path=final_path,
        output_path=Path(paper_dir) / "output" / "final_social.mp4",
    )

if __name__ == "__main__":
    import argparse

    pdf = r"D:\push_agent\paper\2609.20804v1\2609.20804v1.pdf"
    paper_dir = r"D:\push_agent\paper\2609.20804v1"
    parser = argparse.ArgumentParser(description="论文讲解视频：独立图表镜头与词级时间对齐")
    parser.add_argument("--paper-dir", default=paper_dir)
    parser.add_argument("--pdf", default=pdf)
    parser.add_argument("--preview-segments", type=int, nargs="+", help="只合成指定 segment 的聚焦预览")
    arguments = parser.parse_args()
    build_video(arguments.paper_dir, arguments.pdf, arguments.preview_segments)
