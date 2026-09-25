"""Backward-compatible exports for the shared video TTS implementation."""

from research_video_core.tts import (
    DEFAULT_TTS_PITCH,
    DEFAULT_TTS_RATE,
    DEFAULT_TTS_VOICE,
    EdgeTTSBackend,
    TTSBackend,
    create_tts_backend,
    generate_tts,
    get_tts_config,
)

__all__ = [
    "DEFAULT_TTS_PITCH",
    "DEFAULT_TTS_RATE",
    "DEFAULT_TTS_VOICE",
    "EdgeTTSBackend",
    "TTSBackend",
    "create_tts_backend",
    "generate_tts",
    "get_tts_config",
]
