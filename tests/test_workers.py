from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from vcut_studio.models import Project
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
    SourceProofreadWorker,
    TranslationWorker,
)


class DummyWorker(ControllableWorker):
    def __init__(self) -> None:
        super().__init__("测试任务")


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


if __name__ == "__main__":
    unittest.main()
