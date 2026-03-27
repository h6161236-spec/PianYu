from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, Signal, Slot

from ..asr import FasterWhisperTranscriber, TranscriptionSummary
from ..exporting import (
    ExportPlan,
    export_clean_video,
    export_english_dub_video,
    export_english_subtitle_video,
)
from ..models import Segment
from ..project_store import sanitize_filename
from ..providers.translation import (
    TranslationProviderError,
    TranslationRequest,
    TranslationResult,
    create_translation_provider,
)
from ..providers.tts import create_tts_provider
from ..settings import ASRSettings, AppSettings

PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001


class BackgroundTaskCancelled(RuntimeError):
    """Raised when the user stops the current background task."""


def _with_windows_process_handle(
    process: subprocess.Popen[str],
    callback: object,
) -> None:
    if sys.platform != "win32" or process.poll() is not None:
        return

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(
        PROCESS_SUSPEND_RESUME | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE,
        False,
        process.pid,
    )
    if not handle:
        raise OSError(f"无法访问进程 {process.pid}。")
    try:
        callback(handle)
    finally:
        kernel32.CloseHandle(handle)


def _suspend_process(process: subprocess.Popen[str]) -> None:
    def _callback(handle: int) -> None:
        status = ctypes.windll.ntdll.NtSuspendProcess(handle)  # type: ignore[attr-defined]
        if status != 0:
            raise OSError(f"暂停进程失败，状态码：{status}")

    _with_windows_process_handle(process, _callback)


def _resume_process(process: subprocess.Popen[str]) -> None:
    def _callback(handle: int) -> None:
        status = ctypes.windll.ntdll.NtResumeProcess(handle)  # type: ignore[attr-defined]
        if status != 0:
            raise OSError(f"恢复进程失败，状态码：{status}")

    _with_windows_process_handle(process, _callback)


@dataclass(slots=True)
class AnalysisResult:
    segments: list[Segment]
    summary: TranscriptionSummary


@dataclass(slots=True)
class ExportResult:
    export_type: str
    plan: ExportPlan


@dataclass(slots=True)
class TranslationJobResult:
    items: list[tuple[int, TranslationResult]]


@dataclass(slots=True)
class DubbingItemResult:
    row_index: int
    output_path: str
    voice_id: str
    text: str
    rate: float


@dataclass(slots=True)
class DubbingJobResult:
    items: list[DubbingItemResult]


@dataclass(slots=True)
class VoicePreviewResult:
    output_path: str
    voice_id: str


class ControllableWorker(QObject):
    cancelled = Signal(str)

    def __init__(
        self,
        task_label: str,
        *,
        pause_supported: bool = True,
        cancel_supported: bool = True,
    ) -> None:
        super().__init__()
        self.task_label = task_label
        self._pause_supported = pause_supported
        self._cancel_supported = cancel_supported
        self._control_condition = threading.Condition()
        self._pause_requested = False
        self._cancel_requested = False
        self._attached_process: subprocess.Popen[str] | None = None
        self._attached_process_suspended = False

    def supports_pause(self) -> bool:
        return self._pause_supported

    def supports_cancel(self) -> bool:
        return self._cancel_supported

    def is_paused(self) -> bool:
        with self._control_condition:
            return self._pause_requested and not self._cancel_requested

    def is_cancel_requested(self) -> bool:
        with self._control_condition:
            return self._cancel_requested

    def request_pause(self) -> None:
        if not self._pause_supported:
            raise RuntimeError(f"{self.task_label}暂不支持暂停。")
        with self._control_condition:
            if self._cancel_requested or self._pause_requested:
                return
            self._pause_requested = True
            self._sync_attached_process_locked()
            self._control_condition.notify_all()

    def request_resume(self) -> None:
        if not self._pause_supported:
            raise RuntimeError(f"{self.task_label}暂不支持继续。")
        with self._control_condition:
            if self._cancel_requested or not self._pause_requested:
                return
            self._pause_requested = False
            self._sync_attached_process_locked()
            self._control_condition.notify_all()

    def request_cancel(self) -> None:
        if not self._cancel_supported:
            raise RuntimeError(f"{self.task_label}暂不支持终止。")
        with self._control_condition:
            if self._cancel_requested:
                return
            self._cancel_requested = True
            self._pause_requested = False
            self._sync_attached_process_locked()
            self._control_condition.notify_all()

    def checkpoint(self) -> None:
        with self._control_condition:
            self._raise_if_cancelled_locked()
            while self._pause_requested and not self._cancel_requested:
                self._control_condition.wait()
            self._raise_if_cancelled_locked()

    def attach_process(self, process: subprocess.Popen[str]) -> None:
        with self._control_condition:
            self._attached_process = process
            self._sync_attached_process_locked()

    def detach_process(self, process: subprocess.Popen[str]) -> None:
        with self._control_condition:
            if self._attached_process is process:
                self._attached_process = None
                self._attached_process_suspended = False

    def _raise_if_cancelled_locked(self) -> None:
        if self._cancel_requested:
            raise BackgroundTaskCancelled("已终止当前后台任务。")

    def _sync_attached_process_locked(self) -> None:
        process = self._attached_process
        if process is None or process.poll() is not None:
            self._attached_process_suspended = False
            return

        if self._cancel_requested:
            self._attached_process_suspended = False
            process.terminate()
            return

        if not self._pause_supported:
            return

        if self._pause_requested:
            if not self._attached_process_suspended:
                _suspend_process(process)
                self._attached_process_suspended = True
        elif self._attached_process_suspended:
            _resume_process(process)
            self._attached_process_suspended = False


class AnalysisWorker(ControllableWorker):
    progress = Signal(str)
    progress_value = Signal(int, str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        media_path: str | Path,
        asr_settings: ASRSettings,
        default_voice: str | None,
    ) -> None:
        super().__init__("字幕识别 / 分段")
        self.media_path = str(media_path)
        self.asr_settings = asr_settings
        self.default_voice = default_voice

    @Slot()
    def run(self) -> None:
        try:
            self.checkpoint()
            self._emit_progress(3, "正在准备 ASR 分析...")
            transcriber = FasterWhisperTranscriber(self.asr_settings)
            segments, summary = transcriber.transcribe_media(
                self.media_path,
                default_voice=self.default_voice,
                progress_callback=self._emit_progress,
                checkpoint=self.checkpoint,
            )
            self._emit_progress(100, "字幕识别完成。")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:
            self.error.emit(str(exc))
            return

        self.finished.emit(
            AnalysisResult(
                segments=segments,
                summary=summary,
            )
        )

    def _emit_progress(self, value: int, message: str) -> None:
        self.progress.emit(message)
        self.progress_value.emit(value, message)


def _dub_output_dir(settings: AppSettings, project: object) -> Path:
    project_id = str(getattr(project, "project_id", "") or "project")
    project_name = str(getattr(project, "name", "") or "project")
    return (
        Path(settings.workspace.workspace_dir)
        / "dubs"
        / sanitize_filename(project_name)
        / sanitize_filename(project_id)
    )


def _preview_output_dir(settings: AppSettings) -> Path:
    return Path(settings.workspace.temp_dir) / "preview"


def _export_task_label(export_type: str) -> str:
    export_labels = {
        "clean_zh": "纯净中文版导出",
        "en_subtitle": "英文字幕导出",
        "en_dub": "英文配音导出",
        "en_dub_subtitle": "英文配音字幕导出",
    }
    return export_labels.get(export_type, "视频导出")


class ExportWorker(ControllableWorker):
    progress = Signal(str)
    progress_value = Signal(int, str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        project: object,
        settings: AppSettings,
        export_type: str,
        output_dir: str | Path,
        output_container: str,
        burn_subtitles: bool,
        export_sidecar_srt: bool,
        subtitle_mode: str,
    ) -> None:
        super().__init__(_export_task_label(export_type))
        self.project = project
        self.settings = settings
        self.export_type = export_type
        self.output_dir = str(output_dir)
        self.output_container = output_container
        self.burn_subtitles = burn_subtitles
        self.export_sidecar_srt = export_sidecar_srt
        self.subtitle_mode = subtitle_mode

    @Slot()
    def run(self) -> None:
        try:
            self.checkpoint()
            self._emit_progress(5, "正在准备导出方案...")
            if self.export_type == "clean_zh":
                plan = export_clean_video(
                    project=self.project,
                    settings=self.settings,
                    output_dir=self.output_dir,
                    container=self.output_container,
                    controller=self,
                    progress_callback=self._emit_progress,
                )
            elif self.export_type == "en_subtitle":
                plan = export_english_subtitle_video(
                    project=self.project,
                    settings=self.settings,
                    output_dir=self.output_dir,
                    container=self.output_container,
                    burn_subtitles=self.burn_subtitles,
                    export_sidecar_srt=self.export_sidecar_srt,
                    subtitle_mode=self.subtitle_mode,
                    controller=self,
                    progress_callback=self._emit_progress,
                )
            elif self.export_type in {"en_dub", "en_dub_subtitle"}:
                plan = export_english_dub_video(
                    project=self.project,
                    settings=self.settings,
                    output_dir=self.output_dir,
                    container=self.output_container,
                    burn_subtitles=self.burn_subtitles,
                    export_sidecar_srt=self.export_sidecar_srt,
                    subtitle_mode=self.subtitle_mode,
                    export_type=self.export_type,
                    controller=self,
                    progress_callback=self._emit_progress,
                )
            else:
                raise RuntimeError(f"导出类型“{self.export_type}”暂未实现。")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:
            self.error.emit(str(exc))
            return
        self.finished.emit(ExportResult(export_type=self.export_type, plan=plan))

    def _emit_progress(self, value: int, message: str) -> None:
        self.progress.emit(message)
        self.progress_value.emit(value, message)


class DubbingWorker(ControllableWorker):
    progress = Signal(str)
    progress_value = Signal(int, str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        project: object,
        settings: AppSettings,
        row_indices: list[int],
    ) -> None:
        super().__init__("英文配音生成")
        self.project = project
        self.settings = settings
        self.row_indices = row_indices

    @Slot()
    def run(self) -> None:
        try:
            provider = create_tts_provider(self.settings.tts)
            output_dir = _dub_output_dir(self.settings, self.project)
            output_dir.mkdir(parents=True, exist_ok=True)
            total_rows = len(self.row_indices)
            generated_items: list[DubbingItemResult] = []

            for item_index, row_index in enumerate(self.row_indices, start=1):
                self.checkpoint()
                segments = getattr(self.project, "segments", [])
                if not (0 <= row_index < len(segments)):
                    continue
                segment = segments[row_index]
                text = str(getattr(segment, "en_text", "")).strip()
                if not text:
                    continue
                voice_id = str(
                    getattr(segment, "voice_id", None) or self.settings.tts.default_voice or ""
                ).strip()
                if not voice_id:
                    raise RuntimeError("当前没有可用的英文音色，请先在设置里配置默认音色。")
                output_path = output_dir / (
                    f"{row_index + 1:04d}_"
                    f"{sanitize_filename(str(getattr(segment, 'segment_id', 'segment'))[:12])}_"
                    f"{sanitize_filename(voice_id)}.wav"
                )
                progress = 5 + int(((item_index - 1) / max(1, total_rows)) * 85)
                self._emit_progress(
                    progress,
                    f"正在生成第 {item_index}/{total_rows} 个片段的英文配音...",
                )
                provider.synthesize_segment(text, voice_id, output_path, controller=self)
                self.checkpoint()
                generated_items.append(
                    DubbingItemResult(
                        row_index=row_index,
                        output_path=str(output_path),
                        voice_id=voice_id,
                        text=text,
                        rate=self.settings.tts.rate,
                    )
                )

            if not generated_items:
                raise RuntimeError("所选片段中没有可生成配音的英文文本。")
            self._emit_progress(100, "英文配音生成完成。")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:
            self.error.emit(str(exc))
            return

        self.finished.emit(DubbingJobResult(items=generated_items))

    def _emit_progress(self, value: int, message: str) -> None:
        self.progress.emit(message)
        self.progress_value.emit(value, message)


class SourceProofreadWorker(ControllableWorker):
    progress = Signal(str)
    progress_value = Signal(int, str)
    partial_results = Signal(object)
    finished = Signal(int)
    error = Signal(str)

    def __init__(
        self,
        settings: AppSettings,
        row_requests: list[tuple[int, TranslationRequest]],
    ) -> None:
        super().__init__("中文字幕校正")
        self.settings = settings
        self.row_requests = row_requests

    def _is_timeout_error(self, exc: Exception) -> bool:
        message = str(exc).strip().lower()
        return any(marker in message for marker in ("timed out", "timeout", "读取超时", "超时"))

    def _proofread_batch_with_fallback(
        self,
        provider: object,
        batch: list[tuple[int, TranslationRequest]],
        batch_label: str,
    ) -> list[tuple[int, TranslationRequest]]:
        self.checkpoint()
        request_items = [request_item for _, request_item in batch]
        try:
            results = provider.proofread_segments(request_items)
        except TranslationProviderError as exc:
            if self._is_timeout_error(exc):
                if len(batch) > 1:
                    split_index = max(1, len(batch) // 2)
                    left_batch = batch[:split_index]
                    right_batch = batch[split_index:]
                    self._emit_progress(
                        5,
                        f"第 {batch_label} 批文稿校正超时，正在自动拆分重试（{len(batch)} -> {len(left_batch)} + {len(right_batch)}）...",
                    )
                    left_results = self._proofread_batch_with_fallback(provider, left_batch, batch_label + ".1")
                    right_results = self._proofread_batch_with_fallback(provider, right_batch, batch_label + ".2")
                    return left_results + right_results
                raise TranslationProviderError(
                    str(exc) + " 当前单条分段校正也超时了，请增大“超时（秒）”或稍后重试。"
                ) from exc
            raise

        self.checkpoint()
        results_by_id = {item.segment_id: item for item in results}
        corrected_items: list[tuple[int, TranslationRequest]] = []
        for row_index, request_item in batch:
            result_item = results_by_id.get(request_item.segment_id)
            if result_item is None:
                raise TranslationProviderError(
                    f"文稿校正结果中缺少分段 {request_item.segment_id}。"
                )
            corrected_text = result_item.corrected_text.strip() or request_item.source_text
            corrected_items.append(
                (
                    row_index,
                    TranslationRequest(
                        segment_id=request_item.segment_id,
                        source_text=corrected_text,
                    ),
                )
            )
        return corrected_items

    @Slot()
    def run(self) -> None:
        try:
            provider = create_translation_provider(self.settings.translation)
            batch_size = max(1, self.settings.translation.batch_size)
            corrected_count = 0
            total_batches = (len(self.row_requests) + batch_size - 1) // batch_size

            for batch_start in range(0, len(self.row_requests), batch_size):
                self.checkpoint()
                batch = self.row_requests[batch_start : batch_start + batch_size]
                batch_number = batch_start // batch_size + 1
                self._emit_progress(
                    self._progress_for_batch(batch_number - 1, total_batches),
                    f"正在校正文稿第 {batch_number}/{total_batches} 批（{len(batch)} 个分段）...",
                )
                corrected_batch = self._proofread_batch_with_fallback(
                    provider,
                    batch,
                    f"{batch_number}/{total_batches}",
                )
                payload = [(row_index, request_item.source_text) for row_index, request_item in corrected_batch]
                corrected_count += len(payload)
                self.partial_results.emit(payload)
                self._emit_progress(
                    self._progress_for_batch(batch_number, total_batches),
                    f"已完成 {batch_number}/{total_batches} 批文稿校正。",
                )
            self._emit_progress(100, "中文字幕校正完成。")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:
            self.error.emit(str(exc))
            return

        self.finished.emit(corrected_count)

    def _progress_for_batch(self, completed_batches: int, total_batches: int) -> int:
        if total_batches <= 0:
            return 5
        normalized = min(1.0, max(0.0, completed_batches / total_batches))
        return 5 + int(normalized * 90)

    def _emit_progress(self, value: int, message: str) -> None:
        self.progress.emit(message)
        self.progress_value.emit(value, message)


class TranslationWorker(ControllableWorker):
    progress = Signal(str)
    progress_value = Signal(int, str)
    source_corrected = Signal(object)
    partial_results = Signal(object)
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        settings: AppSettings,
        row_requests: list[tuple[int, TranslationRequest]],
        *,
        proofread_source: bool = False,
    ) -> None:
        super().__init__("英文翻译")
        self.settings = settings
        self.row_requests = row_requests
        self.proofread_source = proofread_source

    def _is_timeout_error(self, exc: Exception) -> bool:
        message = str(exc).strip().lower()
        return any(marker in message for marker in ("timed out", "timeout", "读取超时", "超时"))

    def _translate_batch_with_fallback(
        self,
        provider: object,
        batch: list[tuple[int, TranslationRequest]],
        batch_label: str,
    ) -> list[tuple[int, TranslationResult]]:
        self.checkpoint()
        request_items = [request_item for _, request_item in batch]
        try:
            results = provider.translate_segments(request_items)
        except TranslationProviderError as exc:
            if self._is_timeout_error(exc):
                if len(batch) > 1:
                    split_index = max(1, len(batch) // 2)
                    left_batch = batch[:split_index]
                    right_batch = batch[split_index:]
                    self._emit_progress(
                        5,
                        f"第 {batch_label} 批翻译超时，正在自动拆分重试（{len(batch)} -> {len(left_batch)} + {len(right_batch)}）..."
                    )
                    left_results = self._translate_batch_with_fallback(provider, left_batch, batch_label + ".1")
                    right_results = self._translate_batch_with_fallback(provider, right_batch, batch_label + ".2")
                    return left_results + right_results
                raise TranslationProviderError(
                    str(exc) + " 当前单条分段也超时了，请增大“超时（秒）”或稍后重试。"
                ) from exc
            raise

        self.checkpoint()
        results_by_id = {item.segment_id: item for item in results}
        translated_items: list[tuple[int, TranslationResult]] = []
        for row_index, request_item in batch:
            result_item = results_by_id.get(request_item.segment_id)
            if result_item is None:
                raise TranslationProviderError(
                    f"翻译结果中缺少分段 {request_item.segment_id}。"
                )
            translated_items.append((row_index, result_item))
        return translated_items

    def _proofread_batch_with_fallback(
        self,
        provider: object,
        batch: list[tuple[int, TranslationRequest]],
        batch_label: str,
    ) -> list[tuple[int, TranslationRequest]]:
        self.checkpoint()
        request_items = [request_item for _, request_item in batch]
        try:
            results = provider.proofread_segments(request_items)
        except TranslationProviderError as exc:
            if self._is_timeout_error(exc):
                if len(batch) > 1:
                    split_index = max(1, len(batch) // 2)
                    left_batch = batch[:split_index]
                    right_batch = batch[split_index:]
                    self._emit_progress(
                        5,
                        f"第 {batch_label} 批文稿校正超时，正在自动拆分重试（{len(batch)} -> {len(left_batch)} + {len(right_batch)}）...",
                    )
                    left_results = self._proofread_batch_with_fallback(provider, left_batch, batch_label + ".1")
                    right_results = self._proofread_batch_with_fallback(provider, right_batch, batch_label + ".2")
                    return left_results + right_results
                raise TranslationProviderError(
                    str(exc) + " 当前单条分段校正也超时了，请增大“超时（秒）”或稍后重试。"
                ) from exc
            raise

        self.checkpoint()
        results_by_id = {item.segment_id: item for item in results}
        corrected_items: list[tuple[int, TranslationRequest]] = []
        for row_index, request_item in batch:
            result_item = results_by_id.get(request_item.segment_id)
            if result_item is None:
                raise TranslationProviderError(
                    f"文稿校正结果中缺少分段 {request_item.segment_id}。"
                )
            corrected_text = result_item.corrected_text.strip() or request_item.source_text
            corrected_items.append(
                (
                    row_index,
                    TranslationRequest(
                        segment_id=request_item.segment_id,
                        source_text=corrected_text,
                    ),
                )
            )
        return corrected_items

    @Slot()
    def run(self) -> None:
        try:
            provider = create_translation_provider(self.settings.translation)
            batch_size = max(1, self.settings.translation.batch_size)
            working_requests = list(self.row_requests)
            translated_items: list[tuple[int, TranslationResult]] = []

            if self.proofread_source:
                corrected_requests: list[tuple[int, TranslationRequest]] = []
                total_correction_batches = (len(working_requests) + batch_size - 1) // batch_size
                for batch_start in range(0, len(working_requests), batch_size):
                    self.checkpoint()
                    batch = working_requests[batch_start : batch_start + batch_size]
                    batch_number = batch_start // batch_size + 1
                    self._emit_progress(
                        self._progress_for_range(batch_number - 1, total_correction_batches, 5, 42),
                        f"正在校正文稿第 {batch_number}/{total_correction_batches} 批（{len(batch)} 个分段）...",
                    )
                    corrected_batch = self._proofread_batch_with_fallback(
                        provider,
                        batch,
                        f"{batch_number}/{total_correction_batches}",
                    )
                    corrected_requests.extend(corrected_batch)
                    self.source_corrected.emit(
                        [(row_index, request_item.source_text) for row_index, request_item in corrected_batch]
                    )
                    self._emit_progress(
                        self._progress_for_range(batch_number, total_correction_batches, 5, 42),
                        f"已完成 {batch_number}/{total_correction_batches} 批文稿校正。",
                    )
                working_requests = corrected_requests

            total_translation_batches = (len(working_requests) + batch_size - 1) // batch_size
            translation_start = 48 if self.proofread_source else 5
            for batch_start in range(0, len(working_requests), batch_size):
                self.checkpoint()
                batch = working_requests[batch_start : batch_start + batch_size]
                request_items = [request_item for _, request_item in batch]
                batch_number = batch_start // batch_size + 1
                self._emit_progress(
                    self._progress_for_range(batch_number - 1, total_translation_batches, translation_start, 95),
                    f"正在翻译第 {batch_number}/{total_translation_batches} 批（{len(request_items)} 个分段）..."
                )
                translated_batch = self._translate_batch_with_fallback(
                    provider,
                    batch,
                    f"{batch_number}/{total_translation_batches}",
                )
                translated_items.extend(translated_batch)
                self.partial_results.emit(translated_batch)
                self._emit_progress(
                    self._progress_for_range(batch_number, total_translation_batches, translation_start, 95),
                    f"已完成 {batch_number}/{total_translation_batches} 批翻译。",
                )
            self._emit_progress(100, "翻译完成。")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except Exception as exc:
            self.error.emit(str(exc))
            return

        self.finished.emit(TranslationJobResult(items=translated_items))

    def _progress_for_range(
        self,
        completed_batches: int,
        total_batches: int,
        start_value: int,
        end_value: int,
    ) -> int:
        if total_batches <= 0:
            return start_value
        normalized = min(1.0, max(0.0, completed_batches / total_batches))
        return start_value + int(normalized * max(0, end_value - start_value))

    def _emit_progress(self, value: int, message: str) -> None:
        self.progress.emit(message)
        self.progress_value.emit(value, message)


class VoicePreviewWorker(ControllableWorker):
    progress = Signal(str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        settings: AppSettings,
        voice_id: str,
        preview_text: str,
    ) -> None:
        super().__init__("音色试听")
        self.settings = settings
        self.voice_id = voice_id
        self.preview_text = preview_text

    @Slot()
    def run(self) -> None:
        try:
            provider = create_tts_provider(self.settings.tts)
            preview_dir = _preview_output_dir(self.settings)
            preview_dir.mkdir(parents=True, exist_ok=True)
            output_path = preview_dir / (
                f"preview_{sanitize_filename(self.voice_id or 'voice')}_{uuid4().hex[:8]}.wav"
            )
            sample_text = self.preview_text.strip() or self.settings.tts.preview_text.strip()
            if not sample_text:
                sample_text = "Welcome to VCut Studio. This is a voice preview."
            self.progress.emit("正在生成音色试听...")
            provider.synthesize_segment(
                sample_text,
                self.voice_id,
                output_path,
                controller=self,
            )
        except Exception as exc:
            self.error.emit(str(exc))
            return

        self.finished.emit(
            VoicePreviewResult(
                output_path=str(output_path),
                voice_id=self.voice_id,
            )
        )


class TranslationTestWorker(ControllableWorker):
    progress = Signal(str)
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, settings: AppSettings) -> None:
        super().__init__("翻译配置测试", pause_supported=False, cancel_supported=False)
        self.settings = settings

    @Slot()
    def run(self) -> None:
        try:
            self.progress.emit("正在测试翻译提供方连接...")
            provider = create_translation_provider(self.settings.translation)
            sample = provider.test_connection()
        except Exception as exc:
            self.error.emit(str(exc))
            return
        self.finished.emit(sample)
