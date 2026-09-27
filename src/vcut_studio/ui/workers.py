from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, Signal, Slot

from ..asr import FasterWhisperTranscriber, TranscriptionError, TranscriptionSummary
from ..exporting import (
    ExportPlan,
    ExportError,
    export_clean_video,
    export_english_dub_video,
    export_english_subtitle_video,
)
from ..models import Project, Segment
from ..ppt_io import PptImportResult, import_presentation
from ..ppt_exporting import (
    export_ppt_voiceover_video,
    resolve_ppt_subtitle_mode,
    resolve_ppt_voiceover_language,
)
from ..ppt_scripts import (
    generate_chinese_slide_scripts,
    generate_deck_summary,
    translate_chinese_scripts_to_english,
)
from ..process_utils import subprocess_windowless_kwargs
from ..project_store import sanitize_filename
from ..providers.translation import (
    TranslationProviderError,
    TranslationRequest,
    TranslationResult,
    create_translation_provider,
)
from ..providers.tts import TTSProviderError, create_tts_provider
from ..settings import ASRSettings, AppSettings

PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001


class BackgroundTaskCancelled(RuntimeError):
    """Raised when the user stops the current background task."""


def _format_worker_error(task_name: str, exc: Exception) -> str:
    message = str(exc).strip()
    if not message:
        message = exc.__class__.__name__
    return f"{task_name}失败：{message}"


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
        raise OSError(f"?????? {process.pid}?")
    try:
        callback(handle)
    finally:
        kernel32.CloseHandle(handle)


def _suspend_process(process: subprocess.Popen[str]) -> None:
    def _callback(handle: int) -> None:
        status = ctypes.windll.ntdll.NtSuspendProcess(handle)  # type: ignore[attr-defined]
        if status != 0:
            raise OSError(f"???????????{status}")

    _with_windows_process_handle(process, _callback)


def _resume_process(process: subprocess.Popen[str]) -> None:
    def _callback(handle: int) -> None:
        status = ctypes.windll.ntdll.NtResumeProcess(handle)  # type: ignore[attr-defined]
        if status != 0:
            raise OSError(f"???????????{status}")

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
class PptExportResult:
    plan: ExportPlan
    project_snapshot: Project | None = None


@dataclass(slots=True)
class PptVoiceInputResult:
    text: str
    audio_path: str
    segment_count: int


@dataclass(slots=True)
class PptImportJobResult:
    import_result: PptImportResult


@dataclass(slots=True)
class TranslationJobResult:
    items: list[tuple[int, TranslationResult]]


@dataclass(slots=True)
class PptScriptDraftItem:
    slide_index: int
    zh_script: str
    estimated_duration_ms: int


@dataclass(slots=True)
class PptScriptDraftJobResult:
    deck_summary: str
    items: list[PptScriptDraftItem]


@dataclass(slots=True)
class PptScriptTranslationItem:
    slide_index: int
    en_script: str


@dataclass(slots=True)
class PptScriptTranslationJobResult:
    deck_summary: str
    items: list[PptScriptTranslationItem]


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
        self._last_progress_emit_at = 0.0
        self._last_progress_value: int | None = None
        self._last_progress_message = ""

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
            raise RuntimeError(f"{self.task_label} ???????")
        with self._control_condition:
            if self._cancel_requested or self._pause_requested:
                return
            self._pause_requested = True
            self._sync_attached_process_locked()
            self._control_condition.notify_all()

    def request_resume(self) -> None:
        if not self._pause_supported:
            raise RuntimeError(f"{self.task_label} ???????")
        with self._control_condition:
            if self._cancel_requested or not self._pause_requested:
                return
            self._pause_requested = False
            self._sync_attached_process_locked()
            self._control_condition.notify_all()

    def request_cancel(self) -> None:
        if not self._cancel_supported:
            raise RuntimeError(f"{self.task_label} ???????")
        with self._control_condition:
            if self._cancel_requested:
                return
            self._cancel_requested = True
            self._pause_requested = False
            self._sync_attached_process_locked()
            self._control_condition.notify_all()

    def checkpoint(self) -> None:
        # Yield briefly so the GUI thread can keep processing input during long Python-side loops.
        time.sleep(0.001)
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

    def emit_progress_update(
        self,
        progress_signal: Signal,
        progress_value_signal: Signal,
        value: int,
        message: str,
    ) -> None:
        clamped_value = max(0, min(int(value), 100))
        normalized_message = str(message or "")
        now = time.monotonic()

        should_emit = False
        if self._last_progress_value is None:
            should_emit = True
        elif clamped_value in {0, 100}:
            should_emit = True
        elif clamped_value != self._last_progress_value and abs(clamped_value - self._last_progress_value) >= 2:
            should_emit = True
        elif normalized_message != self._last_progress_message and now - self._last_progress_emit_at >= 0.15:
            should_emit = True
        elif now - self._last_progress_emit_at >= 0.4:
            should_emit = True

        if not should_emit:
            return

        self._last_progress_emit_at = now
        self._last_progress_value = clamped_value
        self._last_progress_message = normalized_message
        progress_signal.emit(normalized_message)
        progress_value_signal.emit(clamped_value, normalized_message)

    def _raise_if_cancelled_locked(self) -> None:
        if self._cancel_requested:
            raise BackgroundTaskCancelled("??????????")

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
        super().__init__("瀛楀箷璇嗗埆 / 鍒嗘")
        self.media_path = str(media_path)
        self.asr_settings = asr_settings
        self.default_voice = default_voice

    @Slot()
    def run(self) -> None:
        try:
            self.checkpoint()
            self._emit_progress(3, "姝ｅ湪鍑嗗 ASR 鍒嗘瀽...")
            transcriber = FasterWhisperTranscriber(self.asr_settings)
            segments, summary = transcriber.transcribe_media(
                self.media_path,
                default_voice=self.default_voice,
                progress_callback=self._emit_progress,
                checkpoint=self.checkpoint,
            )
            self._emit_progress(100, "???????")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except TranscriptionError as exc:
            self.error.emit(_format_worker_error("字幕识别", exc))
            return
        except Exception as exc:
            self.error.emit(_format_worker_error("字幕识别", exc))
            return

        self.finished.emit(
            AnalysisResult(
                segments=segments,
                summary=summary,
            )
        )

    def _emit_progress(self, value: int, message: str) -> None:
        self.emit_progress_update(self.progress, self.progress_value, value, message)


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
        "clean_zh": "???????",
        "en_subtitle": "鑻辨枃瀛楀箷瀵煎嚭",
        "en_dub": "鑻辨枃閰嶉煶瀵煎嚭",
        "en_dub_subtitle": "鑻辨枃閰嶉煶瀛楀箷瀵煎嚭",
    }
    return export_labels.get(export_type, "瑙嗛瀵煎嚭")


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
        voiceover_language: str = "zh",
        selected_provider_type: str = "",
        selected_voice_id: str = "",
        prepare_scripts: bool = True,
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
        self.voiceover_language = str(voiceover_language or "zh").strip().lower()
        self.selected_provider_type = str(selected_provider_type or "").strip()
        self.selected_voice_id = str(selected_voice_id or "").strip()
        self.prepare_scripts = bool(prepare_scripts)

    def _emit_stage_progress(
        self,
        start: int,
        end: int,
        completed: int,
        total: int,
        label: str,
        slide_index: int,
    ) -> None:
        self.checkpoint()
        normalized_total = max(1, total)
        progress = start + int((completed / normalized_total) * max(1, end - start))
        self._emit_progress(
            progress,
            f"{label} {completed}/{normalized_total}，当前第 {slide_index} 页",
        )

    def _require_translation_ready(self) -> None:
        missing: list[str] = []
        if not self.settings.translation.base_url.strip():
            missing.append("Base URL")
        if not self.settings.translation.api_key.strip():
            missing.append("API Key")
        if not self.settings.translation.model.strip():
            missing.append("模型名称")
        if missing:
            raise RuntimeError("当前导出需要自动生成或翻译稿件，请先在设置里补全：\n- " + "\n- ".join(missing))

    def _prepare_scripts(self, project: object | None = None) -> None:
        target_project = self.project if project is None else project
        slides = list(getattr(target_project, "slides", []))
        if not slides:
            raise RuntimeError("当前没有可导出的 PPT 页面。")

        deck_name = str(getattr(target_project, "name", "") or "未命名演示")
        deck_summary = str(getattr(target_project, "deck_summary", "") or "").strip()
        effective_voiceover_language = resolve_ppt_voiceover_language(target_project, self.voiceover_language)
        effective_subtitle_mode = resolve_ppt_subtitle_mode(target_project, self.subtitle_mode)
        need_english = (
            effective_voiceover_language == "en"
            or effective_subtitle_mode in {"en", "bilingual"}
        )
        need_chinese = (
            effective_voiceover_language == "zh"
            or effective_subtitle_mode in {"zh", "bilingual"}
            or need_english
        )

        missing_chinese = [slide.slide_index for slide in slides if not slide.zh_script.strip()]
        if need_chinese and missing_chinese:
            self._require_translation_ready()
            if not deck_summary:
                self._emit_progress(4, "正在分析整套 PPT 结构...")
                deck_summary = generate_deck_summary(self.settings.translation, deck_name, slides)
                target_project.deck_summary = deck_summary
            self._emit_progress(8, "正在生成中文口播稿...")
            generated_items = generate_chinese_slide_scripts(
                self.settings.translation,
                deck_name,
                slides,
                missing_chinese,
                deck_summary=deck_summary,
                progress_callback=lambda completed, total, slide_index: self._emit_stage_progress(
                    8,
                    28,
                    completed,
                    total,
                    "正在生成中文稿：",
                    slide_index,
                ),
            )
            slide_by_index = {slide.slide_index: slide for slide in slides}
            for slide_index, zh_script, estimated_duration_ms in generated_items:
                slide = slide_by_index.get(slide_index)
                if slide is None:
                    continue
                slide.zh_script = zh_script
                slide.estimated_duration_ms = estimated_duration_ms
                if not slide.en_script.strip():
                    slide.translation_status = "pending"

        missing_english = [slide.slide_index for slide in slides if not slide.en_script.strip()]
        if need_english and missing_english:
            self._require_translation_ready()
            if not deck_summary:
                self._emit_progress(30, "正在分析整套 PPT 结构...")
                deck_summary = generate_deck_summary(self.settings.translation, deck_name, slides)
                target_project.deck_summary = deck_summary
            self._emit_progress(34, "正在翻译英文口播稿...")
            translated_items = translate_chinese_scripts_to_english(
                self.settings.translation,
                deck_name,
                slides,
                missing_english,
                deck_summary=deck_summary,
                progress_callback=lambda completed, total, slide_index: self._emit_stage_progress(
                    34,
                    45,
                    completed,
                    total,
                    "正在补齐英文稿：",
                    slide_index,
                ),
            )
            slide_by_index = {slide.slide_index: slide for slide in slides}
            for slide_index, en_script in translated_items:
                slide = slide_by_index.get(slide_index)
                if slide is None:
                    continue
                slide.en_script = en_script
                slide.translation_status = "translated"

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
                raise RuntimeError(f"暂不支持导出类型 {self.export_type!r}。")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except (ExportError, TTSProviderError, TranslationProviderError, RuntimeError) as exc:
            self.error.emit(_format_worker_error(self.task_label, exc))
            return
        except Exception as exc:
            self.error.emit(_format_worker_error(self.task_label, exc))
            return
        self.finished.emit(ExportResult(export_type=self.export_type, plan=plan))

    def _emit_progress(self, value: int, message: str) -> None:
        self.emit_progress_update(self.progress, self.progress_value, value, message)


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
        super().__init__("鑻辨枃閰嶉煶鐢熸垚")
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
                    raise RuntimeError("?????????????????")
                output_path = output_dir / (
                    f"{row_index + 1:04d}_"
                    f"{sanitize_filename(str(getattr(segment, 'segment_id', 'segment'))[:12])}_"
                    f"{sanitize_filename(voice_id)}.wav"
                )
                progress = 5 + int(((item_index - 1) / max(1, total_rows)) * 85)
                self._emit_progress(
                    progress,
                    f"????? {item_index}/{total_rows} ????????...",
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
                raise RuntimeError("?????????????????")
            self._emit_progress(100, "?????????")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except TTSProviderError as exc:
            self.error.emit(_format_worker_error("配音生成", exc))
            return
        except Exception as exc:
            self.error.emit(_format_worker_error("配音生成", exc))
            return

        self.finished.emit(DubbingJobResult(items=generated_items))

    def _emit_progress(self, value: int, message: str) -> None:
        self.emit_progress_update(self.progress, self.progress_value, value, message)


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
        super().__init__("涓枃瀛楀箷鏍℃")
        self.settings = settings
        self.row_requests = row_requests

    def _is_timeout_error(self, exc: Exception) -> bool:
        message = str(exc).strip().lower()
        return any(
            marker in message
            for marker in ("timed out", "timeout", "读取超时", "超时", "璇诲彇瓒呮椂", "瓒呮椂")
        )

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
                        f"? {batch_label} ?????????????????{len(batch)} -> {len(left_batch)} + {len(right_batch)}?...",
                    )
                    left_results = self._proofread_batch_with_fallback(provider, left_batch, batch_label + ".1")
                    right_results = self._proofread_batch_with_fallback(provider, right_batch, batch_label + ".2")
                    return left_results + right_results
                raise TranslationProviderError(
                    str(exc) + " ????????????????"
                ) from exc
            raise

        self.checkpoint()
        results_by_id = {item.segment_id: item for item in results}
        corrected_items: list[tuple[int, TranslationRequest]] = []
        for row_index, request_item in batch:
            result_item = results_by_id.get(request_item.segment_id)
            if result_item is None:
                raise TranslationProviderError(
                    f"??????????{request_item.segment_id}?"
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
                    f"??????? {batch_number}/{total_batches} ??{len(batch)} ????...",
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
                        f"??? {batch_number}/{total_batches} ??????",
                )
            self._emit_progress(100, "?????????")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except TranslationProviderError as exc:
            self.error.emit(_format_worker_error("中文字幕校正", exc))
            return
        except Exception as exc:
            self.error.emit(_format_worker_error("中文字幕校正", exc))
            return

        self.finished.emit(corrected_count)

    def _progress_for_batch(self, completed_batches: int, total_batches: int) -> int:
        if total_batches <= 0:
            return 5
        normalized = min(1.0, max(0.0, completed_batches / total_batches))
        return 5 + int(normalized * 90)

    def _emit_progress(self, value: int, message: str) -> None:
        self.emit_progress_update(self.progress, self.progress_value, value, message)


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
        super().__init__("鑻辨枃缈昏瘧")
        self.settings = settings
        self.row_requests = row_requests
        self.proofread_source = proofread_source

    def _is_timeout_error(self, exc: Exception) -> bool:
        message = str(exc).strip().lower()
        return any(
            marker in message
            for marker in ("timed out", "timeout", "读取超时", "超时", "璇诲彇瓒呮椂", "瓒呮椂")
        )

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
                        f"? {batch_label} ???????????????{len(batch)} -> {len(left_batch)} + {len(right_batch)}?...",
                    )
                    left_results = self._translate_batch_with_fallback(provider, left_batch, batch_label + ".1")
                    right_results = self._translate_batch_with_fallback(provider, right_batch, batch_label + ".2")
                    return left_results + right_results
                raise TranslationProviderError(
                    str(exc) + " ????????????????"
                ) from exc
            raise

        self.checkpoint()
        results_by_id = {item.segment_id: item for item in results}
        translated_items: list[tuple[int, TranslationResult]] = []
        for row_index, request_item in batch:
            result_item = results_by_id.get(request_item.segment_id)
            if result_item is None:
                raise TranslationProviderError(
                    f"?????????{request_item.segment_id}?"
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
                        f"? {batch_label} ?????????????????{len(batch)} -> {len(left_batch)} + {len(right_batch)}?...",
                    )
                    left_results = self._proofread_batch_with_fallback(provider, left_batch, batch_label + ".1")
                    right_results = self._proofread_batch_with_fallback(provider, right_batch, batch_label + ".2")
                    return left_results + right_results
                raise TranslationProviderError(
                    str(exc) + " ????????????????"
                ) from exc
            raise

        self.checkpoint()
        results_by_id = {item.segment_id: item for item in results}
        corrected_items: list[tuple[int, TranslationRequest]] = []
        for row_index, request_item in batch:
            result_item = results_by_id.get(request_item.segment_id)
            if result_item is None:
                raise TranslationProviderError(
                    f"?????????{request_item.segment_id}?"
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
                        f"??????? {batch_number}/{total_correction_batches} ??{len(batch)} ????...",
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
                        f"??? {batch_number}/{total_correction_batches} ??????",
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
                    f"????? {batch_number}/{total_translation_batches} ??{len(request_items)} ????...",
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
                    f"??? {batch_number}/{total_translation_batches} ??????",
                )
            self._emit_progress(100, "?????")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except TranslationProviderError as exc:
            self.error.emit(_format_worker_error("翻译任务", exc))
            return
        except Exception as exc:
            self.error.emit(_format_worker_error("翻译任务", exc))
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
        self.emit_progress_update(self.progress, self.progress_value, value, message)


class PptImportWorker(ControllableWorker):
    progress = Signal(str)
    progress_value = Signal(int, str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, ppt_path: str | Path, rendered_slides_dir: str | Path) -> None:
        super().__init__("PPT 瀵煎叆", pause_supported=False, cancel_supported=False)
        self.ppt_path = str(ppt_path)
        self.rendered_slides_dir = str(rendered_slides_dir)

    @Slot()
    def run(self) -> None:
        try:
            import_result = import_presentation(
                self.ppt_path,
                self.rendered_slides_dir,
                progress_callback=self._emit_progress,
            )
        except Exception as exc:
            self.error.emit(_format_worker_error("PPT 导入", exc))
            return

        self.finished.emit(PptImportJobResult(import_result=import_result))

    def _emit_progress(self, value: int, message: str) -> None:
        self.emit_progress_update(self.progress, self.progress_value, value, message)


class PptScriptGenerationWorker(ControllableWorker):
    progress = Signal(str)
    progress_value = Signal(int, str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        project: object,
        settings: AppSettings,
        slide_indices: list[int],
    ) -> None:
        super().__init__("PPT 中文稿生成")
        self.project = project
        self.settings = settings
        self.slide_indices = slide_indices

    @Slot()
    def run(self) -> None:
        try:
            slides = list(getattr(self.project, "slides", []))
            if not slides:
                raise RuntimeError("当前没有可生成中文稿的 PPT 页面。")
            self.checkpoint()
            self._emit_progress(8, "正在分析整套 PPT 结构...")
            deck_summary = generate_deck_summary(
                self.settings.translation,
                str(getattr(self.project, "name", "") or "未命名演示"),
                slides,
            )
            self.checkpoint()
            self._emit_progress(20, "正在生成中文口播稿...")
            generated_items = generate_chinese_slide_scripts(
                self.settings.translation,
                str(getattr(self.project, "name", "") or "未命名演示"),
                slides,
                self.slide_indices,
                deck_summary=deck_summary,
                progress_callback=self._on_slide_progress,
            )
            self.checkpoint()
            self._emit_progress(100, "中文口播稿生成完成。")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except TranslationProviderError as exc:
            self.error.emit(_format_worker_error("中文口播稿生成", exc))
            return
        except Exception as exc:
            self.error.emit(_format_worker_error("中文口播稿生成", exc))
            return

        self.finished.emit(
            PptScriptDraftJobResult(
                deck_summary=deck_summary,
                items=[
                    PptScriptDraftItem(
                        slide_index=slide_index,
                        zh_script=zh_script,
                        estimated_duration_ms=estimated_duration_ms,
                    )
                    for slide_index, zh_script, estimated_duration_ms in generated_items
                ],
            )
        )

    def _emit_progress(self, value: int, message: str) -> None:
        self.emit_progress_update(self.progress, self.progress_value, value, message)

    def _on_slide_progress(self, completed: int, total: int, slide_index: int) -> None:
        normalized_total = max(1, total)
        progress = 20 + int((completed / normalized_total) * 78)
        self._emit_progress(
            progress,
            f"正在生成中文稿 {completed}/{normalized_total}，当前第 {slide_index} 页",
        )


class PptScriptTranslationWorker(ControllableWorker):
    progress = Signal(str)
    progress_value = Signal(int, str)
    item_translated = Signal(object)
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        project: object,
        settings: AppSettings,
        slide_indices: list[int],
    ) -> None:
        super().__init__("PPT 英文稿翻译")
        self.project = project
        self.settings = settings
        self.slide_indices = slide_indices

    @Slot()
    def run(self) -> None:
        try:
            slides = list(getattr(self.project, "slides", []))
            if not slides:
                raise RuntimeError("当前没有可翻译英文稿的 PPT 页面。")
            self.checkpoint()
            self._emit_progress(8, "正在整理中文口播稿...")
            deck_summary = str(getattr(self.project, "deck_summary", "") or "").strip()
            if not deck_summary:
                deck_summary = generate_deck_summary(
                    self.settings.translation,
                    str(getattr(self.project, "name", "") or "未命名演示"),
                    slides,
                )
            self.checkpoint()
            self._emit_progress(20, "正在翻译英文口播稿...")
            translated_items: list[tuple[int, str]] = []
            total_items = len(self.slide_indices)
            completed_items = 0
            requested_batch_size = max(1, int(getattr(self.settings.translation, "batch_size", 8) or 8))
            for batch_start in range(0, len(self.slide_indices), requested_batch_size):
                self.checkpoint()
                batch_indices = self.slide_indices[batch_start : batch_start + requested_batch_size]
                batch_result = translate_chinese_scripts_to_english(
                    self.settings.translation,
                    str(getattr(self.project, "name", "") or "未命名演示"),
                    slides,
                    batch_indices,
                    deck_summary=deck_summary,
                    batch_size=len(batch_indices),
                )
                if not batch_result:
                    raise TranslationProviderError(
                        "当前批次没有返回可用的英文翻译："
                        + ", ".join(str(item) for item in batch_indices)
                    )
                for translated_slide_index, en_script in batch_result:
                    translated_items.append((translated_slide_index, en_script))
                    self.item_translated.emit(
                        PptScriptTranslationItem(
                            slide_index=translated_slide_index,
                            en_script=en_script,
                        )
                    )
                    completed_items += 1
                    self._on_slide_progress(completed_items, total_items, translated_slide_index)
            self.checkpoint()
            self._emit_progress(100, "英文口播稿翻译完成。")
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except TranslationProviderError as exc:
            self.error.emit(_format_worker_error("英文口播稿翻译", exc))
            return
        except Exception as exc:
            self.error.emit(_format_worker_error("英文口播稿翻译", exc))
            return

        self.finished.emit(
            PptScriptTranslationJobResult(
                deck_summary=deck_summary,
                items=[
                    PptScriptTranslationItem(
                        slide_index=slide_index,
                        en_script=en_script,
                    )
                    for slide_index, en_script in translated_items
                ],
            )
        )

    def _emit_progress(self, value: int, message: str) -> None:
        self.emit_progress_update(self.progress, self.progress_value, value, message)

    def _on_slide_progress(self, completed: int, total: int, slide_index: int) -> None:
        normalized_total = max(1, total)
        progress = 20 + int((completed / normalized_total) * 78)
        self._emit_progress(
            progress,
            f"正在翻译英文稿 {completed}/{normalized_total}，当前第 {slide_index} 页",
        )


class PptExportWorker(ExportWorker):
    progress = Signal(str)
    progress_value = Signal(int, str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(
        self,
        project: object,
        settings: AppSettings,
        output_dir: str | Path,
        output_container: str,
        burn_subtitles: bool,
        export_sidecar_srt: bool,
        subtitle_mode: str,
        voiceover_language: str = "zh",
        selected_provider_type: str = "",
        selected_voice_id: str = "",
        prepare_scripts: bool = True,
    ) -> None:
        super().__init__(
            project=project,
            settings=settings,
            export_type="ppt_voiceover",
            output_dir=output_dir,
            output_container=output_container,
            burn_subtitles=burn_subtitles,
            export_sidecar_srt=export_sidecar_srt,
            subtitle_mode=subtitle_mode,
            voiceover_language=voiceover_language,
            selected_provider_type=selected_provider_type,
            selected_voice_id=selected_voice_id,
            prepare_scripts=prepare_scripts,
        )

    @Slot()
    def run(self) -> None:
        try:
            self.checkpoint()
            export_settings = AppSettings.from_dict(self.settings.to_dict())
            export_project = Project.from_dict(self.project.to_dict())
            export_settings.tts.provider_type = export_settings.tts.provider_type_for_language(
                self.voiceover_language
            )
            export_settings.tts.default_voice = export_settings.tts.default_voice_for_language(
                self.voiceover_language
            )
            if self.selected_provider_type:
                export_settings.tts.provider_type = self.selected_provider_type
            if self.selected_voice_id:
                export_settings.tts.default_voice = self.selected_voice_id
            if self.prepare_scripts:
                self._prepare_scripts(export_project)
            self.checkpoint()
            plan, export_project = self._run_export_in_subprocess(export_project, export_settings)
        except BackgroundTaskCancelled as exc:
            self.cancelled.emit(str(exc))
            return
        except (ExportError, TTSProviderError, TranslationProviderError, RuntimeError) as exc:
            self.error.emit(_format_worker_error("PPT 导出", exc))
            return
        except Exception as exc:
            self.error.emit(_format_worker_error("PPT 导出", exc))
            return

        self.finished.emit(PptExportResult(plan=plan, project_snapshot=export_project))

    def _emit_progress(self, value: int, message: str) -> None:
        self.emit_progress_update(self.progress, self.progress_value, value, message)

    def _run_export_in_subprocess(
        self,
        export_project: Project,
        export_settings: AppSettings,
    ) -> tuple[ExportPlan, Project]:
        with tempfile.TemporaryDirectory(prefix="vcut_ppt_export_job_") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            project_path = temp_dir / "project.vcutproj"
            settings_path = temp_dir / "settings.json"
            result_path = temp_dir / "result.json"
            project_path.write_text(
                json.dumps(export_project.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            settings_path.write_text(
                json.dumps(export_settings.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            runner_args = [
                "--project",
                str(project_path),
                "--settings",
                str(settings_path),
                "--result",
                str(result_path),
                "--output-dir",
                self.output_dir,
                "--container",
                self.output_container,
                "--subtitle-mode",
                self.subtitle_mode,
                "--voiceover-language",
                self.voiceover_language,
                "--selected-voice-id",
                self.selected_voice_id or export_settings.tts.default_voice,
            ]
            if getattr(sys, "frozen", False):
                command = [sys.executable, "--ppt-export-runner", *runner_args]
            else:
                command = [sys.executable, "-u", "-m", "vcut_studio.ppt_export_runner", *runner_args]
            if self.burn_subtitles:
                command.append("--burn-subtitles")
            if self.export_sidecar_srt:
                command.append("--export-sidecar-srt")

            env = dict(os.environ)
            src_root = str(Path(__file__).resolve().parents[2])
            repo_root = str(Path(__file__).resolve().parents[3])
            existing_pythonpath = env.get("PYTHONPATH", "").strip()
            env["PYTHONPATH"] = src_root if not existing_pythonpath else src_root + os.pathsep + existing_pythonpath
            env["PYTHONIOENCODING"] = "utf-8"

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                cwd=repo_root,
                **subprocess_windowless_kwargs(),
            )
            self.attach_process(process)
            stdout_lines: list[str] = []
            try:
                assert process.stdout is not None
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    stdout_lines.append(line)
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") != "progress":
                        continue
                    value = int(payload.get("value", 0))
                    message = str(payload.get("message", "")).strip()
                    self._emit_progress(
                        46 + int(max(0, min(value, 100)) * 54 / 100),
                        message,
                    )
                return_code = process.wait()
            finally:
                self.detach_process(process)

            if return_code != 0:
                stdout_tail = "\n".join(stdout_lines[-12:])
                raise RuntimeError(stdout_tail or "PPT 导出子进程执行失败。")
            if not result_path.exists():
                raise RuntimeError("PPT 导出子进程没有生成结果文件。")

            result_data = json.loads(result_path.read_text(encoding="utf-8"))
            output_path = Path(str(result_data.get("output_path", "") or ""))
            if not str(output_path):
                raise RuntimeError("PPT 导出结果缺少输出路径。")
            updated_project = Project.from_dict(result_data.get("project_snapshot", {}))
            plan = ExportPlan(cut_ranges=[], keep_ranges=[], output_path=output_path)
            return (plan, updated_project)


class PptVoiceInputWorker(ControllableWorker):
    progress = Signal(str)
    progress_value = Signal(int, str)
    finished = Signal(object)
    error = Signal(str)

    def __init__(self, audio_path: str | Path, asr_settings: ASRSettings) -> None:
        super().__init__("中文语音识别", pause_supported=False, cancel_supported=False)
        self.audio_path = str(audio_path)
        self.asr_settings = asr_settings

    @Slot()
    def run(self) -> None:
        try:
            self._emit_progress(5, "正在准备语音识别...")
            transcriber = FasterWhisperTranscriber(self.asr_settings)
            segments, _summary = transcriber.transcribe_media(
                self.audio_path,
                progress_callback=self._emit_progress,
            )
            recognized_lines = [
                str(segment.zh_text or "").strip()
                for segment in segments
                if str(getattr(segment, "zh_text", "") or "").strip()
            ]
            recognized_text = "\n".join(recognized_lines).strip()
            if not recognized_text:
                raise RuntimeError("没有识别到可用的中文内容。")
            self._emit_progress(100, "语音识别完成。")
        except Exception as exc:
            self.error.emit(_format_worker_error("中文语音识别", exc))
            return

        self.finished.emit(
            PptVoiceInputResult(
                text=recognized_text,
                audio_path=self.audio_path,
                segment_count=len(recognized_lines),
            )
        )

    def _emit_progress(self, value: int, message: str) -> None:
        self.emit_progress_update(self.progress, self.progress_value, value, message)


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
                sample_text = "This is a sample voice preview."
            self.progress.emit("正在生成音色试听...")
            provider.synthesize_segment(
                sample_text,
                self.voice_id,
                output_path,
                controller=self,
            )
        except TTSProviderError as exc:
            self.error.emit(_format_worker_error("音色试听", exc))
            return
        except Exception as exc:
            self.error.emit(_format_worker_error("音色试听", exc))
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
            self.progress.emit("正在测试翻译服务连接...")
            provider = create_translation_provider(self.settings.translation)
            sample = provider.test_connection()
        except TranslationProviderError as exc:
            self.error.emit(_format_worker_error("翻译配置测试", exc))
            return
        except Exception as exc:
            self.error.emit(_format_worker_error("翻译配置测试", exc))
            return
        self.finished.emit(sample)
