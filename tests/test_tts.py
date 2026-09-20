import pytest

from paper_video_agent.tts import get_tts_config


def test_tts_config_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_VIDEO_TTS_VOICE", "zh-CN-YunxiNeural")
    monkeypatch.setenv("PAPER_VIDEO_TTS_RATE", "+10%")
    monkeypatch.setenv("PAPER_VIDEO_TTS_PITCH", "-2Hz")

    assert get_tts_config() == {
        "backend": "edge",
        "voice": "zh-CN-YunxiNeural",
        "rate": "+10%",
        "pitch": "-2Hz",
    }


def test_tts_config_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="只支持 Edge TTS"):
        get_tts_config(backend="unknown")

