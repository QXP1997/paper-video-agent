import json

from paper_video_agent import paper2video
from paper_video_agent import tts as paper_tts
from research_agent_core.artifacts import canonical_sha256, write_json_atomic
from research_video_core import subtitles, timeline, tts


def test_atomic_json_and_canonical_hash_are_stable(tmp_path):
    first = {"中文": [2, 1], "nested": {"enabled": True}}
    second = {"nested": {"enabled": True}, "中文": [2, 1]}
    assert canonical_sha256(first) == canonical_sha256(second)

    output_path = tmp_path / "nested" / "artifact.json"
    write_json_atomic(output_path, first)
    assert json.loads(output_path.read_text(encoding="utf-8")) == first
    assert not output_path.with_suffix(".json.tmp").exists()


def test_paper_video_compatibility_exports_use_shared_video_core():
    assert paper_tts.generate_tts is tts.generate_tts
    assert paper2video.build_subtitles is subtitles.build_subtitles
    assert paper2video.write_segment_srt is subtitles.write_segment_srt
    assert paper2video.enrich_manifest_timeline is timeline.enrich_manifest_timeline
