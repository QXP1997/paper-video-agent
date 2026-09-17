import asyncio
import json
import subprocess
from pathlib import Path

import edge_tts

from paper_video_agent.chat import generate_paper_script
from paper_video_agent.models import PaperScript
from paper_video_agent.pdf_util import parse_pdf


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

# tts 生成
async def generate_tts(
    text: str,
    output_mp3: Path,
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: str = "+0%",
) -> list[dict]:

    output_mp3.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        boundary="WordBoundary",
    )

    words = []

    with output_mp3.open("wb") as audio_file:

        async for chunk in communicate.stream():

            # 写入音频
            if chunk["type"] == "audio":
                audio_file.write(
                    chunk["data"]
                )

            # 保存词级时间戳
            elif chunk["type"] == "WordBoundary":
                start = (
                    chunk["offset"]
                    / 10_000_000
                )

                duration = (
                    chunk["duration"]
                    / 10_000_000
                )

                words.append({
                    "text": chunk["text"],
                    "start": round(start, 3),
                    "end": round(
                        start + duration,
                        3,
                    ),
                })

    return words


# 生成视频segment.mp3以及audio_manifest.json
async def generate_script_audio(
    _script: PaperScript,
    output_dir: str | Path,
):
    output_dir = Path(output_dir)

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    manifest = {
        "title": _script.title,
        "chapters": [],
        "segments": [],
    }

    total_segments = sum(
        len(chapter.segments)
        for chapter in _script.chapters
    )
    segment_index = 0
    chapter_count = len(_script.chapters)

    for chapter_index, chapter in enumerate(
        _script.chapters,
        start=1,
    ):
        chapter_segment_start = segment_index + 1
        chapter_segment_count = len(chapter.segments)

        for chapter_segment_index, segment in enumerate(
            chapter.segments,
            start=1,
        ):
            segment_index += 1
            audio_name = f"segment_{segment_index:03d}.mp3"
            audio_path = output_dir / audio_name

            print(
                f"生成 TTS: {segment_index}/{total_segments} "
                f"[{chapter.title} "
                f"{chapter_segment_index}/{chapter_segment_count}]"
            )

            words = await generate_tts(
                text=segment.text,
                output_mp3=audio_path,
            )

            manifest["segments"].append({
                "index": segment_index,
                "chapter_id": chapter.chapter_id,
                "chapter_index": chapter_index,
                "chapter_count": chapter_count,
                "chapter_title": chapter.title,
                "chapter_segment_index": chapter_segment_index,
                "chapter_segment_count": chapter_segment_count,

                # 视频需要展示的论文页
                "page": segment.page,

                # 原始解说内容
                "text": segment.text,

                # 用相对路径，方便以后移动整个目录
                "audio_file": audio_name,

                # edge-tts 给出的语音级时间
                "words": words,
            })

        manifest["chapters"].append({
            "chapter_id": chapter.chapter_id,
            "index": chapter_index,
            "title": chapter.title,
            "segment_start": chapter_segment_start,
            "segment_end": segment_index,
        })

    manifest_path = (
        output_dir
        / "audio_manifest.json"
    )

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


def build_subtitles(
    words: list[dict],
    max_chars: int = 18,
    min_chars: int = 8,
) -> list[dict]:
    """
    根据 WordBoundary 生成字幕段。

    规则：
    1. 每条字幕尽量不超过 max_chars
    2. 达到 min_chars 后，遇到标点优先切
    3. 时间直接使用真实 WordBoundary
    """

    subtitles = []

    current_words = []
    current_text = ""

    def flush():
        nonlocal current_words, current_text

        if not current_words:
            return

        subtitles.append({
            "text": current_text,
            "start": current_words[0]["start"],
            "end": current_words[-1]["end"],
        })

        current_words = []
        current_text = ""

    for word in words:
        text = word["text"]

        if not text:
            continue

        # 加上当前 word 会超长，先把上一条字幕提交
        if (
            current_words
            and len(current_text) + len(text) > max_chars
        ):
            flush()

        current_words.append(word)
        current_text += text

        # 长度差不多了，而且遇到了自然标点
        if (
            len(current_text) >= min_chars
            and current_text[-1] in PUNCTUATIONS
        ):
            flush()

    flush()

    return subtitles


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
    chapter_elapsed: float,
    segment_duration: float,
    chapter_duration: float,
    width: int,
) -> list[str]:
    """Build a chapter progress bar that advances while the segment plays."""
    if not chapters:
        return []

    margin_x = 36
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

    filters = [
        (
            f"drawbox=x=24:y=24:w={width - 48}:h=116:"
            "color=white@0.90:t=fill"
        ),
        (
            f"drawbox=x={margin_x}:y=42:w={navigation_width}:h=8:"
            "color=0xCBD5E1:t=fill"
        ),
    ]

    current_cell_x = margin_x + round(
        (current_chapter_index - 1) * cell_width
    )
    current_cell_end = margin_x + round(
        current_chapter_index * cell_width
    )
    current_cell_width = current_cell_end - current_cell_x

    # Completed chapters stay blue. The current chapter fills orange based on
    # its elapsed audio time, including a few pixels that appear during this
    # segment as FFmpeg's local `t` advances.
    completed_width = current_cell_x - margin_x
    if completed_width > 0:
        filters.append(
            f"drawbox=x={margin_x}:y=42:w={completed_width}:h=8:"
            "color=0x3B82F6:t=fill"
        )

    safe_chapter_duration = max(chapter_duration, 0.001)
    start_progress = min(
        1.0,
        max(0.0, chapter_elapsed / safe_chapter_duration),
    )
    end_progress = min(
        1.0,
        max(
            start_progress,
            (chapter_elapsed + segment_duration) / safe_chapter_duration,
        ),
    )
    static_end_x = current_cell_x + int(
        current_cell_width * start_progress
    )
    dynamic_end_x = current_cell_x + round(
        current_cell_width * end_progress
    )

    if static_end_x > current_cell_x:
        filters.append(
            f"drawbox=x={current_cell_x}:y=42:"
            f"w={static_end_x - current_cell_x}:h=8:"
            "color=0xF97316:t=fill"
        )

    for pixel_x in range(static_end_x, dynamic_end_x):
        pixel_progress = (
            (pixel_x + 0.5 - current_cell_x)
            / max(current_cell_width, 1)
        )
        activation_time = max(
            0.0,
            pixel_progress * safe_chapter_duration - chapter_elapsed,
        )
        filters.append(
            f"drawbox=x={pixel_x}:y=42:w=1:h=8:"
            "color=0xF97316:t=fill:"
            f"enable='gte(t,{activation_time:.3f})'"
        )

    # Small separators make the chapter boundaries readable without turning
    # the bar back into a row of independent tabs.
    for boundary_index in range(1, len(chapters)):
        boundary_x = margin_x + round(boundary_index * cell_width)
        filters.append(
            f"drawbox=x={boundary_x}:y=39:w=2:h=14:"
            "color=white@0.95:t=fill"
        )

    for chapter in chapters:
        chapter_index = int(chapter["index"])
        title = escape_drawtext_text(str(chapter["title"]))
        cell_x = margin_x + round((chapter_index - 1) * cell_width)
        next_cell_x = margin_x + round(chapter_index * cell_width)
        actual_cell_width = next_cell_x - cell_x

        if chapter_index < current_chapter_index:
            text_color = "0x475569"
        elif chapter_index == current_chapter_index:
            text_color = "0xC2410C"
        else:
            text_color = "0x94A3B8"

        filters.append(
            (
                "drawtext="
                f"{font_option}:"
                f"text='{title}':"
                f"fontcolor={text_color}:"
                f"fontsize={font_size}:"
                f"x={cell_x + actual_cell_width / 2}-text_w/2:"
                "y=72"
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
    chapter_elapsed: float,
    segment_duration: float,
    chapter_duration: float,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
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
            "force_original_aspect_ratio=decrease"
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
        chapter_elapsed=chapter_elapsed,
        segment_duration=segment_duration,
        chapter_duration=chapter_duration,
        width=width,
    ))
    vf = ",".join(filters)

    cmd = [
        "ffmpeg",
        "-y",

        # 静态 PDF 页面
        "-loop", "1",
        "-i", str(image_path.resolve()),

        # TTS
        "-i", str(audio_path.resolve()),

        "-vf", vf,

        "-r", str(fps),

        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",

        "-c:a", "aac",
        "-b:a", "192k",

        # 音频结束，当前 segment 就结束
        "-shortest",

        str(output_path.resolve()),
    ]

    run_cmd(
        cmd,
        cwd=subtitle_path.parent,
    )

def build_all_segment_videos(
    manifest_path: str | Path,
    audio_dir: str | Path,
    image_dir: str | Path,
    subtitle_dir: str | Path,
    output_dir: str | Path,
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

    manifest = json.loads(
        manifest_path.read_text(
            encoding="utf-8"
        )
    )
    enrich_manifest_timeline(manifest)
    chapters_by_index = {
        int(chapter["index"]): chapter
        for chapter in manifest["chapters"]
    }

    video_files = []

    for segment in manifest["segments"]:
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
            chapter_elapsed=float(segment["chapter_elapsed"]),
            segment_duration=float(segment["duration"]),
            chapter_duration=float(
                chapters_by_index[int(segment["chapter_index"])]["duration"]
            ),
        )

        video_files.append(
            video_path
        )

    return video_files
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
        / "concat.txt"
    )

    lines = []

    for video_path in video_files:
        path = (
            str(video_path.resolve())
            .replace("\\", "/")
            .replace("'", r"'\''")
        )

        lines.append(
            f"file '{path}'"
        )

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

        # 前面所有 segment 参数一致，
        # 所以直接 copy 即可
        "-c", "copy",

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


def build_video(paper_dir: str | Path, pdf_path: str | Path):
    # pdf转文本和图片
    pages, page_images = parse_pdf(
        pdf_path=pdf_path,
        output_dir=fr"{paper_dir}\images",
        zoom=2.0,
    )

    # 生成论文脚本
    script = generate_paper_script(pages["pages"])

    # 暂存脚本json
    save_paper_script(
        script,
        fr"{paper_dir}\output\paper_script.json",
    )

    # 保存segment视频片段
    asyncio.run(
        generate_script_audio(
            script,
            fr"{paper_dir}\audio",
        )
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
        output_dir=fr"{paper_dir}\output\segments",
    )

    # 合成高质量视频
    final_path = Path(paper_dir) / "output" / "final.mp4"
    concat_segment_videos(
        video_files=video_files,
        output_path=final_path,
    )

    # 另外生成一个体积更小、兼容性较好的社交平台发布版
    compress_video_for_social(
        input_path=final_path,
        output_path=Path(paper_dir) / "output" / "final_social.mp4",
    )

if __name__ == "__main__":
    PAPER_DIR = r"D:\push_agent\paper\test3"
    PDF_PATH = r"D:\push_agent\paper\2609.11977v1.pdf"
    build_video(PAPER_DIR, PDF_PATH)
