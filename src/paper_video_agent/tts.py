"""Pluggable TTS backends with a shared word-timestamp result format.

Every backend returns the same list used by the subtitle renderer::

    [{"text": "词", "start": 0.0, "end": 0.35}, ...]

The default backend remains Edge TTS. Azure Speech is optional and is only
imported when selected through ``PAPER_VIDEO_TTS_BACKEND=azure``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Protocol

import edge_tts


DEFAULT_TTS_BACKEND = "edge"
DEFAULT_TTS_VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_TTS_RATE = "+0%"
DEFAULT_TTS_PITCH = "+0Hz"


class TTSBackend(Protocol):
    async def synthesize(
        self,
        text: str,
        output_path: Path,
    ) -> list[dict]:
        """Write an MP3 and return word-level timestamps in seconds."""


def get_tts_config(
    backend: str | None = None,
    voice: str | None = None,
    rate: str | None = None,
    pitch: str | None = None,
) -> dict[str, str]:
    """Resolve the current TTS settings from arguments and environment."""
    return {
        "backend": (
            backend
            or os.getenv("PAPER_VIDEO_TTS_BACKEND", DEFAULT_TTS_BACKEND)
        ).strip().lower(),
        "voice": (
            voice
            or os.getenv("PAPER_VIDEO_TTS_VOICE", DEFAULT_TTS_VOICE)
        ).strip(),
        "rate": (
            rate
            or os.getenv("PAPER_VIDEO_TTS_RATE", DEFAULT_TTS_RATE)
        ).strip(),
        "pitch": (
            pitch
            or os.getenv("PAPER_VIDEO_TTS_PITCH", DEFAULT_TTS_PITCH)
        ).strip(),
    }


class EdgeTTSBackend:
    def __init__(
        self,
        voice: str,
        rate: str,
        pitch: str,
    ):
        self.voice = voice
        self.rate = rate
        self.pitch = pitch

    async def synthesize(
        self,
        text: str,
        output_path: Path,
    ) -> list[dict]:
        words = []
        communicate = edge_tts.Communicate(
            text=text,
            voice=self.voice,
            rate=self.rate,
            pitch=self.pitch,
            boundary="WordBoundary",
        )

        with output_path.open("wb") as audio_file:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_file.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    start = chunk["offset"] / 10_000_000
                    duration = chunk["duration"] / 10_000_000
                    words.append({
                        "text": chunk["text"],
                        "start": round(start, 3),
                        "end": round(start + duration, 3),
                    })

        return words


class AzureTTSBackend:
    """Optional Azure Speech backend with native word-boundary timestamps."""

    def __init__(self, voice: str):
        self.voice = voice

    async def synthesize(
        self,
        text: str,
        output_path: Path,
    ) -> list[dict]:
        return await asyncio.to_thread(
            self._synthesize_sync,
            text,
            output_path,
        )

    def _synthesize_sync(
        self,
        text: str,
        output_path: Path,
    ) -> list[dict]:
        try:
            import azure.cognitiveservices.speech as speechsdk
        except ImportError as exc:
            raise RuntimeError(
                "Azure TTS 需要先安装 azure-cognitiveservices-speech"
            ) from exc

        key = os.getenv("AZURE_SPEECH_KEY")
        region = os.getenv("AZURE_SPEECH_REGION")
        if not key or not region:
            raise RuntimeError(
                "Azure TTS 需要配置 AZURE_SPEECH_KEY 和 AZURE_SPEECH_REGION"
            )

        speech_config = speechsdk.SpeechConfig(
            subscription=key,
            region=region,
        )
        speech_config.speech_synthesis_voice_name = self.voice
        speech_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Audio24Khz160KBitRateMonoMp3
        )
        audio_config = speechsdk.audio.AudioOutputConfig(
            filename=str(output_path),
        )
        synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=speech_config,
            audio_config=audio_config,
        )

        words = []

        def on_word_boundary(event):
            token = str(getattr(event, "text", "") or "")
            if not token:
                return
            start = float(event.audio_offset) / 10_000_000
            duration = float(event.duration) / 10_000_000
            words.append({
                "text": token,
                "start": round(start, 3),
                "end": round(start + duration, 3),
            })

        synthesizer.synthesis_word_boundary.connect(on_word_boundary)
        result = synthesizer.speak_text_async(text).get()
        if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
            details = getattr(result, "cancellation_details", None)
            message = getattr(details, "error_details", "未知错误")
            raise RuntimeError(f"Azure TTS 生成失败: {message}")

        return words


def create_tts_backend(config: dict[str, str] | None = None) -> TTSBackend:
    config = config or get_tts_config()
    backend = config["backend"]

    if backend in {"edge", "edge_tts"}:
        return EdgeTTSBackend(
            voice=config["voice"],
            rate=config["rate"],
            pitch=config["pitch"],
        )
    if backend in {"azure", "azure_speech"}:
        return AzureTTSBackend(voice=config["voice"])

    raise ValueError(
        f"不支持的 TTS 后端: {backend}。可选值为 edge 或 azure"
    )


async def generate_tts(
    text: str,
    output_mp3: Path,
    voice: str | None = None,
    rate: str | None = None,
    max_attempts: int = 5,
    backend: str | None = None,
    pitch: str | None = None,
) -> list[dict]:
    """Generate an MP3 atomically and return provider-neutral word timings."""
    output_mp3 = Path(output_mp3)
    output_mp3.parent.mkdir(parents=True, exist_ok=True)
    config = get_tts_config(
        backend=backend,
        voice=voice,
        rate=rate,
        pitch=pitch,
    )
    tts_backend = create_tts_backend(config)
    temporary_path = output_mp3.with_suffix(output_mp3.suffix + ".part")

    for attempt in range(1, max_attempts + 1):
        temporary_path.unlink(missing_ok=True)
        try:
            words = await tts_backend.synthesize(text, temporary_path)
            if (
                not temporary_path.exists()
                or temporary_path.stat().st_size <= 0
                or not words
            ):
                raise RuntimeError("TTS 返回了空音频或空时间戳")

            temporary_path.replace(output_mp3)
            return words
        except Exception:
            temporary_path.unlink(missing_ok=True)
            if attempt >= max_attempts:
                raise

            delay = min(2 ** attempt, 15)
            print(
                f"TTS 网络调用失败，{delay} 秒后重试 "
                f"{attempt + 1}/{max_attempts}..."
            )
            await asyncio.sleep(delay)

    raise RuntimeError("TTS 生成失败")
