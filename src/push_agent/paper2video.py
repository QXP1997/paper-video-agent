import subprocess
import edge_tts, asyncio, json
from pathlib import Path

from push_agent.chat import generate_paper_script
from push_agent.models import PaperScript
from push_agent.pdf_util import parse_pdf


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
        "segments": [],
    }

    for index, segment in enumerate(
        _script.segments,
        start=1,
    ):
        audio_name = (
            f"segment_{index:03d}.mp3"
        )

        audio_path = (
            output_dir / audio_name
        )

        print(
            f"生成 TTS: "
            f"{index}/{len(_script.segments)}"
        )

        words = await generate_tts(
            text=segment.text,
            output_mp3=audio_path,
        )

        manifest["segments"].append({
            "index": index,

            # 视频需要展示的论文页
            "page": segment.page,

            # 原始解说内容
            "text": segment.text,

            # 用相对路径，方便以后移动整个目录
            "audio_file": audio_name,

            # edge-tts 给出的语音级时间
            "words": words,
        })

    manifest_path = (
        output_dir
        / "audio_manifest.json"
    )

    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

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

def build_segment_video(
    image_path: Path,
    audio_path: Path,
    subtitle_path: Path,
    output_path: Path,
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

    vf = (
        f"scale={width}:{height}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:"
        "(ow-iw)/2:(oh-ih)/2:white,"
        f"subtitles={subtitle_name}:"
        "force_style='"
        "FontName=Microsoft YaHei,"
        "FontSize=26,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BorderStyle=1,"
        "Outline=2,"
        "Shadow=0,"
        "Alignment=2,"
        "MarginV=120"
        "'"
    )

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

    # 合成视频
    concat_segment_videos(
        video_files=video_files,
        output_path=fr"{paper_dir}\output\final.mp4",
    )

if __name__ == "__main__":
    PAPER_DIR = r"D:\push_agent\paper\test2"
    PDF_PATH = r"D:\push_agent\paper\2609.11977v1.pdf"
    build_video(PAPER_DIR, PDF_PATH)