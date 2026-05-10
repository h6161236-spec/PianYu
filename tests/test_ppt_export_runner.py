from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vcut_studio.models import Project, SlidePage
from vcut_studio.settings import AppSettings


class PptExportRunnerTests(unittest.TestCase):
    def test_runner_selects_language_specific_tts_settings(self) -> None:
        project = Project.new("Deck", project_kind="ppt")
        project.slides = [
            SlidePage(
                slide_id="slide-001",
                slide_index=1,
                title="Intro",
                preview_image_path="slide_001.png",
                zh_script="中文口播",
                en_script="English voiceover",
            )
        ]
        settings = AppSettings()
        settings.tts.provider_type = "kokoro_local"
        settings.tts.default_voice = "am_adam"
        settings.tts.chinese_provider_type = "melo_local"
        settings.tts.chinese_default_voice = "melo_zh_female"

        captured: dict[str, object] = {}

        def _fake_export(**kwargs: object) -> object:
            export_settings = kwargs["settings"]
            captured["provider_type"] = export_settings.tts.provider_type
            captured["default_voice"] = export_settings.tts.default_voice

            class _Plan:
                output_path = Path("exports/deck.mp4")

            return _Plan()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            project_path = temp_root / "project.vcutproj"
            settings_path = temp_root / "settings.json"
            result_path = temp_root / "result.json"
            project_path.write_text(json.dumps(project.to_dict(), ensure_ascii=False), encoding="utf-8")
            settings_path.write_text(json.dumps(settings.to_dict(), ensure_ascii=False), encoding="utf-8")

            argv = [
                "ppt_export_runner",
                "--project",
                str(project_path),
                "--settings",
                str(settings_path),
                "--result",
                str(result_path),
                "--output-dir",
                str(temp_root / "exports"),
                "--container",
                "mp4",
                "--subtitle-mode",
                "zh",
                "--voiceover-language",
                "zh",
                "--selected-voice-id",
                "melo_zh_female",
            ]

            with patch("sys.argv", argv), patch(
                "vcut_studio.ppt_export_runner.export_ppt_voiceover_video",
                _fake_export,
            ):
                from vcut_studio.ppt_export_runner import main

                self.assertEqual(main(), 0)

        self.assertEqual(captured["provider_type"], "melo_local")
        self.assertEqual(captured["default_voice"], "melo_zh_female")


if __name__ == "__main__":
    unittest.main()
