import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from paper_video_agent.models import ChapterVisualPlan
from paper_video_agent.visual import align_visual_plan, generate_visual_plan, load_assets, _validate_plan


class VisualPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "table.png").touch()
        (self.root / "assets.json").write_text(json.dumps({"assets": [
            {"asset_id": "table1", "kind": "table", "page": 2, "path": "table.png", "bbox": [0, 0, 100, 100]},
            {"asset_id": "eq1", "kind": "formula", "page": 2, "path": "missing.png"},
        ]}), encoding="utf-8")

    def align(self, text, words, cues):
        return align_visual_plan(
            {"segments": [{"segment_index": 1, "cues": cues}]},
            {"segments": [{"index": 1, "text": text, "words": words, "duration": 12}]},
            self.root, self.root / "timeline.json",
        )

    def cue(self, start, end=""):
        return {"asset_id": "table1", "start_quote": start, "end_quote": end, "reason": "compare"}

    def test_punctuation_normalization_and_word_timestamps(self):
        result = self.align("先看。GPT-5：结果很好。然后总结。", [
            {"text": "先看", "start": 0, "end": 1},
            {"text": "GPT", "start": 2, "end": 3},
            {"text": "5", "start": 3, "end": 4},
            {"text": "结果很好", "start": 4, "end": 7},
            {"text": "然后总结", "start": 8, "end": 10},
        ], [self.cue("GPT-5", "结果很好")])
        cue = result["segments"][0]["cues"][0]
        self.assertEqual((cue["start"], cue["end"]), (2, 7))
        self.assertEqual(result["warnings"], [])
        self.assertEqual(cue["page"], 2)  # Cross-page asset selection is allowed.

    def test_ambiguous_quotes_are_rejected(self):
        result = self.align("结果很好，结果很好", [
            {"text": "结果很好结果很好", "start": 0, "end": 10},
        ], [self.cue("结果很好")])
        self.assertFalse(result["segments"][0]["cues"])
        self.assertEqual(len(result["warnings"]), 1)

    def test_missing_tts_content_does_not_guess_timing(self):
        result = self.align("这里解释一张复杂表格", [
            {"text": "没有对应内容", "start": 0, "end": 10},
        ], [self.cue("这里解释")])
        self.assertFalse(result["segments"][0]["cues"])

    def test_hold_to_end_uses_audio_duration(self):
        result = self.align("现在来看这张表", [
            {"text": "现在来看这张表", "start": 1, "end": 10},
        ], [self.cue("现在来看")])
        self.assertEqual(result["segments"][0]["cues"][0]["end"], 12)

    def test_unknown_asset_overlap_and_duplicate_segment(self):
        for plans in [
            [{"segment_index": 1, "cues": [{**self.cue("甲乙"), "asset_id": "unknown"}]}],
            [{"segment_index": 1, "cues": [self.cue("甲乙", "戊己"), self.cue("丙丁")]}],
            [{"segment_index": 1, "cues": []}, {"segment_index": 1, "cues": []}],
        ]:
            with self.subTest(plans=plans), self.assertRaises(ValueError):
                _validate_plan(ChapterVisualPlan.model_validate({"segments": plans}),
                               [{"index": 1, "text": "甲乙丙丁戊己庚辛"}], load_assets(self.root))

    def test_formula_ignored_and_path_escape_rejected(self):
        self.assertEqual(set(load_assets(self.root)), {"table1"})
        (self.root / "assets.json").write_text(json.dumps({"assets": [
            {"asset_id": "bad", "kind": "figure", "path": "../outside.png"},
        ]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_assets(self.root)

    def test_saved_edits_survive_rerun_and_narration_changes_invalidate_cache(self):
        segment = SimpleNamespace(page=1, text="甲乙丙丁戊己")
        script = SimpleNamespace(title="test", chapters=[
            SimpleNamespace(chapter_id="ch1", title="chapter", segments=[segment]),
        ])
        model_plan = ChapterVisualPlan.model_validate({"segments": [
            {"segment_index": 1, "cues": [self.cue("甲乙")]},
        ]})
        output = self.root / "visual_plan.json"
        with patch("paper_video_agent.chat.invoke_structured_with_retry", return_value=model_plan) as invoke:
            generate_visual_plan(script, self.root, output)
            self.assertEqual(invoke.call_count, 1)
            edited = json.loads(output.read_text(encoding="utf-8"))
            edited["segments"][0]["cues"] = []
            output.write_text(json.dumps(edited), encoding="utf-8")
            result = generate_visual_plan(script, self.root, output)
            self.assertEqual(result["segments"][0]["cues"], [])
            self.assertEqual(invoke.call_count, 1)
            segment.text += "庚辛"
            generate_visual_plan(script, self.root, output)
            self.assertEqual(invoke.call_count, 2)


if __name__ == "__main__":
    unittest.main()
