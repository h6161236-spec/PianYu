from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path


def runtime_base_dirs() -> list[Path]:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        try:
            candidates.append(Path(sys.executable).resolve().parent)
        except Exception:
            candidates.append(Path(sys.executable).parent)

    runtime_unpack_dir = getattr(sys, "_MEIPASS", None)
    if runtime_unpack_dir:
        candidates.append(Path(runtime_unpack_dir))

    try:
        candidates.append(Path(__file__).resolve().parents[2])
    except Exception:
        candidates.append(Path(__file__).resolve().parent)

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


def find_app_icon() -> Path | None:
    preferred_names = (
        "app_icon.svg",
        "app_icon.ico",
        "app_icon.png",
        "图标3.ico",
        "图标3.png",
        "图标2.ico",
        "图标2.png",
        "图标.ico",
        "图标.png",
    )
    for base_dir in runtime_base_dirs():
        for name in preferred_names:
            candidate = base_dir / name
            if candidate.exists():
                return candidate
    return None


def _is_kokoro_model_dir(path: Path) -> bool:
    required_paths = (
        path / "model.onnx",
        path / "voices.bin",
        path / "tokens.txt",
        path / "espeak-ng-data",
    )
    return all(candidate.exists() for candidate in required_paths)


def _is_melo_model_dir(path: Path) -> bool:
    required_paths = (
        path / "model.onnx",
        path / "lexicon.txt",
        path / "tokens.txt",
        path / "date.fst",
        path / "number.fst",
        path / "phone.fst",
    )
    return all(candidate.exists() for candidate in required_paths)


def _kokoro_voice_language_hint(preferred_voice_id: str | None = None) -> str:
    normalized_voice_id = str(preferred_voice_id or "").strip().lower()
    if normalized_voice_id.startswith(("zf_", "zm_")):
        return "zh"
    if normalized_voice_id.startswith(("af_", "am_", "bf_", "bm_")):
        return "en"
    return ""


_KOKORO_V1_1_ENGLISH_VOICE_IDS = {"af_maple", "af_sol", "bf_vale"}


def _kokoro_preferred_model_names(
    language: str = "",
    preferred_voice_id: str | None = None,
) -> tuple[str, ...]:
    normalized_language = str(language or "").strip().lower()
    normalized_voice_id = str(preferred_voice_id or "").strip().lower()
    if normalized_voice_id in _KOKORO_V1_1_ENGLISH_VOICE_IDS:
        return (
            "csukuangfj-kokoro-multi-lang-v1_1",
            "kokoro-multi-lang-v1_1",
            "kokoro-multi-lang-v1_0",
            "kokoro-en-v0_19",
        )
    if normalized_language == "zh":
        return (
            "csukuangfj-kokoro-multi-lang-v1_1",
            "kokoro-multi-lang-v1_1",
            "kokoro-multi-lang-v1_0",
        )
    if normalized_language == "en":
        return (
            "kokoro-multi-lang-v1_0",
            "kokoro-en-v0_19",
            "csukuangfj-kokoro-multi-lang-v1_1",
            "kokoro-multi-lang-v1_1",
        )
    return (
        "csukuangfj-kokoro-multi-lang-v1_1",
        "kokoro-multi-lang-v1_1",
        "kokoro-multi-lang-v1_0",
        "kokoro-en-v0_19",
    )


def _kokoro_search_patterns(
    language: str = "",
    preferred_voice_id: str | None = None,
) -> tuple[str, ...]:
    preferred_models = _kokoro_preferred_model_names(
        language=language,
        preferred_voice_id=preferred_voice_id,
    )
    expanded_patterns: list[str] = []
    for model_name in preferred_models:
        expanded_patterns.extend(
            [
                f"models/tts/{model_name}",
                f"tts/{model_name}",
                model_name,
            ]
        )
    return tuple(expanded_patterns) + (
        "models/tts/kokoro-*",
        "tts/kokoro-*",
        "kokoro-*",
    )


def find_kokoro_model_dir(
    language: str | None = None,
    preferred_voice_id: str | None = None,
) -> Path | None:
    normalized_language = _kokoro_voice_language_hint(preferred_voice_id) or str(language or "").strip().lower()
    search_patterns = _kokoro_search_patterns(
        normalized_language,
        preferred_voice_id=preferred_voice_id,
    )
    for base_dir in runtime_base_dirs():
        for pattern in search_patterns:
            for candidate in sorted(base_dir.glob(pattern)):
                if not candidate.is_dir() or not _is_kokoro_model_dir(candidate):
                    continue
                candidate_name = candidate.name.lower()
                if normalized_language == "zh" and "multi-lang" not in candidate_name:
                    continue
                if normalized_language == "en" and "kokoro-en" not in candidate_name and "multi-lang" not in candidate_name:
                    continue
                return candidate
    return None


def find_melo_model_dir() -> Path | None:
    search_patterns = (
        "models/tts/vits-melo-tts-zh_en",
        "tts/vits-melo-tts-zh_en",
        "vits-melo-tts-zh_en",
        "models/tts/*melo*zh_en*",
        "tts/*melo*zh_en*",
        "*melo*zh_en*",
    )
    for base_dir in runtime_base_dirs():
        for pattern in search_patterns:
            for candidate in sorted(base_dir.glob(pattern)):
                if candidate.is_dir() and _is_melo_model_dir(candidate):
                    return candidate
    return None


def find_sherpa_onnx_tts_executable() -> Path | None:
    search_patterns = (
        "models/tts/sherpa-onnx-*/bin/sherpa-onnx-offline-tts.exe",
        "tts/sherpa-onnx-*/bin/sherpa-onnx-offline-tts.exe",
        "sherpa-onnx-*/bin/sherpa-onnx-offline-tts.exe",
        "bin/sherpa-onnx-offline-tts.exe",
        "sherpa-onnx-offline-tts.exe",
    )
    for base_dir in runtime_base_dirs():
        for pattern in search_patterns:
            for candidate in sorted(base_dir.glob(pattern)):
                if candidate.is_file():
                    return candidate
    return None


def kokoro_local_assets_available() -> bool:
    return find_kokoro_model_dir() is not None and find_sherpa_onnx_tts_executable() is not None


def melo_local_assets_available() -> bool:
    return find_melo_model_dir() is not None and find_sherpa_onnx_tts_executable() is not None


def path_contains_non_ascii(value: str | Path) -> bool:
    return any(ord(character) > 127 for character in str(value or ""))


def _runtime_cache_root() -> Path:
    base_dir = os.environ.get("LOCALAPPDATA")
    if base_dir:
        root = Path(base_dir) / "VCutStudio" / "runtime_cache"
    else:
        root = Path(tempfile.gettempdir()) / "VCutStudio_runtime_cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _runtime_cache_key(source_path: Path, cache_group: str) -> str:
    stat = source_path.stat()
    fingerprint = "|".join(
        [
            cache_group,
            str(source_path.resolve()),
            "dir" if source_path.is_dir() else "file",
            str(stat.st_mtime_ns),
            str(stat.st_size if source_path.is_file() else 0),
        ]
    )
    return hashlib.sha1(fingerprint.encode("utf-8", errors="ignore")).hexdigest()[:16]


def prepare_ascii_safe_runtime_path(path: str | Path, cache_group: str) -> Path:
    source_path = Path(path)
    if not source_path.exists():
        return source_path

    try:
        resolved_source_path = source_path.resolve()
    except Exception:
        resolved_source_path = source_path

    if not path_contains_non_ascii(resolved_source_path):
        return resolved_source_path

    # Some Windows-only TTS tooling fails on non-ASCII paths, so we mirror the
    # payload into a stable ASCII-only cache and run from there instead.
    cache_key = _runtime_cache_key(resolved_source_path, cache_group)
    cache_root = _runtime_cache_root() / cache_group / cache_key
    payload_path = cache_root / ("payload" if resolved_source_path.is_dir() else f"payload{resolved_source_path.suffix}")

    if payload_path.exists():
        return payload_path

    cache_root.mkdir(parents=True, exist_ok=True)
    if resolved_source_path.is_dir():
        shutil.copytree(resolved_source_path, payload_path, dirs_exist_ok=True)
    else:
        shutil.copy2(resolved_source_path, payload_path)
    return payload_path
