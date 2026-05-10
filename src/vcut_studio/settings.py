from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .runtime_assets import find_kokoro_model_dir, kokoro_local_assets_available, melo_local_assets_available


DEFAULT_TRANSLATION_SYSTEM_PROMPT = """You are a professional audiovisual translator.
Translate each Chinese segment into natural spoken English.
Keep the meaning accurate, concise, and subtitle-friendly.
Return structured JSON that preserves each segment_id."""


def _default_workspace_dir() -> str:
    return str(Path.home() / "Documents" / "片语")


def _default_temp_dir() -> str:
    return str(Path(_default_workspace_dir()) / "temp")


def _default_output_dir() -> str:
    return str(Path.home() / "Videos" / "片语导出")


def _default_asr_cache_dir() -> str:
    return str(Path.home() / ".cache" / "huggingface" / "hub")


def _default_kokoro_chinese_voice() -> str:
    model_dir = find_kokoro_model_dir(language="zh")
    model_name = model_dir.name.lower() if model_dir is not None else ""
    if "v1_1" in model_name:
        return "zm_001"
    return "zm_yunxi"


def _default_tts_provider_type(language: str = "en") -> str:
    normalized_language = str(language or "en").strip().lower()
    if normalized_language == "zh":
        if find_kokoro_model_dir(language="zh") is not None:
            return "kokoro_local"
        if melo_local_assets_available():
            return "melo_local"
        return "builtin_voice_catalog"
    if kokoro_local_assets_available():
        return "kokoro_local"
    return "builtin_voice_catalog"


def _default_tts_voice(provider_type: str | None = None, language: str = "en") -> str:
    normalized_language = str(language or "en").strip().lower()
    normalized = (provider_type or _default_tts_provider_type(normalized_language)).strip()
    if normalized == "melo_local":
        return "melo_zh_female" if normalized_language == "zh" else ""
    if normalized == "kokoro_local":
        return _default_kokoro_chinese_voice() if normalized_language == "zh" else "af_bella"
    if normalized_language == "zh":
        return ""
    return "emma_clear"


_DEFAULT_SUBTITLE_ENGLISH_COLOR = "#FFFFFF"
_DEFAULT_SUBTITLE_CHINESE_COLOR = "#FFD966"
_HEX_COLOR_PATTERN = re.compile(r"^#?(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")


def _normalize_hex_color(value: Any, fallback: str) -> str:
    normalized = str(value or "").strip()
    if _HEX_COLOR_PATTERN.fullmatch(normalized):
        return f"#{normalized.lstrip('#').upper()}"
    return fallback


def _normalize_subtitle_position_percent(value: Any, fallback: float) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(100.0, normalized))


def _portable_settings_path() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    try:
        runtime_dir = Path(sys.executable).resolve().parent
    except Exception:
        runtime_dir = Path(sys.executable).parent
    candidate = runtime_dir / "settings.json"
    if candidate.exists():
        return candidate
    return None


def _default_settings_path() -> Path:
    portable_path = _portable_settings_path()
    if portable_path is not None:
        return portable_path
    appdata = os.environ.get("APPDATA")
    base_dir = Path(appdata) if appdata else Path.home() / ".config"
    return base_dir / "VCutStudio" / "settings.json"


@dataclass(slots=True)
class WorkspaceSettings:
    workspace_dir: str = field(default_factory=_default_workspace_dir)
    temp_dir: str = field(default_factory=_default_temp_dir)
    auto_save_minutes: int = 3
    recent_projects_limit: int = 10
    log_level: str = "INFO"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkspaceSettings":
        return cls(
            workspace_dir=str(data.get("workspace_dir", _default_workspace_dir())),
            temp_dir=str(data.get("temp_dir", _default_temp_dir())),
            auto_save_minutes=int(data.get("auto_save_minutes", 3)),
            recent_projects_limit=int(data.get("recent_projects_limit", 10)),
            log_level=str(data.get("log_level", "INFO")),
        )


@dataclass(slots=True)
class MediaSettings:
    ffmpeg_path: str = "ffmpeg"
    default_output_dir: str = field(default_factory=_default_output_dir)
    overwrite_existing: bool = False
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    quality_preset: str = "medium"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaSettings":
        return cls(
            ffmpeg_path=str(data.get("ffmpeg_path", "ffmpeg")),
            default_output_dir=str(data.get("default_output_dir", _default_output_dir())),
            overwrite_existing=bool(data.get("overwrite_existing", False)),
            video_codec=str(data.get("video_codec", "libx264")),
            audio_codec=str(data.get("audio_codec", "aac")),
            quality_preset=str(data.get("quality_preset", "medium")),
        )


@dataclass(slots=True)
class ASRSettings:
    model_name: str = "small"
    device: str = "cpu"
    language: str = "zh"
    vad_enabled: bool = True
    segment_max_seconds: int = 25
    min_confidence: float = 0.55
    model_cache_dir: str = field(default_factory=_default_asr_cache_dir)
    local_model_dir: str = ""
    local_files_only: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ASRSettings":
        return cls(
            model_name=str(data.get("model_name", "small")),
            device=str(data.get("device", "cpu")),
            language=str(data.get("language", "zh")),
            vad_enabled=bool(data.get("vad_enabled", True)),
            segment_max_seconds=int(data.get("segment_max_seconds", 25)),
            min_confidence=float(data.get("min_confidence", 0.55)),
            model_cache_dir=str(data.get("model_cache_dir", _default_asr_cache_dir())),
            local_model_dir=str(data.get("local_model_dir", "")),
            local_files_only=bool(data.get("local_files_only", False)),
        )


@dataclass(slots=True)
class CuttingSettings:
    filler_words: list[str] = field(
        default_factory=lambda: [
            "\u55ef",
            "\u554a",
            "\u5443",
            "\u8fd9\u4e2a",
            "\u90a3\u4e2a",
            "\u5c31\u662f",
            "\u7136\u540e",
        ]
    )
    pause_threshold_ms: int = 550
    cut_padding_ms: int = 90
    merge_gap_ms: int = 120
    mode: str = "standard"
    preserve_intro_pause: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CuttingSettings":
        words = data.get(
            "filler_words",
            [
                "\u55ef",
                "\u554a",
                "\u5443",
                "\u8fd9\u4e2a",
                "\u90a3\u4e2a",
                "\u5c31\u662f",
                "\u7136\u540e",
            ],
        )
        return cls(
            filler_words=[str(word) for word in words],
            pause_threshold_ms=int(data.get("pause_threshold_ms", 550)),
            cut_padding_ms=int(data.get("cut_padding_ms", 90)),
            merge_gap_ms=int(data.get("merge_gap_ms", 120)),
            mode=str(data.get("mode", "standard")),
            preserve_intro_pause=bool(data.get("preserve_intro_pause", True)),
        )


@dataclass(slots=True)
class TranslationSettings:
    provider_type: str = "openai_compatible"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    system_prompt: str = DEFAULT_TRANSLATION_SYSTEM_PROMPT
    timeout_sec: int = 180
    max_retries: int = 3
    concurrency: int = 2
    batch_size: int = 2
    proxy_enabled: bool = False
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationSettings":
        return cls(
            provider_type=str(data.get("provider_type", "openai_compatible")),
            base_url=str(data.get("base_url", "")),
            api_key=str(data.get("api_key", "")),
            model=str(data.get("model", "")),
            system_prompt=str(data.get("system_prompt", DEFAULT_TRANSLATION_SYSTEM_PROMPT)),
            timeout_sec=int(data.get("timeout_sec", 180)),
            max_retries=int(data.get("max_retries", 3)),
            concurrency=int(data.get("concurrency", 2)),
            batch_size=int(data.get("batch_size", 2)),
            proxy_enabled=bool(data.get("proxy_enabled", False)),
            http_proxy=str(data.get("http_proxy", "")),
            https_proxy=str(data.get("https_proxy", "")),
            no_proxy=str(data.get("no_proxy", "")),
        )


@dataclass(slots=True)
class TTSSettings:
    provider_type: str = field(default_factory=_default_tts_provider_type)
    default_voice: str = field(default_factory=_default_tts_voice)
    rate: float = 1.0
    volume: float = 1.0
    sample_rate: int = 22050
    retry_count: int = 2
    preview_text: str = "This is a sample English dubbing preview."
    english_provider_type: str = field(default_factory=lambda: _default_tts_provider_type("en"))
    english_default_voice: str = field(default_factory=lambda: _default_tts_voice(language="en"))
    english_preview_text: str = "This is a sample English dubbing preview."
    chinese_provider_type: str = field(default_factory=lambda: _default_tts_provider_type("zh"))
    chinese_default_voice: str = field(default_factory=lambda: _default_tts_voice(language="zh"))
    chinese_preview_text: str = "这是一段中文配音试听文本。"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TTSSettings":
        provider_type = str(data.get("provider_type", _default_tts_provider_type()))
        settings = cls(
            provider_type=provider_type,
            default_voice=str(data.get("default_voice", _default_tts_voice(provider_type, language="en"))),
            rate=float(data.get("rate", 1.0)),
            volume=float(data.get("volume", 1.0)),
            sample_rate=int(data.get("sample_rate", 22050)),
            retry_count=int(data.get("retry_count", 2)),
            preview_text=str(
                data.get(
                    "preview_text",
                    "This is a sample English dubbing preview.",
                )
            ),
            english_provider_type=str(data.get("english_provider_type", provider_type)),
            english_default_voice=str(
                data.get(
                    "english_default_voice",
                    data.get("default_voice", _default_tts_voice(provider_type, language="en")),
                )
            ),
            english_preview_text=str(
                data.get(
                    "english_preview_text",
                    data.get(
                        "preview_text",
                        "This is a sample English dubbing preview.",
                    ),
                )
            ),
            chinese_provider_type=str(
                data.get(
                    "chinese_provider_type",
                    provider_type if provider_type in {"kokoro_local", "melo_local"} else _default_tts_provider_type("zh"),
                )
            ),
            chinese_default_voice=str(
                data.get(
                    "chinese_default_voice",
                    _default_tts_voice(
                        str(
                            data.get(
                                "chinese_provider_type",
                                provider_type if provider_type in {"kokoro_local", "melo_local"} else _default_tts_provider_type("zh"),
                            )
                        ),
                        language="zh",
                    ),
                )
            ),
            chinese_preview_text=str(
                data.get(
                    "chinese_preview_text",
                    "这是一段中文配音试听文本。",
                )
            ),
        )
        settings.sync_legacy_defaults()
        return settings

    def provider_type_for_language(self, language: str) -> str:
        normalized = str(language or "en").strip().lower()
        if normalized == "zh":
            return str(self.chinese_provider_type or self.provider_type or _default_tts_provider_type("zh")).strip()
        return str(self.english_provider_type or self.provider_type or _default_tts_provider_type("en")).strip()

    def default_voice_for_language(self, language: str) -> str:
        normalized = str(language or "en").strip().lower()
        if normalized == "zh":
            return str(
                self.chinese_default_voice
                or _default_tts_voice(self.provider_type_for_language("zh"), language="zh")
            ).strip()
        return str(
            self.english_default_voice
            or self.default_voice
            or _default_tts_voice(self.provider_type_for_language("en"), language="en")
        ).strip()

    def preview_text_for_language(self, language: str) -> str:
        normalized = str(language or "en").strip().lower()
        if normalized == "zh":
            return str(self.chinese_preview_text or "这是一段中文配音试听文本。").strip()
        return str(
            self.english_preview_text
            or self.preview_text
            or "This is a sample English dubbing preview."
        ).strip()

    def set_provider_type_for_language(self, language: str, provider_type: str) -> None:
        normalized = str(language or "en").strip().lower()
        if normalized == "zh":
            self.chinese_provider_type = str(provider_type or "").strip()
        else:
            self.english_provider_type = str(provider_type or "").strip()
        self.sync_legacy_defaults()

    def set_default_voice_for_language(self, language: str, voice_id: str) -> None:
        normalized = str(language or "en").strip().lower()
        if normalized == "zh":
            self.chinese_default_voice = str(voice_id or "").strip()
        else:
            self.english_default_voice = str(voice_id or "").strip()
        self.sync_legacy_defaults()

    def set_preview_text_for_language(self, language: str, preview_text: str) -> None:
        normalized = str(language or "en").strip().lower()
        if normalized == "zh":
            self.chinese_preview_text = str(preview_text or "").strip()
        else:
            self.english_preview_text = str(preview_text or "").strip()
        self.sync_legacy_defaults()

    def sync_legacy_defaults(self) -> None:
        self.provider_type = self.provider_type_for_language("en")
        self.default_voice = self.default_voice_for_language("en")
        self.preview_text = self.preview_text_for_language("en")


@dataclass(slots=True)
class ExportSettings:
    default_export_type: str = "clean_zh"
    subtitle_mode: str = "en"
    subtitle_font_size: int = 28
    subtitle_english_color: str = _DEFAULT_SUBTITLE_ENGLISH_COLOR
    subtitle_chinese_color: str = _DEFAULT_SUBTITLE_CHINESE_COLOR
    subtitle_safe_area_enabled: bool = True
    subtitle_english_x_percent: float = 50.0
    subtitle_english_y_percent: float = 90.0
    subtitle_chinese_x_percent: float = 50.0
    subtitle_chinese_y_percent: float = 82.0
    burn_subtitles: bool = True
    export_sidecar_srt: bool = True
    audio_lufs_target: int = -16
    output_container: str = "mp4"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExportSettings":
        return cls(
            default_export_type=str(data.get("default_export_type", "clean_zh")),
            subtitle_mode=str(data.get("subtitle_mode", "en")),
            subtitle_font_size=max(1, int(data.get("subtitle_font_size", 28))),
            subtitle_english_color=_normalize_hex_color(
                data.get("subtitle_english_color", _DEFAULT_SUBTITLE_ENGLISH_COLOR),
                _DEFAULT_SUBTITLE_ENGLISH_COLOR,
            ),
            subtitle_chinese_color=_normalize_hex_color(
                data.get("subtitle_chinese_color", _DEFAULT_SUBTITLE_CHINESE_COLOR),
                _DEFAULT_SUBTITLE_CHINESE_COLOR,
            ),
            subtitle_safe_area_enabled=bool(data.get("subtitle_safe_area_enabled", True)),
            subtitle_english_x_percent=_normalize_subtitle_position_percent(
                data.get("subtitle_english_x_percent", 50.0),
                50.0,
            ),
            subtitle_english_y_percent=_normalize_subtitle_position_percent(
                data.get("subtitle_english_y_percent", 90.0),
                90.0,
            ),
            subtitle_chinese_x_percent=_normalize_subtitle_position_percent(
                data.get("subtitle_chinese_x_percent", 50.0),
                50.0,
            ),
            subtitle_chinese_y_percent=_normalize_subtitle_position_percent(
                data.get("subtitle_chinese_y_percent", 82.0),
                82.0,
            ),
            burn_subtitles=bool(data.get("burn_subtitles", True)),
            export_sidecar_srt=bool(data.get("export_sidecar_srt", True)),
            audio_lufs_target=int(data.get("audio_lufs_target", -16)),
            output_container=str(data.get("output_container", "mp4")),
        )


@dataclass(slots=True)
class AppearanceSettings:
    theme_mode: str = "system"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppearanceSettings":
        normalized = str(data.get("theme_mode", "system")).strip().lower()
        if normalized not in {"system", "dark", "light"}:
            normalized = "system"
        return cls(theme_mode=normalized)


@dataclass(slots=True)
class AppSettings:
    appearance: AppearanceSettings = field(default_factory=AppearanceSettings)
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    media: MediaSettings = field(default_factory=MediaSettings)
    asr: ASRSettings = field(default_factory=ASRSettings)
    cutting: CuttingSettings = field(default_factory=CuttingSettings)
    translation: TranslationSettings = field(default_factory=TranslationSettings)
    tts: TTSSettings = field(default_factory=TTSSettings)
    export: ExportSettings = field(default_factory=ExportSettings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "appearance": asdict(self.appearance),
            "workspace": asdict(self.workspace),
            "media": asdict(self.media),
            "asr": asdict(self.asr),
            "cutting": asdict(self.cutting),
            "translation": asdict(self.translation),
            "tts": asdict(self.tts),
            "export": asdict(self.export),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSettings":
        return cls(
            appearance=AppearanceSettings.from_dict(data.get("appearance", {})),
            workspace=WorkspaceSettings.from_dict(data.get("workspace", {})),
            media=MediaSettings.from_dict(data.get("media", {})),
            asr=ASRSettings.from_dict(data.get("asr", {})),
            cutting=CuttingSettings.from_dict(data.get("cutting", {})),
            translation=TranslationSettings.from_dict(data.get("translation", {})),
            tts=TTSSettings.from_dict(data.get("tts", {})),
            export=ExportSettings.from_dict(data.get("export", {})),
        )


class SettingsStore:
    def __init__(self, settings_path: str | Path | None = None) -> None:
        if settings_path is None:
            settings_path = _default_settings_path()
        self.settings_path = Path(settings_path)

    def load(self) -> AppSettings:
        if not self.settings_path.exists():
            settings = AppSettings()
            self.save(settings)
            return settings
        data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        settings = AppSettings.from_dict(data)
        legacy_builtin_voices = {"", "emma_clear", "oliver_story", "maya_bright", "henry_anchor"}
        if (
            kokoro_local_assets_available()
            and settings.tts.provider_type == "builtin_voice_catalog"
            and settings.tts.default_voice in legacy_builtin_voices
        ):
            settings.tts.english_provider_type = "kokoro_local"
            settings.tts.english_default_voice = _default_tts_voice("kokoro_local", language="en")
        zh_kokoro_available = find_kokoro_model_dir(language="zh") is not None
        if zh_kokoro_available:
            if (
                not settings.tts.chinese_provider_type.strip()
                or (
                    settings.tts.chinese_provider_type == "melo_local"
                    and settings.tts.chinese_default_voice in {"", "melo_zh_female"}
                )
            ):
                settings.tts.chinese_provider_type = "kokoro_local"
            if settings.tts.chinese_provider_type == "kokoro_local" and (
                not settings.tts.chinese_default_voice.strip()
                or settings.tts.chinese_default_voice == "melo_zh_female"
            ):
                settings.tts.chinese_default_voice = _default_tts_voice("kokoro_local", language="zh")
        elif settings.tts.chinese_provider_type == "kokoro_local":
            if melo_local_assets_available():
                settings.tts.chinese_provider_type = "melo_local"
                settings.tts.chinese_default_voice = _default_tts_voice("melo_local", language="zh")
            else:
                settings.tts.chinese_provider_type = "builtin_voice_catalog"
                settings.tts.chinese_default_voice = ""
        elif melo_local_assets_available():
            if not settings.tts.chinese_provider_type.strip():
                settings.tts.chinese_provider_type = "melo_local"
            if not settings.tts.chinese_default_voice.strip():
                settings.tts.chinese_default_voice = _default_tts_voice("melo_local", language="zh")
        settings.tts.sync_legacy_defaults()
        return settings

    def save(self, settings: AppSettings) -> Path:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(settings.to_dict(), indent=2, ensure_ascii=False)
        self.settings_path.write_text(payload, encoding="utf-8")
        return self.settings_path
