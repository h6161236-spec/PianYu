from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vcut_studio.models import CutSuggestion, MediaInfo, Project, Segment
from vcut_studio.project_store import ProjectStore
from vcut_studio.settings import AppSettings, SettingsStore


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


if __name__ == "__main__":
    unittest.main()
