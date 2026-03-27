from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vcut_studio.models import Project, Segment
from vcut_studio.settings import AppSettings
from vcut_studio.ui.dubbing_logic import (
    build_dubbing_summary_text,
    dub_review_status,
    pick_voice_preview_text,
)


class DubbingLogicTests(unittest.TestCase):
    def test_dub_review_status_returns_reusable_for_matching_cache(self) -> None:
        settings = AppSettings()
        settings.tts.default_voice = "emma_clear"
        settings.tts.rate = 1.0

        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "cached.wav"
            audio_path.write_bytes(b"RIFFtest")
            segment = Segment(
                segment_id="seg-001",
                start_ms=0,
                end_ms=1000,
                en_text="Hello there",
                dub_audio_path=str(audio_path),
                dub_text="Hello there",
                dub_voice_id="emma_clear",
                dub_rate=1.0,
                voice_id="emma_clear",
                tts_status="completed",
            )

            self.assertEqual(dub_review_status(segment, settings), "reusable")

    def test_dub_review_status_returns_stale_when_cache_no_longer_matches(self) -> None:
        settings = AppSettings()
        settings.tts.default_voice = "emma_clear"
        settings.tts.rate = 1.0

        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "cached.wav"
            audio_path.write_bytes(b"RIFFtest")
            segment = Segment(
                segment_id="seg-001",
                start_ms=0,
                end_ms=1000,
                en_text="Updated line",
                dub_audio_path=str(audio_path),
                dub_text="Old line",
                dub_voice_id="emma_clear",
                dub_rate=1.0,
                voice_id="emma_clear",
                tts_status="completed",
            )

            self.assertEqual(dub_review_status(segment, settings), "stale")

    def test_dub_review_status_returns_missing_text_before_pending(self) -> None:
        settings = AppSettings()
        segment = Segment(segment_id="seg-001", start_ms=0, end_ms=1000, en_text="")

        self.assertEqual(dub_review_status(segment, settings), "missing_text")

    def test_build_dubbing_summary_text_reports_multiple_states(self) -> None:
        settings = AppSettings()
        settings.tts.default_voice = "emma_clear"
        settings.tts.rate = 1.0
        project = Project.new("Demo")
        project.segments = [
            Segment(segment_id="reusable", start_ms=0, end_ms=1000, en_text="Ready", voice_id="emma_clear"),
            Segment(segment_id="pending", start_ms=1000, end_ms=2000, en_text="Need dub", voice_id="emma_clear"),
            Segment(segment_id="missing", start_ms=2000, end_ms=3000, en_text=""),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "cached.wav"
            audio_path.write_bytes(b"RIFFtest")
            project.segments[0].dub_audio_path = str(audio_path)
            project.segments[0].dub_text = "Ready"
            project.segments[0].dub_voice_id = "emma_clear"
            project.segments[0].dub_rate = 1.0
            project.segments[0].tts_status = "completed"

            summary = build_dubbing_summary_text(project, settings)

        self.assertIn("共 3 个片段", summary)
        self.assertIn("1 个可直接复用", summary)
        self.assertIn("1 个待生成", summary)
        self.assertIn("1 个缺少英文", summary)

    def test_pick_voice_preview_text_prefers_selected_segment_text(self) -> None:
        segments = [
            Segment(segment_id="seg-001", start_ms=0, end_ms=500, en_text=""),
            Segment(segment_id="seg-002", start_ms=500, end_ms=1000, en_text="Selected line"),
        ]

        text, message = pick_voice_preview_text(segments, [0, 1], "Fallback")

        self.assertEqual(text, "Selected line")
        self.assertIn("第 2 个片段", message)

    def test_pick_voice_preview_text_falls_back_to_settings_text(self) -> None:
        segments = [Segment(segment_id="seg-001", start_ms=0, end_ms=500, en_text="")]

        text, message = pick_voice_preview_text(segments, [0], "Fallback preview")

        self.assertEqual(text, "Fallback preview")
        self.assertIn("设置中的试听文本", message)

    def test_pick_voice_preview_text_raises_when_no_text_is_available(self) -> None:
        segments = [Segment(segment_id="seg-001", start_ms=0, end_ms=500, en_text="")]

        with self.assertRaisesRegex(ValueError, "没有可用于试听的英文文本"):
            pick_voice_preview_text(segments, [0], "   ")


if __name__ == "__main__":
    unittest.main()
