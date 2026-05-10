from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from vcut_studio.models import CutSuggestion, MediaInfo, Project, Segment, SlidePage
from vcut_studio.project_store import ProjectStore
from vcut_studio.settings import AppSettings, SettingsStore


def _workspace_temp_dir(prefix: str) -> Path:
    path = Path.cwd() / ".test-artifacts" / f"{prefix}-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


class ProjectStoreTests(unittest.TestCase):
    def test_project_round_trip(self) -> None:
        project = Project.new("Demo")
        project.video_path = "demo.mp4"
        project.source_duration_ms = 12_340
        project.analysis_completed = True
        project.media_info = MediaInfo(
            duration_ms=12_340,
            has_video=True,
            has_audio=True,
            width=1920,
            height=1080,
            fps=25.0,
            video_codec="h264",
            audio_codec="aac",
            sample_rate=48000,
        )
        project.segments.append(
            Segment(
                segment_id="seg-001",
                start_ms=0,
                end_ms=1200,
                zh_text="\u8fd9\u4e2a\u6211\u4eec\u5148\u8bd5\u4e00\u4e0b",
                en_text="Let us try this first.",
                dub_selected=True,
                tts_status="completed",
                dub_audio_path="dubs/seg-001.wav",
                dub_text="Let us try this first.",
                dub_voice_id="emma_clear",
                dub_rate=1.0,
                voice_id="emma_clear",
            )
        )
        project.cut_suggestions.append(
            CutSuggestion(
                suggestion_id="cut-001",
                start_ms=100,
                end_ms=220,
                reason="filler_word",
                score=0.91,
                accepted=True,
            )
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "demo.vcutproj"
            store = ProjectStore()
            store.save(project, path)
            loaded = store.load(path)

        self.assertEqual(loaded.name, "Demo")
        self.assertEqual(len(loaded.segments), 1)
        self.assertEqual(
            loaded.segments[0].zh_text,
            "\u8fd9\u4e2a\u6211\u4eec\u5148\u8bd5\u4e00\u4e0b",
        )
        self.assertEqual(loaded.segments[0].dub_voice_id, "emma_clear")
        self.assertEqual(loaded.segments[0].dub_rate, 1.0)
        self.assertTrue(loaded.segments[0].dub_selected)
        self.assertEqual(loaded.media_info.width, 1920)
        self.assertTrue(loaded.media_info.has_audio)
        self.assertTrue(loaded.analysis_completed)
        self.assertTrue(loaded.cut_suggestions[0].accepted)

    def test_ppt_project_round_trip(self) -> None:
        project = Project.new("PPT Demo", project_kind="ppt")
        project.source_ppt_path = "demo.pptx"
        project.rendered_slides_dir = "previews/demo"
        project.deck_summary = "这是一个培训课件的讲解摘要。"
        project.slides.append(
            SlidePage(
                slide_id="slide-001",
                slide_index=1,
                title="Overview",
                source_text="Intro text",
                notes_text="Speaker notes",
                preview_image_path="previews/demo/slide_001.png",
                zh_script="大家好，这一页先介绍整体内容。",
                en_script="Hello everyone. This slide introduces the overall content.",
                estimated_duration_ms=15000,
                translation_status="translated",
            )
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "ppt_demo.vcutproj"
            store = ProjectStore()
            store.save(project, path)
            loaded = store.load(path)

        self.assertEqual(loaded.project_kind, "ppt")
        self.assertEqual(loaded.source_ppt_path, "demo.pptx")
        self.assertEqual(loaded.deck_summary, "这是一个培训课件的讲解摘要。")
        self.assertEqual(len(loaded.slides), 1)
        self.assertEqual(loaded.slides[0].preview_image_path, "previews/demo/slide_001.png")
        self.assertEqual(loaded.slides[0].zh_script, "大家好，这一页先介绍整体内容。")


class SettingsStoreTests(unittest.TestCase):
    def test_default_translation_settings_leave_endpoint_and_model_blank(self) -> None:
        settings = AppSettings()

        self.assertEqual(settings.translation.base_url, "")
        self.assertEqual(settings.translation.model, "")

    def test_settings_round_trip(self) -> None:
        settings = AppSettings()
        settings.asr.model_cache_dir = "D:/models/huggingface"
        settings.asr.local_model_dir = "D:/models/faster-whisper-medium"
        settings.asr.local_files_only = True
        settings.translation.base_url = "https://example.test/v1"
        settings.translation.api_key = "dummy-api-key"
        settings.translation.model = "test-model"
        settings.translation.proxy_enabled = True
        settings.translation.http_proxy = "http://127.0.0.1:7890"
        settings.translation.https_proxy = "http://127.0.0.1:7890"
        settings.translation.no_proxy = "localhost,.corp.local"
        settings.export.subtitle_mode = "bilingual"
        settings.export.subtitle_font_size = 36
        settings.export.subtitle_english_color = "#00ff88"
        settings.export.subtitle_chinese_color = "#ffdd55"
        settings.export.subtitle_safe_area_enabled = False
        settings.export.subtitle_english_x_percent = 46
        settings.export.subtitle_english_y_percent = 91
        settings.export.subtitle_chinese_x_percent = 53
        settings.export.subtitle_chinese_y_percent = 84

        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "settings.json"
            store = SettingsStore(path)
            store.save(settings)
            loaded = store.load()

        self.assertEqual(loaded.asr.model_cache_dir, "D:/models/huggingface")
        self.assertEqual(loaded.asr.local_model_dir, "D:/models/faster-whisper-medium")
        self.assertTrue(loaded.asr.local_files_only)
        self.assertEqual(loaded.translation.base_url, "https://example.test/v1")
        self.assertEqual(loaded.translation.api_key, "dummy-api-key")
        self.assertEqual(loaded.translation.model, "test-model")
        self.assertTrue(loaded.translation.proxy_enabled)
        self.assertEqual(loaded.translation.http_proxy, "http://127.0.0.1:7890")
        self.assertEqual(loaded.translation.https_proxy, "http://127.0.0.1:7890")
        self.assertEqual(loaded.translation.no_proxy, "localhost,.corp.local")
        self.assertEqual(loaded.export.subtitle_mode, "bilingual")
        self.assertEqual(loaded.export.subtitle_font_size, 36)
        self.assertEqual(loaded.export.subtitle_english_color, "#00FF88")
        self.assertEqual(loaded.export.subtitle_chinese_color, "#FFDD55")
        self.assertFalse(loaded.export.subtitle_safe_area_enabled)
        self.assertEqual(loaded.export.subtitle_english_x_percent, 46.0)
        self.assertEqual(loaded.export.subtitle_english_y_percent, 91.0)
        self.assertEqual(loaded.export.subtitle_chinese_x_percent, 53.0)
        self.assertEqual(loaded.export.subtitle_chinese_y_percent, 84.0)

    def test_settings_store_prefers_portable_settings_next_to_frozen_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime_root = Path(tmp_dir)
            exe_path = runtime_root / "VCutStudio.exe"
            settings_path = runtime_root / "settings.json"
            exe_path.write_bytes(b"")
            settings_path.write_text('{"asr":{"model_name":"medium"}}', encoding="utf-8")

            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(exe_path)),
            ):
                store = SettingsStore()
                loaded = store.load()

        self.assertEqual(store.settings_path, settings_path)
        self.assertEqual(loaded.asr.model_name, "medium")

    def test_settings_color_without_hash_is_normalized(self) -> None:
        loaded = AppSettings.from_dict(
            {
                "export": {
                    "subtitle_english_color": "047bff",
                    "subtitle_chinese_color": "ffd966",
                    "subtitle_safe_area_enabled": False,
                    "subtitle_english_x_percent": 125,
                    "subtitle_chinese_y_percent": -15,
                }
            }
        )

        self.assertEqual(loaded.export.subtitle_english_color, "#047BFF")
        self.assertEqual(loaded.export.subtitle_chinese_color, "#FFD966")
        self.assertFalse(loaded.export.subtitle_safe_area_enabled)
        self.assertEqual(loaded.export.subtitle_english_x_percent, 100.0)
        self.assertEqual(loaded.export.subtitle_chinese_y_percent, 0.0)

    @patch("vcut_studio.settings.find_kokoro_model_dir", return_value=Path("dummy/models/csukuangfj-kokoro-multi-lang-v1_1"))
    @patch("vcut_studio.settings.kokoro_local_assets_available", return_value=True)
    @patch("vcut_studio.settings.melo_local_assets_available", return_value=True)
    def test_settings_store_migrates_default_chinese_voice_to_kokoro_male_when_available(
        self,
        _mock_melo_ready: object,
        _mock_kokoro_ready: object,
        _mock_model_dir: object,
    ) -> None:
        tmp_dir = _workspace_temp_dir("settings-store")
        try:
            path = tmp_dir / "settings.json"
            path.write_text(
                '{"tts":{"chinese_provider_type":"melo_local","chinese_default_voice":"melo_zh_female"}}',
                encoding="utf-8",
            )
            store = SettingsStore(path)
            loaded = store.load()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertEqual(loaded.tts.provider_type_for_language("zh"), "kokoro_local")
        self.assertEqual(loaded.tts.default_voice_for_language("zh"), "zm_001")


if __name__ == "__main__":
    unittest.main()
