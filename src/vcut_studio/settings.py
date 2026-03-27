from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .runtime_assets import kokoro_local_assets_available


DEFAULT_TRANSLATION_SYSTEM_PROMPT = """You are a professional audiovisual translator.
Translate each Chinese segment into natural spoken English.
Keep the meaning accurate, concise, and subtitle-friendly.
Return structured JSON that preserves each segment_id."""


def _default_workspace_dir() -> str:
    return str(Path.home() / "Documents" / "VCut Studio")


def _default_temp_dir() -> str:
    return str(Path(_default_workspace_dir()) / "temp")


def _default_output_dir() -> str:
    return str(Path.home() / "Videos" / "VCut Studio Exports")


def _default_asr_cache_dir() -> str:
    return str(Path.home() / ".cache" / "huggingface" / "hub")


def _default_tts_provider_type() -> str:
    if kokoro_local_assets_available():
        return "kokoro_local"
    return "builtin_voice_catalog"


def _default_tts_voice(provider_type: str | None = None) -> str:
    normalized = (provider_type or _default_tts_provider_type()).strip()
    if normalized == "kokoro_local":
        return "af_bella"
    return "emma_clear"


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
    preview_text: str = "Welcome to VCut Studio. This is a sample English dubbing preview."

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TTSSettings":
        provider_type = str(data.get("provider_type", _default_tts_provider_type()))
        return cls(
            provider_type=provider_type,
            default_voice=str(data.get("default_voice", _default_tts_voice(provider_type))),
            rate=float(data.get("rate", 1.0)),
            volume=float(data.get("volume", 1.0)),
            sample_rate=int(data.get("sample_rate", 22050)),
            retry_count=int(data.get("retry_count", 2)),
            preview_text=str(
                data.get(
                    "preview_text",
                    "Welcome to VCut Studio. This is a sample English dubbing preview.",
                )
            ),
        )


@dataclass(slots=True)
class ExportSettings:
    default_export_type: str = "clean_zh"
    subtitle_mode: str = "en"
    subtitle_font_size: int = 28
    burn_subtitles: bool = True
    export_sidecar_srt: bool = True
    audio_lufs_target: int = -16
    output_container: str = "mp4"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExportSettings":
        return cls(
            default_export_type=str(data.get("default_export_type", "clean_zh")),
            subtitle_mode=str(data.get("subtitle_mode", "en")),
            subtitle_font_size=int(data.get("subtitle_font_size", 28)),
            burn_subtitles=bool(data.get("burn_subtitles", True)),
            export_sidecar_srt=bool(data.get("export_sidecar_srt", True)),
            audio_lufs_target=int(data.get("audio_lufs_target", -16)),
            output_container=str(data.get("output_container", "mp4")),
        )


@dataclass(slots=True)
class AppSettings:
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    media: MediaSettings = field(default_factory=MediaSettings)
    asr: ASRSettings = field(default_factory=ASRSettings)
    cutting: CuttingSettings = field(default_factory=CuttingSettings)
    translation: TranslationSettings = field(default_factory=TranslationSettings)
    tts: TTSSettings = field(default_factory=TTSSettings)
    export: ExportSettings = field(default_factory=ExportSettings)

    def to_dict(self) -> dict[str, Any]:
        return {
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
            settings.tts.provider_type = "kokoro_local"
            settings.tts.default_voice = "af_bella"
        return settings

    def save(self, settings: AppSettings) -> Path:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(settings.to_dict(), indent=2, ensure_ascii=False)
        self.settings_path.write_text(payload, encoding="utf-8")
        return self.settings_path
