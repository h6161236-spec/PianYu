from __future__ import annotations

import unittest

from vcut_studio.cutting import generate_cut_suggestions, normalize_token
from vcut_studio.models import Segment, WordTiming
from vcut_studio.settings import CuttingSettings


class CuttingLogicTests(unittest.TestCase):
    def test_normalize_token(self) -> None:
        self.assertEqual(normalize_token(" 这个，"), "这个")
        self.assertEqual(normalize_token("啊..."), "啊")

    def test_generate_filler_and_pause_suggestions(self) -> None:
        segment = Segment(
            segment_id="seg-1",
            start_ms=0,
            end_ms=3000,
            zh_text="嗯 我们 继续",
            words=[
                WordTiming(text="嗯", start_ms=0, end_ms=120, confidence=0.99),
                WordTiming(text="我们", start_ms=900, end_ms=1200, confidence=0.99),
                WordTiming(text="继续", start_ms=1600, end_ms=1900, confidence=0.99),
            ],
        )
        settings = CuttingSettings(
            filler_words=["嗯", "啊", "这个"],
            pause_threshold_ms=500,
            cut_padding_ms=80,
            merge_gap_ms=120,
            mode="standard",
            preserve_intro_pause=True,
        )

        suggestions = generate_cut_suggestions([segment], settings)
        self.assertEqual(len(suggestions), 2)
        reasons = {suggestion.reason for suggestion in suggestions}
        self.assertEqual(reasons, {"filler_word", "long_pause"})

    def test_generate_inter_segment_silence_suggestion(self) -> None:
        left = Segment(
            segment_id="left",
            start_ms=0,
            end_ms=1000,
            zh_text="第一句",
            words=[WordTiming(text="第一句", start_ms=0, end_ms=1000, confidence=0.95)],
        )
        right = Segment(
            segment_id="right",
            start_ms=1800,
            end_ms=2600,
            zh_text="第二句",
            words=[WordTiming(text="第二句", start_ms=1800, end_ms=2600, confidence=0.95)],
        )
        settings = CuttingSettings(
            filler_words=["嗯"],
            pause_threshold_ms=500,
            cut_padding_ms=60,
            merge_gap_ms=120,
            mode="standard",
            preserve_intro_pause=True,
        )

        suggestions = generate_cut_suggestions([left, right], settings)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].reason, "silent_range")
        self.assertGreater(suggestions[0].duration_ms, 0)

    def test_generate_leading_and_trailing_silence_suggestions(self) -> None:
        segment = Segment(
            segment_id="seg-3",
            start_ms=3200,
            end_ms=4300,
            zh_text="正式开始",
            words=[WordTiming(text="正式开始", start_ms=3200, end_ms=4300, confidence=0.95)],
        )
        settings = CuttingSettings(
            filler_words=["嗯"],
            pause_threshold_ms=500,
            cut_padding_ms=60,
            merge_gap_ms=120,
            mode="standard",
            preserve_intro_pause=True,
        )

        suggestions = generate_cut_suggestions([segment], settings, total_duration_ms=7000)

        self.assertEqual([suggestion.reason for suggestion in suggestions], ["silent_range", "silent_range"])
        self.assertIn("片头", suggestions[0].details)
        self.assertIn("片尾", suggestions[1].details)

    def test_generate_full_video_silence_suggestion_when_no_segments(self) -> None:
        settings = CuttingSettings(
            filler_words=["嗯"],
            pause_threshold_ms=500,
            cut_padding_ms=60,
            merge_gap_ms=120,
            mode="standard",
            preserve_intro_pause=True,
        )

        suggestions = generate_cut_suggestions([], settings, total_duration_ms=4500)

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].reason, "silent_range")
        self.assertIn("整段未检测到语音", suggestions[0].details)

    def test_merge_nearby_filler_suggestions(self) -> None:
        segment = Segment(
            segment_id="seg-2",
            start_ms=0,
            end_ms=2000,
            zh_text="嗯 啊 开始",
            words=[
                WordTiming(text="嗯", start_ms=0, end_ms=80, confidence=0.99),
                WordTiming(text="啊", start_ms=150, end_ms=220, confidence=0.99),
                WordTiming(text="开始", start_ms=600, end_ms=1000, confidence=0.99),
            ],
        )
        settings = CuttingSettings(
            filler_words=["嗯", "啊"],
            pause_threshold_ms=500,
            cut_padding_ms=40,
            merge_gap_ms=120,
            mode="standard",
            preserve_intro_pause=True,
        )

        suggestions = generate_cut_suggestions([segment], settings)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].reason, "filler_word")
        self.assertIn("嗯", suggestions[0].details)
        self.assertIn("啊", suggestions[0].details)


if __name__ == "__main__":
    unittest.main()
