from __future__ import annotations

import ctypes
import importlib.metadata
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Callable
from uuid import uuid4

from .models import Segment, WordTiming
from .process_utils import subprocess_windowless_kwargs
from .runtime_assets import prepare_ascii_safe_runtime_path
from .settings import ASRSettings

try:  # pragma: no cover - optional runtime dependency
    from opencc import OpenCC
except ImportError:  # pragma: no cover - optional runtime dependency
    OpenCC = None

_SIMPLIFIED_CHINESE_CONVERTER = OpenCC("t2s") if OpenCC is not None else None


class TranscriptionError(RuntimeError):
    """Raised when transcription cannot be completed."""


@dataclass(slots=True)
class TranscriptionSummary:
    detected_language: str
    language_probability: float
    segment_count: int


@dataclass(slots=True)
class AsrRuntimeDiagnostics:
    requested_device: str
    faster_whisper_version: str
    ctranslate2_version: str
    cuda_device_count: int
    missing_cuda_libraries: list[str]
    nvidia_gpu_names: list[str]
    nvidia_smi_available: bool

    @property
    def gpu_ready(self) -> bool:
        return self.cuda_device_count > 0 and not self.missing_cuda_libraries


def package_version(package_name: str) -> str:
    try:
        return importlib.metadata.version(package_name)
    except Exception:
        return "未安装"


def missing_cuda_runtime_libraries() -> list[str]:
    if not hasattr(ctypes, "WinDLL"):
        return []
    missing: list[str] = []
    for dll_name in ("cublas64_12.dll", "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(dll_name)
        except Exception:
            missing.append(dll_name)
    return missing


def detect_cuda_device_count() -> int:
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def list_nvidia_gpu_names() -> tuple[bool, list[str]]:
    if shutil.which("nvidia-smi") is None:
        return False, []
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **subprocess_windowless_kwargs(),
        )
    except Exception:
        return True, []
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return True, lines


def collect_asr_runtime_diagnostics(requested_device: str) -> AsrRuntimeDiagnostics:
    nvidia_smi_available, nvidia_gpu_names = list_nvidia_gpu_names()
    cuda_device_count = detect_cuda_device_count()
    requested = str(requested_device or "auto").strip().lower() or "auto"
    should_check_cuda_runtime = requested in {"auto", "cuda"} or cuda_device_count > 0
    return AsrRuntimeDiagnostics(
        requested_device=requested,
        faster_whisper_version=package_version("faster-whisper"),
        ctranslate2_version=package_version("ctranslate2"),
        cuda_device_count=cuda_device_count,
        missing_cuda_libraries=(
            missing_cuda_runtime_libraries() if should_check_cuda_runtime else []
        ),
        nvidia_gpu_names=nvidia_gpu_names,
        nvidia_smi_available=nvidia_smi_available,
    )


def _ms(value: float | int | None) -> int:
    if value is None:
        return 0
    return max(0, int(float(value) * 1000))


def normalize_simplified_chinese(text: str) -> str:
    normalized = str(text or "")
    converter = _SIMPLIFIED_CHINESE_CONVERTER
    if converter is None or not normalized.strip():
        return normalized
    try:
        return str(converter.convert(normalized))
    except Exception:
        return normalized


def _runtime_model_dirs() -> list[Path]:
    candidates: list[Path] = [Path.cwd()]
    if getattr(sys, "frozen", False):
        try:
            candidates.append(Path(sys.executable).resolve().parent)
        except Exception:
            candidates.append(Path(sys.executable).parent)
    runtime_unpack_dir = getattr(sys, "_MEIPASS", None)
    if runtime_unpack_dir:
        candidates.append(Path(runtime_unpack_dir))

    unique_dirs: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        if resolved in seen or not resolved.exists():
            continue
        unique_dirs.append(resolved)
        seen.add(resolved)
    return unique_dirs


def _word_timings_from_whisper(raw_segment: object) -> list[WordTiming]:
    word_items = getattr(raw_segment, "words", None) or []
    timings: list[WordTiming] = []
    for word in word_items:
        timings.append(
            WordTiming(
                text=normalize_simplified_chinese(str(getattr(word, "word", "")).strip()),
                start_ms=_ms(getattr(word, "start", None)),
                end_ms=_ms(getattr(word, "end", None)),
                confidence=float(getattr(word, "probability", 1.0) or 0.0),
            )
        )
    return [timing for timing in timings if timing.text]


def segment_from_whisper(raw_segment: object, default_voice: str | None = None) -> Segment:
    words = _word_timings_from_whisper(raw_segment)
    start_ms = words[0].start_ms if words else _ms(getattr(raw_segment, "start", None))
    end_ms = words[-1].end_ms if words else _ms(getattr(raw_segment, "end", None))
    text = normalize_simplified_chinese(str(getattr(raw_segment, "text", "")).strip())
    return Segment(
        segment_id=uuid4().hex,
        start_ms=start_ms,
        end_ms=end_ms,
        zh_text=text,
        words=words,
        translation_status="pending",
        tts_status="pending",
        voice_id=default_voice,
    )


class FasterWhisperTranscriber:
    def __init__(self, settings: ASRSettings) -> None:
        self.settings = settings

    def _resolved_local_model_dir(self) -> str:
        local_model_dir = str(self.settings.local_model_dir or "").strip()
        if not local_model_dir:
            return ""
        model_path = Path(local_model_dir)
        if model_path.is_absolute():
            return str(prepare_ascii_safe_runtime_path(model_path, "asr_model"))
        if model_path.exists():
            return str(prepare_ascii_safe_runtime_path(model_path.resolve(), "asr_model"))
        for runtime_dir in _runtime_model_dirs():
            candidate = runtime_dir / model_path
            if candidate.exists():
                return str(prepare_ascii_safe_runtime_path(candidate.resolve(), "asr_model"))
        return local_model_dir

    def _model_reference(self) -> str:
        local_model_dir = self._resolved_local_model_dir()
        if local_model_dir:
            return local_model_dir
        model_name = str(self.settings.model_name or "").strip()
        return model_name or "small"

    def _model_cache_dir(self) -> str | None:
        cache_dir = str(self.settings.model_cache_dir or "").strip()
        return cache_dir or None

    def _missing_cuda_runtime_libraries(self) -> list[str]:
        return missing_cuda_runtime_libraries()

    def _resolved_device(self) -> str:
        requested_device = str(self.settings.device or "cpu").strip().lower()
        if requested_device == "cuda":
            return "cuda"
        if requested_device != "auto":
            return requested_device or "cpu"
        if detect_cuda_device_count() > 0 and not self._missing_cuda_runtime_libraries():
            return "cuda"
        return "cpu"

    def _compute_type(self, resolved_device: str) -> str:
        if resolved_device == "cuda":
            return "float16"
        return "int8"

    def _looks_like_local_model_reference(self) -> bool:
        model_reference = self._model_reference()
        if not model_reference:
            return False
        if str(self.settings.local_model_dir or "").strip():
            return True
        if Path(model_reference).exists():
            return True
        return any(marker in model_reference for marker in ("/", "\\", ":"))

    def _loading_message(self, resolved_device: str, elapsed_seconds: int) -> str:
        model_name = Path(self._model_reference()).name or self._model_reference()
        if self._looks_like_local_model_reference():
            base = f"正在加载本地 ASR 模型：{model_name}"
        else:
            base = f"正在加载 ASR 模型：{model_name}"
        if elapsed_seconds <= 0:
            if resolved_device == "cpu" and model_name in {"medium", "large-v2", "large-v3", "large"}:
                return base + "（当前模型较大，CPU 首次加载可能很慢，建议改用 small）..."
            return base + "（首次使用可能需要下载，请稍候）..."
        if not self._looks_like_local_model_reference():
            return base + f"（首次可能在下载，已等待 {elapsed_seconds} 秒）..."
        return base + f"（已等待 {elapsed_seconds} 秒）..."

    def _should_retry_on_cpu(self, resolved_device: str, exc: Exception) -> bool:
        if resolved_device != "cuda":
            return False
        error_text = str(exc).lower()
        return any(
            marker in error_text
            for marker in (
                "cublas64_12.dll",
                "cudnn64_9.dll",
                "cuda",
                "cublas",
                "cudnn",
            )
        )

    def _friendly_model_load_error(self, exc: Exception) -> Exception:
        message = str(exc)
        normalized = message.lower()
        local_model_dir = str(self.settings.local_model_dir or "").strip()
        if local_model_dir and not Path(local_model_dir).exists():
            return TranscriptionError(f"本地 ASR 模型目录不存在：{local_model_dir}")
        if self.settings.local_files_only and any(
            marker in normalized
            for marker in (
                "disk cache",
                "local_files_only",
                "cannot find the requested files",
                "cannot find an appropriate cached snapshot",
            )
        ):
            return TranscriptionError(
                "当前开启了“仅使用本地缓存/本地模型”，但没有在本地找到可用 ASR 模型。"
                "请先下载模型，或填写正确的“本地模型目录”。"
            )
        return TranscriptionError(message)

    def _load_model_with_feedback(
        self,
        whisper_model_factory: object,
        resolved_device: str,
        progress_callback: Callable[[int, str], None] | None,
        checkpoint: Callable[[], None] | None,
    ) -> object:
        model_holder: dict[str, object] = {}
        error_holder: dict[str, Exception] = {}

        def _load_model() -> None:
            try:
                model_holder["model"] = whisper_model_factory(
                    self._model_reference(),
                    device=resolved_device,
                    compute_type=self._compute_type(resolved_device),
                    download_root=self._model_cache_dir(),
                    local_files_only=bool(self.settings.local_files_only),
                )
            except Exception as exc:
                error_holder["error"] = exc

        load_thread = threading.Thread(target=_load_model, daemon=True)
        load_thread.start()
        loading_tick = 0
        while load_thread.is_alive():
            if checkpoint is not None:
                checkpoint()
            load_thread.join(timeout=1.0)
            if load_thread.is_alive():
                loading_tick += 1
                if progress_callback is not None:
                    heartbeat_progress = min(11, 5 + min(loading_tick, 6))
                    progress_callback(
                        heartbeat_progress,
                        self._loading_message(resolved_device, loading_tick),
                    )

        if checkpoint is not None:
            checkpoint()
        if "error" in error_holder:
            raise error_holder["error"]
        model = model_holder.get("model")
        if model is None:
            raise TranscriptionError("ASR 模型加载失败，未返回可用模型实例。")
        return model

    def transcribe_media(
        self,
        media_path: str | Path,
        default_voice: str | None = None,
        progress_callback: Callable[[int, str], None] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> tuple[list[Segment], TranscriptionSummary]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionError(
                "faster-whisper is not installed. Install the optional ASR dependency first."
            ) from exc

        resolved_device = self._resolved_device()
        if (
            str(self.settings.device or "").strip().lower() in {"auto", "cuda"}
            and resolved_device == "cpu"
        ):
            missing_libraries = self._missing_cuda_runtime_libraries()
            if missing_libraries and progress_callback is not None:
                progress_callback(
                    4,
                    "未检测到 CUDA 运行库（"
                    + ", ".join(missing_libraries)
                    + "），已自动切换到 CPU 识别。",
                )
        if progress_callback is not None:
            progress_callback(5, self._loading_message(resolved_device, 0))
        if checkpoint is not None:
            checkpoint()

        try:
            model = self._load_model_with_feedback(
                WhisperModel,
                resolved_device=resolved_device,
                progress_callback=progress_callback,
                checkpoint=checkpoint,
            )
        except Exception as exc:
            if self._should_retry_on_cpu(resolved_device, exc):
                if progress_callback is not None:
                    progress_callback(
                        5,
                        "CUDA 运行库不可用，已自动回退到 CPU 继续加载 ASR 模型...",
                    )
                resolved_device = "cpu"
                model = self._load_model_with_feedback(
                    WhisperModel,
                    resolved_device=resolved_device,
                    progress_callback=progress_callback,
                    checkpoint=checkpoint,
                )
            else:
                raise self._friendly_model_load_error(exc) from exc
        vad_parameters = None
        if self.settings.vad_enabled:
            vad_parameters = {"max_speech_duration_s": self.settings.segment_max_seconds}
        
        try:
            if checkpoint is not None:
                checkpoint()
            raw_segments, info = model.transcribe(
                str(media_path),
                language=self.settings.language,
                beam_size=5,
                condition_on_previous_text=False,
                word_timestamps=True,
                vad_filter=self.settings.vad_enabled,
                vad_parameters=vad_parameters,
            )
            total_duration_ms = max(
                0,
                int(float(getattr(info, "duration", 0.0) or 0.0) * 1000),
            )
            segments: list[Segment] = []
            if progress_callback is not None:
                progress_callback(12, "正在执行中文 ASR 转写...")
            for raw_segment in raw_segments:
                if checkpoint is not None:
                    checkpoint()
                if not str(getattr(raw_segment, "text", "")).strip():
                    continue
                segment = segment_from_whisper(raw_segment, default_voice=default_voice)
                segments.append(segment)
                if progress_callback is not None and total_duration_ms > 0:
                    progress = 12 + int(min(1.0, segment.end_ms / total_duration_ms) * 76)
                    progress_callback(progress, f"正在执行中文 ASR 转写... 已处理 {len(segments)} 段")
            if progress_callback is not None:
                progress_callback(90, "正在整理转写结果...")
            if checkpoint is not None:
                checkpoint()
        except Exception as exc:  # pragma: no cover - library/runtime dependent
            raise TranscriptionError(str(exc)) from exc

        summary = TranscriptionSummary(
            detected_language=str(getattr(info, "language", self.settings.language)),
            language_probability=float(getattr(info, "language_probability", 0.0) or 0.0),
            segment_count=len(segments),
        )
        return segments, summary
