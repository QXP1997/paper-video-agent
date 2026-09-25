"""Provider-neutral subtitle segmentation and SRT output."""

from __future__ import annotations

import copy
import json
from pathlib import Path

PUNCTUATIONS = "，。！？；：,.!?;:"


def format_srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    hours = ms // 3_600_000
    ms %= 3_600_000
    minutes = ms // 60_000
    ms %= 60_000
    seconds = ms // 1000
    ms %= 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def _words_with_spaced_punctuation(
    words: list[dict],
    source_text: str | None,
) -> list[dict]:
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
            gap = ""
        else:
            gap = source_text[source_cursor:found_at]
            source_cursor = found_at + len(token)

        spaced_word = copy.copy(word)
        spaced_word["text"] = (
            (" " if any(char in punctuation or char.isspace() for char in gap) else "")
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
    """Build readable subtitle chunks while preserving word timestamps."""
    words = _words_with_spaced_punctuation(words, source_text)
    subtitles = []
    max_lines = max(1, int(max_lines))
    max_chars = max(1, int(max_chars))
    max_total_chars = max_chars * max_lines
    current_words = []
    current_text = ""

    def flush() -> None:
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
        if (
            len(current_text) >= min_chars
            and current_text[-1] in PUNCTUATIONS
            and not decimal_continues
        ):
            flush()
    flush()

    sentence_punctuation = set(
        PUNCTUATIONS + "、，。！？；：‘’“”《》（）【】—…·-–—/"
    )

    def raw_text(items: list[dict]) -> str:
        return "".join(str(item.get("text", "")) for item in items)

    def refresh(item: dict) -> None:
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
        if (
            len(prefix_text) < min_chars
            or len(following_text) + tail_length > max_total_chars
        ):
            continue

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
        text = " ".join(text.strip().split())
        if len(text) <= max_chars or max_lines == 1:
            return text
        split_at = text.rfind(" ", 0, max_chars + 1)
        if split_at < max(1, max_chars // 2) or len(text) - split_at > max_chars:
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
) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subtitles = build_subtitles(
        words=segment["words"],
        source_text=segment.get("text"),
        max_chars=max_chars,
    )
    blocks = [
        (
            f"{index}\n"
            f"{format_srt_time(subtitle['start'])} --> "
            f"{format_srt_time(subtitle['end'])}\n"
            f"{subtitle['text']}\n"
        )
        for index, subtitle in enumerate(subtitles, start=1)
    ]
    output_path.write_text("\n".join(blocks), encoding="utf-8")


def generate_segment_srts(
    manifest_path: str | Path,
    output_dir: str | Path,
    max_chars: int = 18,
) -> None:
    manifest_path = Path(manifest_path)
    output_dir = Path(output_dir)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for segment in manifest["segments"]:
        output_path = output_dir / f"segment_{segment['index']:03d}.srt"
        write_segment_srt(segment, output_path, max_chars=max_chars)
        print(f"字幕生成完成: {output_path}")

