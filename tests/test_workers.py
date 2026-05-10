from __future__ import annotations

import shutil
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from vcut_studio.exporting import ExportPlan
from vcut_studio.models import Project, SlidePage
from vcut_studio.providers.translation import (
    CorrectionResult,
    TranslationProviderError,
    TranslationRequest,
    TranslationResult,
)
from vcut_studio.settings import AppSettings
from vcut_studio.ui.workers import (
    BackgroundTaskCancelled,
    ControllableWorker,
    PptExportWorker,
    SourceProofreadWorker,
    TranslationWorker,
)


class DummyWorker(ControllableWorker):
    def __init__(self) -> None:
        super().__init__("测试任务")


def _workspace_temp_dir(prefix: str) -> Path:
    path = Path.cwd() / ".test-artifacts" / f"{prefix}-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 12345
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 1


class WorkerControlTests(unittest.TestCase):
    def test_checkpoint_waits_until_resume(self) -> None:
        worker = DummyWorker()
        worker.request_pause()
        started = threading.Event()
        released = threading.Event()

        def _run_checkpoint() -> None:
            started.set()
            worker.checkpoint()
            released.set()

        thread = threading.Thread(target=_run_checkpoint, daemon=True)
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        self.assertFalse(released.wait(timeout=0.2))

        worker.request_resume()

        self.assertTrue(released.wait(timeout=1))
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())

    def test_checkpoint_raises_after_cancel(self) -> None:
        worker = DummyWorker()
        worker.request_cancel()

        with self.assertRaises(BackgroundTaskCancelled):
            worker.checkpoint()

    @patch("vcut_studio.ui.workers._resume_process")
    @patch("vcut_studio.ui.workers._suspend_process")
    def test_pause_and_resume_control_attached_process(
        self,
        suspend_process: object,
        resume_process: object,
    ) -> None:
        worker = DummyWorker()
        process = FakeProcess()
        worker.attach_process(process)

        worker.request_pause()
        suspend_process.assert_called_once_with(process)

        worker.request_resume()
        resume_process.assert_called_once_with(process)

    def test_cancel_terminates_attached_process(self) -> None:
        worker = DummyWorker()
        process = FakeProcess()
        worker.attach_process(process)

        worker.request_cancel()

        self.assertTrue(process.terminated)


class TranslationWorkerRetryTests(unittest.TestCase):
    def test_translation_worker_splits_batch_after_timeout(self) -> None:
        settings = AppSettings()
        worker = TranslationWorker(
            settings=settings,
            row_requests=[
                (0, TranslationRequest(segment_id="seg1", source_text="你好")),
                (1, TranslationRequest(segment_id="seg2", source_text="世界")),
            ],
        )

        class FakeProvider:
            def translate_segments(self, requests: list[TranslationRequest]) -> list[TranslationResult]:
                if len(requests) > 1:
                    raise TranslationProviderError("翻译接口读取超时（60 秒）。")
                return [
                    TranslationResult(
                        segment_id=requests[0].segment_id,
                        translated_text="OK-" + requests[0].segment_id,
                    )
                ]

        results = worker._translate_batch_with_fallback(
            FakeProvider(),
            worker.row_requests,
            "1/1",
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][1].translated_text, "OK-seg1")
        self.assertEqual(results[1][1].translated_text, "OK-seg2")

    def test_translation_worker_can_proofread_batch(self) -> None:
        settings = AppSettings()
        worker = TranslationWorker(
            settings=settings,
            row_requests=[(0, TranslationRequest(segment_id="seg1", source_text="泥好"))],
            proofread_source=True,
        )

        class FakeProvider:
            def proofread_segments(self, requests: list[TranslationRequest]) -> list[CorrectionResult]:
                return [
                    CorrectionResult(
                        segment_id=requests[0].segment_id,
                        corrected_text="你好",
                    )
                ]

        results = worker._proofread_batch_with_fallback(
            FakeProvider(),
            worker.row_requests,
            "1/1",
        )

        self.assertEqual(results[0][1].source_text, "你好")


class SourceProofreadWorkerTests(unittest.TestCase):
    def test_source_proofread_worker_splits_batch_after_timeout(self) -> None:
        settings = AppSettings()
        worker = SourceProofreadWorker(
            settings=settings,
            row_requests=[
                (0, TranslationRequest(segment_id="seg1", source_text="泥好")),
                (1, TranslationRequest(segment_id="seg2", source_text="世介")),
            ],
        )

        class FakeProvider:
            def proofread_segments(self, requests: list[TranslationRequest]) -> list[CorrectionResult]:
                if len(requests) > 1:
                    raise TranslationProviderError("翻译接口读取超时（60 秒）。")
                return [
                    CorrectionResult(
                        segment_id=requests[0].segment_id,
                        corrected_text=f"校正-{requests[0].segment_id}",
                    )
                ]

        results = worker._proofread_batch_with_fallback(
            FakeProvider(),
            worker.row_requests,
            "1/1",
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][1].source_text, "校正-seg1")
        self.assertEqual(results[1][1].source_text, "校正-seg2")

class PptExportWorkerTests(unittest.TestCase):
    def test_ppt_export_worker_uses_project_snapshot_for_quick_export(self) -> None:
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
        )
        finished_results: list[object] = []
        worker.finished.connect(finished_results.append)

        tmp_dir = _workspace_temp_dir("ppt-export-worker")
        try:
            output_path = tmp_dir / "deck.mp4"

            def _fake_export(project: Project, **_kwargs: object) -> ExportPlan:
                project.deck_summary = "snapshot only"
                project.slides[0].zh_script = "快照中文稿"
                project.slides[0].en_script = "Snapshot English"
                project.slides[0].translation_status = "translated"
                project.slides[0].tts_status = "completed"
                project.slides[0].voice_id = "zm_yunxi"
                project.slides[0].tts_audio_path = "tts/slide_001.wav"
                project.slides[0].actual_tts_duration_ms = 2300
                return ExportPlan(cut_ranges=[], keep_ranges=[], output_path=output_path)

            with patch("vcut_studio.ui.workers.export_ppt_voiceover_video", side_effect=_fake_export):
                worker.run()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertEqual(project.deck_summary, "")
        self.assertEqual(project.slides[0].zh_script, "中文稿")
        self.assertEqual(project.slides[0].en_script, "English script")
        self.assertEqual(project.slides[0].tts_status, "pending")
        self.assertEqual(len(finished_results), 1)
        result = finished_results[0]
        self.assertEqual(result.plan.output_path, output_path)
        self.assertIsNot(result.project_snapshot, project)
        self.assertEqual(result.project_snapshot.deck_summary, "snapshot only")
        self.assertEqual(result.project_snapshot.slides[0].voice_id, "zm_yunxi")


if __name__ == "__main__":
    unittest.main()
