from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog

from vcut_studio.context import AppContext
from vcut_studio.models import Project, SlidePage
from vcut_studio.project_store import ProjectStore
from vcut_studio.settings import AppSettings, SettingsStore
from vcut_studio.ui.ppt_main_window import PptExportProcessController, PptMainWindow


def _qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _workspace_temp_dir() -> tempfile.TemporaryDirectory[str]:
    artifacts_root = Path.cwd() / ".test-artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=artifacts_root)


class PptMainWindowExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = _qt_app()

    def setUp(self) -> None:
        self._temp_dir = _workspace_temp_dir()
        root = Path(self._temp_dir.name)
        settings = AppSettings()
        settings.workspace.workspace_dir = str(root / "workspace")
        settings.workspace.temp_dir = str(root / "temp")
        settings.media.default_output_dir = str(root / "exports")
        settings_store = SettingsStore(root / "settings.json")
        self.context = AppContext(
            settings_store=settings_store,
            project_store=ProjectStore(),
            settings=settings,
        )
        self._voice_recorder_patcher = patch.object(
            PptMainWindow,
            "_setup_voice_input_recorder",
            autospec=True,
            return_value=None,
        )
        self._voice_recorder_patcher.start()
        self.window = PptMainWindow(self.context)

    def tearDown(self) -> None:
        self.window.close()
        self._voice_recorder_patcher.stop()
        try:
            self._temp_dir.cleanup()
        except PermissionError:
            pass

    def test_show_export_dialog_allows_missing_english_scripts(self) -> None:
        project = Project.new("Deck", project_kind="ppt")
        project.slides = [
            SlidePage(
                slide_id="slide-001",
                slide_index=1,
                title="Intro",
                preview_image_path="slide_001.png",
                zh_script="仅有中文稿",
                en_script="",
            )
        ]
        self.context.set_project(project)
        dialog_calls: list[tuple[AppSettings, object]] = []

        class _FakeDialog:
            def __init__(self, settings: AppSettings, parent: object) -> None:
                dialog_calls.append((settings, parent))

            def exec(self) -> int:
                return int(QDialog.DialogCode.Rejected)

        with (
            patch("vcut_studio.ui.ppt_main_window.PptExportDialog", _FakeDialog),
            patch("vcut_studio.ui.ppt_main_window.QMessageBox.information") as information,
        ):
            self.window.show_export_dialog()

        self.assertEqual(len(dialog_calls), 1)
        information.assert_not_called()

    def test_show_export_dialog_falls_back_to_chinese_voiceover_defaults(self) -> None:
        project = Project.new("Deck", project_kind="ppt")
        project.slides = [
            SlidePage(
                slide_id="slide-001",
                slide_index=1,
                title="Intro",
                preview_image_path="slide_001.png",
                zh_script="中文稿",
                en_script="",
            )
        ]
        self.context.set_project(project)
        captured_options: list[dict[str, object]] = []

        class _FakeDialog:
            def __init__(self, _settings: AppSettings, _parent: object) -> None:
                pass

            def exec(self) -> int:
                return int(QDialog.DialogCode.Accepted)

            def export_options(self) -> dict[str, object]:
                return {
                    "output_dir": "",
                    "output_container": "mp4",
                    "subtitle_mode": "bilingual",
                    "burn_subtitles": True,
                    "export_sidecar_srt": False,
                }

        with (
            patch("vcut_studio.ui.ppt_main_window.PptExportDialog", _FakeDialog),
            patch.object(self.context.settings_store, "save", return_value=Path(self._temp_dir.name) / "settings.json"),
            patch.object(self.window, "_start_export_job", side_effect=lambda options: captured_options.append(dict(options))),
        ):
            self.window.show_export_dialog()

        self.assertEqual(len(captured_options), 1)
        self.assertEqual(captured_options[0]["voiceover_language"], "zh")
        self.assertEqual(captured_options[0]["subtitle_mode"], "zh")
        self.assertEqual(
            captured_options[0]["selected_provider_type"],
            self.context.settings.tts.provider_type_for_language("zh"),
        )
        self.assertEqual(
            captured_options[0]["selected_voice_id"],
            self.context.settings.tts.default_voice_for_language("zh"),
        )

    def test_show_export_dialog_supplies_voiceover_defaults_before_start(self) -> None:
        project = Project.new("Deck", project_kind="ppt")
        project.slides = [
            SlidePage(
                slide_id="slide-001",
                slide_index=1,
                title="Intro",
                preview_image_path="slide_001.png",
                zh_script="中文稿",
                en_script="English script",
            )
        ]
        self.context.set_project(project)
        captured_options: list[dict[str, object]] = []

        class _FakeDialog:
            def __init__(self, _settings: AppSettings, _parent: object) -> None:
                pass

            def exec(self) -> int:
                return int(QDialog.DialogCode.Accepted)

            def export_options(self) -> dict[str, object]:
                return {
                    "output_dir": "",
                    "output_container": "mp4",
                    "subtitle_mode": "bilingual",
                    "burn_subtitles": True,
                    "export_sidecar_srt": False,
                }

        with (
            patch("vcut_studio.ui.ppt_main_window.PptExportDialog", _FakeDialog),
            patch.object(self.context.settings_store, "save", return_value=Path(self._temp_dir.name) / "settings.json"),
            patch.object(self.window, "_start_export_job", side_effect=lambda options: captured_options.append(dict(options))),
        ):
            self.window.show_export_dialog()

        self.assertEqual(len(captured_options), 1)
        self.assertEqual(captured_options[0]["voiceover_language"], "en")
        self.assertEqual(
            captured_options[0]["selected_provider_type"],
            self.context.settings.tts.provider_type_for_language("en"),
        )
        self.assertEqual(
            captured_options[0]["selected_voice_id"],
            self.context.settings.tts.default_voice_for_language("en"),
        )
        self.assertFalse(bool(captured_options[0].get("prepare_scripts", True)))

    def test_process_controller_throttles_dense_progress_updates(self) -> None:
        project = Project.new("Deck", project_kind="ppt")
        project.slides = [
            SlidePage(
                slide_id="slide-001",
                slide_index=1,
                title="Intro",
                preview_image_path="slide_001.png",
                zh_script="中文稿",
                en_script="English script",
            )
        ]
        controller = PptExportProcessController(
            project=project,
            settings=self.context.settings,
            output_dir=str(Path(self._temp_dir.name) / "exports"),
            output_container="mp4",
            burn_subtitles=True,
            export_sidecar_srt=False,
            subtitle_mode="bilingual",
            voiceover_language="en",
            selected_provider_type="",
            selected_voice_id="",
        )
        progress_updates: list[tuple[int, str]] = []
        controller.progress_value.connect(lambda value, message: progress_updates.append((value, message)))

        with patch("vcut_studio.ui.ppt_main_window.time.monotonic", side_effect=[0.0, 0.05, 0.5]):
            controller._handle_output_line(
                json.dumps({"type": "progress", "value": 10, "message": "正在准备"}, ensure_ascii=False)
            )
            controller._handle_output_line(
                json.dumps({"type": "progress", "value": 11, "message": "正在准备"}, ensure_ascii=False)
            )
            controller._handle_output_line(
                json.dumps({"type": "progress", "value": 12, "message": "继续导出"}, ensure_ascii=False)
            )

        self.assertEqual(len(progress_updates), 2)
        self.assertEqual(progress_updates[0][0], 51)
        self.assertEqual(progress_updates[1][0], 52)


if __name__ == "__main__":
    unittest.main()
