from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from vcut_studio.models import Project, SlidePage
from vcut_studio.settings import AppSettings
from vcut_studio.ui.workers import PptExportWorker


class _FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)


class _FakePopen:
    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = 43210
        self.returncode: int | None = None
        self.stdout = _FakeStdout(
            [
                json.dumps({"type": "progress", "value": 10, "message": "子进程准备中"}, ensure_ascii=False) + "\n",
                json.dumps({"type": "progress", "value": 100, "message": "子进程导出完成"}, ensure_ascii=False) + "\n",
            ]
        )

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int:
        project_path = Path(self.command[self.command.index("--project") + 1])
        result_path = Path(self.command[self.command.index("--result") + 1])
        output_dir = Path(self.command[self.command.index("--output-dir") + 1])

        project_data = json.loads(project_path.read_text(encoding="utf-8"))
        project_data["deck_summary"] = "来自子进程的快照"
        project_data["slides"][0]["tts_status"] = "completed"
        project_data["slides"][0]["voice_id"] = "zh_male_clear"
        project_data["slides"][0]["tts_audio_path"] = str(output_dir / "slide_001.wav")

        result_path.write_text(
            json.dumps(
                {
                    "output_path": str(output_dir / "deck.mp4"),
                    "project_snapshot": project_data,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 1


class PptExportWorkerSubprocessTests(unittest.TestCase):
    def test_worker_reads_subprocess_progress_and_snapshot(self) -> None:
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
        settings = AppSettings()
        worker = PptExportWorker(
            project=project,
            settings=settings,
            output_dir="exports",
            output_container="mp4",
            burn_subtitles=True,
            export_sidecar_srt=False,
            subtitle_mode="bilingual",
            voiceover_language="zh",
            prepare_scripts=False,
        )

        finished_results: list[object] = []
        progress_updates: list[tuple[int, str]] = []
        worker.finished.connect(finished_results.append)
        worker.progress_value.connect(lambda value, message: progress_updates.append((value, message)))

        with patch("vcut_studio.ui.workers.subprocess.Popen", _FakePopen):
            worker.run()

        self.assertEqual(project.deck_summary, "")
        self.assertEqual(project.slides[0].tts_status, "pending")
        self.assertEqual(len(finished_results), 1)

        result = finished_results[0]
        self.assertEqual(result.plan.output_path, Path("exports") / "deck.mp4")
        self.assertEqual(result.project_snapshot.deck_summary, "来自子进程的快照")
        self.assertEqual(result.project_snapshot.slides[0].tts_status, "completed")
        self.assertEqual(result.project_snapshot.slides[0].voice_id, "zh_male_clear")
        self.assertTrue(any("子进程准备中" in message for _value, message in progress_updates))


if __name__ == "__main__":
    unittest.main()
