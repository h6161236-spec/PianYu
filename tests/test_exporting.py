from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from vcut_studio.exporting import (
    DubAudioInput,
    accepted_cut_ranges,
    build_dub_cues,
    build_dub_video_timeline,
    build_export_plan,
    build_stretched_dub_cues,
    build_subtitle_cues,
    build_subtitle_cues_from_dub_cues,
    bilingual_subtitle_filter_expression,
    can_reuse_segment_dub,
    export_clean_video,
    export_english_dub_video,
    export_english_subtitle_video,
    ffmpeg_filter_path,
    keep_ranges_from_cuts,
    subtitle_filter_expression,
    write_ass_subtitle_file,
)
from vcut_studio.media import probe_media
from vcut_studio.models import CutSuggestion, MediaInfo, Project, Segment
from vcut_studio.settings import AppSettings
from vcut_studio.subtitles import SubtitleCueData


class ExportPlanningTests(unittest.TestCase):
    def test_accepted_cut_ranges_merge_overlap(self) -> None:
        suggestions = [
            CutSuggestion("a", 100, 220, "filler_word", 0.9, accepted=True),
            CutSuggestion("b", 200, 340, "long_pause", 0.8, accepted=True),
            CutSuggestion("c", 900, 1000, "long_pause", 0.7, accepted=False),
        ]
        cut_ranges = accepted_cut_ranges(suggestions, total_duration_ms=1500)
        self.assertEqual([(item.start_ms, item.end_ms) for item in cut_ranges], [(100, 340)])

    def test_keep_ranges_from_cuts(self) -> None:
        keep_ranges = keep_ranges_from_cuts(
            2000,
            [
                type("Range", (), {"start_ms": 100, "end_ms": 300})(),
                type("Range", (), {"start_ms": 900, "end_ms": 1200})(),
            ],
        )
        self.assertEqual(
            [(item.start_ms, item.end_ms) for item in keep_ranges],
            [(0, 100), (300, 900), (1200, 2000)],
        )

    def test_build_export_plan_uses_accepted_suggestions(self) -> None:
        project = Project.new("Plan Demo")
        project.video_path = "demo.mp4"
        project.source_duration_ms = 5000
        project.media_info = MediaInfo(duration_ms=5000, has_video=True, has_audio=True)
        project.cut_suggestions = [
            CutSuggestion("cut1", 500, 900, "filler_word", 0.9, accepted=True),
            CutSuggestion("cut2", 2000, 2400, "long_pause", 0.8, accepted=False),
        ]
        settings = AppSettings()

        with tempfile.TemporaryDirectory() as tmp_dir:
            plan = build_export_plan(
                project=project,
                settings=settings,
                export_type="clean_zh",
                output_dir=tmp_dir,
                container="mp4",
            )

        self.assertEqual([(item.start_ms, item.end_ms) for item in plan.cut_ranges], [(500, 900)])
        self.assertEqual(
            [(item.start_ms, item.end_ms) for item in plan.keep_ranges],
            [(0, 500), (900, 5000)],
        )

    def test_build_subtitle_cues_retimes_around_cuts(self) -> None:
        project = Project.new("Subtitle Demo")
        project.source_duration_ms = 5000
        project.media_info = MediaInfo(duration_ms=5000, has_video=True, has_audio=True)
        project.segments = [
            Segment(segment_id="s1", start_ms=0, end_ms=1000, zh_text="一", en_text="One"),
            Segment(segment_id="s2", start_ms=1200, end_ms=2200, zh_text="二", en_text="Two"),
        ]
        keep_ranges = keep_ranges_from_cuts(5000, accepted_cut_ranges([CutSuggestion("c1", 500, 1500, "long_pause", 0.8, accepted=True)], 5000))
        cues = build_subtitle_cues(project, keep_ranges)
        self.assertEqual([(cue.start_ms, cue.end_ms, cue.text) for cue in cues], [(0, 500, "One"), (500, 1200, "Two")])

    def test_build_subtitle_cues_supports_bilingual_mode(self) -> None:
        project = Project.new("Bilingual Subtitle Demo")
        project.source_duration_ms = 2000
        project.media_info = MediaInfo(duration_ms=2000, has_video=True, has_audio=True)
        project.segments = [
            Segment(segment_id="s1", start_ms=0, end_ms=1000, zh_text="你好", en_text="Hello"),
        ]
        cues = build_subtitle_cues(project, [type("Range", (), {"start_ms": 0, "end_ms": 2000, "duration_ms": 2000})()], subtitle_mode="bilingual")
        self.assertEqual([(cue.start_ms, cue.end_ms, cue.text) for cue in cues], [(0, 1000, "你好\nHello")])

    def test_build_dub_cues_merges_segment_across_removed_ranges(self) -> None:
        project = Project.new("Dub Demo")
        project.source_duration_ms = 5000
        project.media_info = MediaInfo(duration_ms=5000, has_video=True, has_audio=True)
        project.segments = [
            Segment(segment_id="s1", start_ms=0, end_ms=2200, zh_text="涓€", en_text="One line"),
        ]
        keep_ranges = keep_ranges_from_cuts(
            5000,
            accepted_cut_ranges(
                [CutSuggestion("c1", 500, 1500, "long_pause", 0.8, accepted=True)],
                5000,
            ),
        )
        cues = build_dub_cues(project, keep_ranges, default_voice="emma_clear")
        self.assertEqual(
            [(cue.start_ms, cue.end_ms, cue.text, cue.voice_id) for cue in cues],
            [(0, 1200, "One line", "emma_clear")],
        )

    def test_build_dub_video_timeline_stretches_longer_english_segment(self) -> None:
        dub_inputs = [
            DubAudioInput(
                cue=type(
                    "Cue",
                    (),
                    {
                        "index": 1,
                        "segment_index": 0,
                        "start_ms": 200,
                        "end_ms": 700,
                        "text": "Long English line",
                        "voice_id": "emma_clear",
                    },
                )(),
                clip_path=Path("demo.wav"),
                clip_duration_ms=1200,
            )
        ]
        timeline = build_dub_video_timeline(1000, dub_inputs)
        self.assertEqual(
            [
                (segment.source_start_ms, segment.source_end_ms, segment.output_start_ms, segment.output_end_ms)
                for segment in timeline
            ],
            [(0, 200, 0, 200), (200, 700, 200, 1400), (700, 1000, 1400, 1700)],
        )

    def test_build_stretched_dub_cues_updates_output_range(self) -> None:
        cue = type(
            "Cue",
            (),
            {
                "index": 1,
                "segment_index": 0,
                "start_ms": 200,
                "end_ms": 700,
                "text": "Long English line",
                "voice_id": "emma_clear",
            },
        )()
        dub_inputs = [DubAudioInput(cue=cue, clip_path=Path("demo.wav"), clip_duration_ms=1200)]
        timeline = build_dub_video_timeline(1000, dub_inputs)
        stretched_cues = build_stretched_dub_cues(dub_inputs, timeline)
        self.assertEqual(
            [(item.start_ms, item.end_ms, item.text) for item in stretched_cues],
            [(200, 1400, "Long English line")],
        )

    def test_build_subtitle_cues_from_dub_cues_uses_stretched_ranges(self) -> None:
        project = Project.new("Stretched Subtitle Demo")
        project.segments = [
            Segment(segment_id="s1", start_ms=0, end_ms=1000, zh_text="你好", en_text="Hello there"),
        ]
        dub_cues = [
            type(
                "Cue",
                (),
                {
                    "index": 1,
                    "segment_index": 0,
                    "start_ms": 0,
                    "end_ms": 1600,
                },
            )()
        ]
        cues = build_subtitle_cues_from_dub_cues(project, dub_cues, subtitle_mode="bilingual")
        self.assertEqual([(cue.start_ms, cue.end_ms, cue.text) for cue in cues], [(0, 1600, "你好\nHello there")])

    def test_subtitle_filter_expression_includes_configured_font_size(self) -> None:
        settings = AppSettings()
        settings.export.subtitle_font_size = 40

        expression = subtitle_filter_expression(settings, "demo subtitles.srt")

        self.assertEqual(
            expression,
            (
                f"subtitles='{ffmpeg_filter_path('demo subtitles.srt')}':"
                "force_style='FontSize=40,Alignment=2,PrimaryColour=&H00FFFFFF,"
                "Outline=0,Shadow=0,BorderStyle=1,OutlineColour=&H00000000,BackColour=&H00000000'"
            ),
        )

    def test_subtitle_filter_expression_accepts_color_without_hash(self) -> None:
        settings = AppSettings()
        settings.export.subtitle_english_color = "047bff"

        expression = subtitle_filter_expression(settings, "demo subtitles.srt")

        self.assertIn("PrimaryColour=&H00FF7B04", expression)

    def test_bilingual_subtitle_filter_expression_uses_separate_colors(self) -> None:
        settings = AppSettings()
        settings.export.subtitle_font_size = 30
        settings.export.subtitle_chinese_color = "#FF0000"
        settings.export.subtitle_english_color = "#00FF00"

        expression = bilingual_subtitle_filter_expression(
            settings,
            "chinese.srt",
            "english.srt",
        )

        self.assertEqual(
            expression,
            (
                f"subtitles='{ffmpeg_filter_path('chinese.srt')}':"
                "force_style='FontSize=30,Alignment=2,PrimaryColour=&H000000FF,"
                "Outline=0,Shadow=0,BorderStyle=1,OutlineColour=&H00000000,BackColour=&H00000000,MarginV=84',"
                f"subtitles='{ffmpeg_filter_path('english.srt')}':"
                "force_style='FontSize=30,Alignment=2,PrimaryColour=&H0000FF00,"
                "Outline=0,Shadow=0,BorderStyle=1,OutlineColour=&H00000000,BackColour=&H00000000,MarginV=36'"
            ),
        )

    def test_write_ass_subtitle_file_uses_requested_resolution_and_font_size(self) -> None:
        settings = AppSettings()
        settings.export.subtitle_font_size = 36
        settings.export.subtitle_english_x_percent = 40
        settings.export.subtitle_english_y_percent = 75

        with tempfile.TemporaryDirectory() as tmp_dir:
            ass_path = write_ass_subtitle_file(
                [
                    SubtitleCueData(
                        index=1,
                        start_ms=0,
                        end_ms=1000,
                        text="Hello subtitle",
                    )
                ],
                Path(tmp_dir) / "subtitle.ass",
                settings=settings,
                subtitle_language="en",
                play_res_x=1280,
                play_res_y=720,
            )

            ass_text = ass_path.read_text(encoding="utf-8")

        self.assertIn("PlayResX: 1280", ass_text)
        self.assertIn("PlayResY: 720", ass_text)
        self.assertIn("Style: Default,Arial,54,", ass_text)
        self.assertIn(r"{\an5\pos(512,540)}Hello subtitle", ass_text)

    def test_write_ass_subtitle_file_wraps_long_english_lines(self) -> None:
        settings = AppSettings()
        settings.export.subtitle_font_size = 28

        with tempfile.TemporaryDirectory() as tmp_dir:
            ass_path = write_ass_subtitle_file(
                [
                    SubtitleCueData(
                        index=1,
                        start_ms=0,
                        end_ms=1000,
                        text="This is a very long English subtitle line that should wrap before it runs outside the video frame.",
                    )
                ],
                Path(tmp_dir) / "subtitle_wrap_en.ass",
                settings=settings,
                subtitle_language="en",
                play_res_x=640,
                play_res_y=360,
            )

            ass_text = ass_path.read_text(encoding="utf-8")

        self.assertIn(r"\N", ass_text)

    def test_write_ass_subtitle_file_wraps_long_chinese_lines(self) -> None:
        settings = AppSettings()
        settings.export.subtitle_font_size = 28

        with tempfile.TemporaryDirectory() as tmp_dir:
            ass_path = write_ass_subtitle_file(
                [
                    SubtitleCueData(
                        index=1,
                        start_ms=0,
                        end_ms=1000,
                        text="这是一条特别长的中文字幕内容用于测试在导出视频的时候会不会自动换行避免超出画面范围。",
                    )
                ],
                Path(tmp_dir) / "subtitle_wrap_zh.ass",
                settings=settings,
                subtitle_language="zh",
                play_res_x=640,
                play_res_y=360,
            )

            ass_text = ass_path.read_text(encoding="utf-8")

        self.assertIn(r"\N", ass_text)

    def test_can_reuse_segment_dub_matches_cached_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            audio_path = Path(tmp_dir) / "cached.wav"
            audio_path.write_bytes(b"RIFFtest")
            segment = Segment(
                segment_id="s1",
                start_ms=0,
                end_ms=1000,
                zh_text="浣犲ソ",
                en_text="Hello",
                dub_audio_path=str(audio_path),
                dub_text="Hello",
                dub_voice_id="emma_clear",
                dub_rate=1.0,
            )

            reused = can_reuse_segment_dub(segment, voice_id="emma_clear", rate=1.0, text="Hello")
            self.assertEqual(reused, audio_path)
            self.assertIsNone(
                can_reuse_segment_dub(segment, voice_id="emma_clear", rate=1.1, text="Hello")
            )


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for export integration tests")
class ExportIntegrationTests(unittest.TestCase):
    def test_export_clean_video_creates_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            source = temp_root / "source.mp4"
            command = [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-shortest",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(source),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)

            project = Project.new("Export Demo")
            project.video_path = str(source)
            project.source_duration_ms = 1000
            project.media_info = MediaInfo(
                duration_ms=1000,
                has_video=True,
                has_audio=True,
                width=320,
                height=240,
                fps=25.0,
                video_codec="h264",
                audio_codec="aac",
                sample_rate=44100,
            )
            project.cut_suggestions = [
                CutSuggestion("cut1", 200, 400, "long_pause", 0.8, accepted=True),
            ]

            settings = AppSettings()
            settings.media.ffmpeg_path = "ffmpeg"
            output_plan = export_clean_video(
                project=project,
                settings=settings,
                output_dir=temp_root / "exports",
                container="mp4",
            )

            self.assertTrue(output_plan.output_path.exists())
            self.assertGreater(output_plan.output_path.stat().st_size, 0)

    def test_export_english_subtitle_video_creates_output_and_srt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            source = temp_root / "source.mp4"
            command = [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-shortest",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(source),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)

            project = Project.new("Subtitle Export Demo")
            project.video_path = str(source)
            project.source_duration_ms = 1000
            project.media_info = MediaInfo(
                duration_ms=1000,
                has_video=True,
                has_audio=True,
                width=320,
                height=240,
                fps=25.0,
                video_codec="h264",
                audio_codec="aac",
                sample_rate=44100,
            )
            project.segments = [
                Segment(segment_id="s1", start_ms=0, end_ms=1000, zh_text="你好", en_text="Hello"),
            ]

            settings = AppSettings()
            settings.media.ffmpeg_path = "ffmpeg"
            plan = export_english_subtitle_video(
                project=project,
                settings=settings,
                output_dir=temp_root / "exports",
                container="mp4",
                burn_subtitles=False,
                export_sidecar_srt=True,
            )

            self.assertTrue(plan.output_path.exists())
            self.assertTrue(plan.output_path.with_suffix(".srt").exists())

    @unittest.skipUnless(
        sys.platform == "win32" and shutil.which("powershell"),
        "Windows PowerShell voices are required for dub export integration tests",
    )
    def test_export_english_dub_video_creates_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            source = temp_root / "source.mp4"
            command = [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=3",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-shortest",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(source),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)

            project = Project.new("Dub Export Demo")
            project.video_path = str(source)
            project.source_duration_ms = 3000
            project.media_info = MediaInfo(
                duration_ms=3000,
                has_video=True,
                has_audio=True,
                width=320,
                height=240,
                fps=25.0,
                video_codec="h264",
                audio_codec="aac",
                sample_rate=44100,
            )
            project.segments = [
                Segment(
                    segment_id="s1",
                    start_ms=0,
                    end_ms=3000,
                    zh_text="浣犲ソ",
                    en_text="Hello from VCut Studio.",
                    voice_id="emma_clear",
                ),
            ]

            settings = AppSettings()
            settings.media.ffmpeg_path = "ffmpeg"
            plan = export_english_dub_video(
                project=project,
                settings=settings,
                output_dir=temp_root / "exports",
                container="mp4",
            )

            self.assertTrue(plan.output_path.exists())
            self.assertGreater(plan.output_path.stat().st_size, 0)

    def test_export_english_dub_video_extends_video_for_longer_cached_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            source = temp_root / "source.mp4"
            command = [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-shortest",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(source),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)

            cached_wav = temp_root / "cached_long.wav"
            with wave.open(str(cached_wav), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(22050)
                wav_file.writeframes(b"\x00\x00" * 22050 * 2)

            project = Project.new("Dub Stretch Demo")
            project.video_path = str(source)
            project.source_duration_ms = 1000
            project.media_info = MediaInfo(
                duration_ms=1000,
                has_video=True,
                has_audio=True,
                width=320,
                height=240,
                fps=25.0,
                video_codec="h264",
                audio_codec="aac",
                sample_rate=44100,
            )
            project.segments = [
                Segment(
                    segment_id="s1",
                    start_ms=0,
                    end_ms=1000,
                    zh_text="你好",
                    en_text="This is a much longer English sentence.",
                    voice_id="emma_clear",
                    dub_audio_path=str(cached_wav),
                    dub_text="This is a much longer English sentence.",
                    dub_voice_id="emma_clear",
                    dub_rate=1.0,
                ),
            ]

            settings = AppSettings()
            settings.media.ffmpeg_path = "ffmpeg"
            plan = export_english_dub_video(
                project=project,
                settings=settings,
                output_dir=temp_root / "exports",
                container="mp4",
            )

            probe = probe_media(plan.output_path, settings.media.ffmpeg_path)
            self.assertTrue(plan.output_path.exists())
            self.assertGreaterEqual(probe.media_info.duration_ms, 1800)


if __name__ == "__main__":
    unittest.main()
