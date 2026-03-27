from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vcut_studio.subtitles import SubtitleCueData, parse_subtitle_text, read_subtitle_file, write_subtitle_file


class SubtitleHelpersTests(unittest.TestCase):
    def test_parse_srt_text(self) -> None:
        cues = parse_subtitle_text(
            "1\n"
            "00:00:00,000 --> 00:00:01,200\n"
            "你好，世界\n\n"
            "2\n"
            "00:00:01,300 --> 00:00:02,000\n"
            "第二行\n"
        )
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start_ms, 0)
        self.assertEqual(cues[0].end_ms, 1200)
        self.assertEqual(cues[0].text, "你好，世界")

    def test_parse_vtt_text(self) -> None:
        cues = parse_subtitle_text(
            "WEBVTT\n\n"
            "1\n"
            "00:00:00.000 --> 00:00:01.500\n"
            "Hello world\n"
        )
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].start_ms, 0)
        self.assertEqual(cues[0].end_ms, 1500)
        self.assertEqual(cues[0].text, "Hello world")

    def test_write_and_read_srt_file_round_trip(self) -> None:
        cues = [
            SubtitleCueData(index=1, start_ms=0, end_ms=1000, text="第一句"),
            SubtitleCueData(index=2, start_ms=1200, end_ms=2500, text="Second line"),
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "demo.srt"
            write_subtitle_file(cues, output_path)
            loaded = read_subtitle_file(output_path)

        self.assertEqual(loaded, cues)

    def test_write_vtt_file_includes_header(self) -> None:
        cues = [SubtitleCueData(index=1, start_ms=0, end_ms=800, text="测试")]
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "demo.vtt"
            write_subtitle_file(cues, output_path)
            content = output_path.read_text(encoding="utf-8")

        self.assertIn("WEBVTT", content)
        self.assertIn("00:00:00.000 --> 00:00:00.800", content)


if __name__ == "__main__":
    unittest.main()
