from __future__ import annotations

import shutil
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from vcut_studio.models import Project, SlidePage
from vcut_studio.ppt_exporting import export_ppt_voiceover_video
from vcut_studio.settings import AppSettings
from vcut_studio.ui.workers import PptExportWorker


def _write_dummy_wav(path: Path, duration_ms: int = 800) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_rate = 22050
    frame_count = int(frame_rate * duration_ms / 1000)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(frame_rate)
        wav_file.writeframes(b"\x00\x00" * frame_count)


class _FakeTtsProvider:
    def synthesize_segment(
        self,
        text: str,
        voice_id: str,
        output_path: str | Path,
        controller: object | None = None,
    ) -> Path:
        _write_dummy_wav(Path(output_path), duration_ms=900 if "agenda" in text.lower() else 700)
        return Path(output_path)


class PptExportTests(unittest.TestCase):
    def _fake_render_slide_segment(
        self,
        image_path: str | Path,
        audio_path: str | Path,
        duration_ms: int,
        output_path: str | Path,
        *,
        settings: AppSettings,
        controller: object | None = None,
    ) -> Path:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            f"{Path(image_path).name}|{Path(audio_path).name}|{duration_ms}".encode("utf-8")
        )
        return destination

    def _fake_concat_segments(
        self,
        segment_paths: list[Path],
        output_path: str | Path,
        *,
        settings: AppSettings,
        controller: object | None = None,
    ) -> Path:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\n".join(path.read_bytes() for path in segment_paths))
        return destination

    def _fake_render_video_with_subtitles(
        self,
        base_video_path: str | Path,
        output_path: str | Path,
        *,
        settings: AppSettings,
        container: str,
        burn_subtitles: bool,
        has_audio: bool,
        srt_path: str | Path,
        subtitle_mode: str,
        chinese_srt_path: str | Path | None = None,
        english_srt_path: str | Path | None = None,
        controller: object | None = None,
    ) -> Path:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(base_video_path, destination)
        return destination

    def test_export_ppt_voiceover_video_creates_video_and_srt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            preview_dir = root / "previews"
            export_dir = root / "exports"
            preview_dir.mkdir(parents=True, exist_ok=True)
            for slide_index in (1, 2):
                (preview_dir / f"slide_{slide_index:03d}.png").write_bytes(b"fake-png")

            project = Project.new("PPT Demo", project_kind="ppt")
            project.slides = [
                SlidePage(
                    slide_id="slide-001",
                    slide_index=1,
                    title="Cover",
                    preview_image_path=str(preview_dir / "slide_001.png"),
                    zh_script="这一页是封面。",
                    en_script="This is the cover slide.",
                    voice_id="af_bella",
                ),
                SlidePage(
                    slide_id="slide-002",
                    slide_index=2,
                    title="Agenda",
                    preview_image_path=str(preview_dir / "slide_002.png"),
                    zh_script="这一页介绍目录。",
                    en_script="This slide introduces the agenda.",
                    voice_id="af_bella",
                ),
            ]

            settings = AppSettings()
            settings.workspace.workspace_dir = str(root / "workspace")
            settings.media.default_output_dir = str(export_dir)

            with (
                patch("vcut_studio.ppt_exporting.create_tts_provider", return_value=_FakeTtsProvider()),
                patch("vcut_studio.ppt_exporting._render_slide_segment", side_effect=self._fake_render_slide_segment),
                patch("vcut_studio.ppt_exporting._concat_segments", side_effect=self._fake_concat_segments),
                patch(
                    "vcut_studio.ppt_exporting.render_video_with_subtitles",
                    side_effect=self._fake_render_video_with_subtitles,
                ),
            ):
                plan = export_ppt_voiceover_video(
                    project,
                    settings,
                    export_dir,
                    "mp4",
                    burn_subtitles=True,
                    export_sidecar_srt=True,
                    subtitle_mode="bilingual",
                )

            self.assertTrue(plan.output_path.exists())
            self.assertTrue(plan.output_path.with_suffix(".srt").exists())
            srt_text = plan.output_path.with_suffix(".srt").read_text(encoding="utf-8")
            self.assertIn("这一页是封面。", srt_text)
            self.assertIn("This slide introduces the agenda.", srt_text)
            self.assertEqual(project.slides[0].tts_status, "completed")
            self.assertGreater(project.slides[0].actual_tts_duration_ms, 0)

    def test_export_ppt_voiceover_video_allows_chinese_voiceover_without_english_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            preview_path = root / "slide_001.png"
            preview_path.write_bytes(b"fake-png")

            project = Project.new("PPT Missing Script", project_kind="ppt")
            project.slides = [
                SlidePage(
                    slide_id="slide-001",
                    slide_index=1,
                    title="Only Slide",
                    preview_image_path=str(preview_path),
                    zh_script="这是中文稿。",
                    en_script="",
                )
            ]
            settings = AppSettings()
            settings.workspace.workspace_dir = str(root / "workspace")
            progress_messages: list[str] = []

            with (
                patch("vcut_studio.ppt_exporting.create_tts_provider", return_value=_FakeTtsProvider()),
                patch("vcut_studio.ppt_exporting._render_slide_segment", side_effect=self._fake_render_slide_segment),
                patch("vcut_studio.ppt_exporting._concat_segments", side_effect=self._fake_concat_segments),
                patch(
                    "vcut_studio.ppt_exporting.render_video_with_subtitles",
                    side_effect=self._fake_render_video_with_subtitles,
                ),
            ):
                plan = export_ppt_voiceover_video(
                    project,
                    settings,
                    root / "exports",
                    "mp4",
                    burn_subtitles=True,
                    export_sidecar_srt=False,
                    subtitle_mode="zh",
                    voiceover_language="zh",
                    progress_callback=lambda _value, message: progress_messages.append(message),
                )

            self.assertTrue(plan.output_path.exists())
            self.assertTrue(any("正在准备中文配音" in message for message in progress_messages))
            self.assertTrue(any("正在生成第 1/1 页的中文配音" in message for message in progress_messages))
            self.assertFalse(any("英文配音" in message for message in progress_messages))

    def test_export_ppt_voiceover_video_falls_back_to_chinese_when_english_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            preview_path = root / "slide_001.png"
            preview_path.write_bytes(b"fake-png")

            project = Project.new("PPT Fallback", project_kind="ppt")
            project.slides = [
                SlidePage(
                    slide_id="slide-001",
                    slide_index=1,
                    title="Only Slide",
                    preview_image_path=str(preview_path),
                    zh_script="??????",
                    en_script="",
                )
            ]
            settings = AppSettings()
            settings.workspace.workspace_dir = str(root / "workspace")

            with (
                patch("vcut_studio.ppt_exporting.create_tts_provider", return_value=_FakeTtsProvider()),
                patch("vcut_studio.ppt_exporting._render_slide_segment", side_effect=self._fake_render_slide_segment),
                patch("vcut_studio.ppt_exporting._concat_segments", side_effect=self._fake_concat_segments),
                patch(
                    "vcut_studio.ppt_exporting.render_video_with_subtitles",
                    side_effect=self._fake_render_video_with_subtitles,
                ),
            ):
                plan = export_ppt_voiceover_video(
                    project,
                    settings,
                    root / "exports",
                    "mp4",
                    burn_subtitles=True,
                    export_sidecar_srt=True,
                    subtitle_mode="bilingual",
                    voiceover_language="en",
                )

            self.assertTrue(plan.output_path.exists())
            self.assertTrue(plan.output_path.with_suffix(".srt").exists())
            srt_text = plan.output_path.with_suffix(".srt").read_text(encoding="utf-8")
            self.assertIn("??????", srt_text)
            self.assertEqual(project.slides[0].tts_status, "completed")

    def test_prepare_scripts_skips_translation_for_chinese_voiceover_with_chinese_subtitles(self) -> None:
        project = Project.new("PPT Chinese Only", project_kind="ppt")
        project.slides = [
            SlidePage(
                slide_id="slide-001",
                slide_index=1,
                title="Only Slide",
                preview_image_path="unused.png",
                zh_script="这是中文稿。",
                en_script="",
            )
        ]
        settings = AppSettings()
        worker = PptExportWorker(
            project=project,
            settings=settings,
            output_dir="exports",
            output_container="mp4",
            burn_subtitles=True,
            export_sidecar_srt=False,
            subtitle_mode="zh",
            voiceover_language="zh",
        )

        with (
            patch("vcut_studio.ui.workers.generate_deck_summary") as mock_summary,
            patch("vcut_studio.ui.workers.generate_chinese_slide_scripts") as mock_generate_zh,
            patch("vcut_studio.ui.workers.translate_chinese_scripts_to_english") as mock_translate_en,
        ):
            worker._prepare_scripts()

        mock_summary.assert_not_called()
        mock_generate_zh.assert_not_called()
        mock_translate_en.assert_not_called()
        self.assertEqual(project.slides[0].zh_script, "这是中文稿。")
        self.assertEqual(project.slides[0].en_script, "")
