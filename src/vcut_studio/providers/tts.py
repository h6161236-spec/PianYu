from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..process_utils import subprocess_windowless_kwargs
from ..runtime_assets import (
    find_kokoro_model_dir,
    find_melo_model_dir,
    find_sherpa_onnx_tts_executable,
    kokoro_local_assets_available,
    melo_local_assets_available,
    prepare_ascii_safe_runtime_path,
)
from ..settings import TTSSettings

BUILTIN_PROVIDER = "builtin_voice_catalog"
KOKORO_LOCAL_PROVIDER = "kokoro_local"
MELO_LOCAL_PROVIDER = "melo_local"


@dataclass(frozen=True, slots=True)
class VoiceOption:
    voice_id: str
    label: str
    accent: str
    style: str


@dataclass(frozen=True, slots=True)
class InstalledVoice:
    name: str
    culture: str


class TTSProviderError(RuntimeError):
    """Raised when a TTS provider request cannot be completed."""


PRESET_VOICES = [
    VoiceOption("emma_clear", "Emma Clear", "US", "Neutral"),
    VoiceOption("oliver_story", "Oliver Story", "UK", "Narration"),
    VoiceOption("maya_bright", "Maya Bright", "US", "Energetic"),
    VoiceOption("henry_anchor", "Henry Anchor", "US", "Broadcast"),
]

PRESET_RATE_OFFSETS = {
    "emma_clear": 0,
    "oliver_story": -1,
    "maya_bright": 1,
    "henry_anchor": -2,
}

KOKORO_MULTI_LANG_V1_VOICES = [
    VoiceOption("af_alloy", "Alloy", "US", "清亮干练，适合说明"),
    VoiceOption("af_aoede", "Aoede", "US", "成熟自然，适合讲解"),
    VoiceOption("af_bella", "Bella", "US", "亲和稳定，适合培训"),
    VoiceOption("af_heart", "Heart", "US", "轻柔温和，适合叙述"),
    VoiceOption("af_jessica", "Jessica", "US", "自然清晰，适合课程"),
    VoiceOption("af_kore", "Kore", "US", "明快利落，适合演示"),
    VoiceOption("af_nicole", "Nicole", "US", "温暖顺滑，适合讲解"),
    VoiceOption("af_nova", "Nova", "US", "年轻活泼，适合轻松口播"),
    VoiceOption("af_river", "River", "US", "平稳柔和，适合旁白"),
    VoiceOption("af_sarah", "Sarah", "US", "专业清晰，适合正式说明"),
    VoiceOption("af_sky", "Sky", "US", "明亮轻快，适合演示"),
    VoiceOption("am_adam", "Adam", "US", "自然男声，通用场景"),
    VoiceOption("am_echo", "Echo", "US", "轻快男声，适合短讲"),
    VoiceOption("am_eric", "Eric", "US", "清爽讲解，适合培训"),
    VoiceOption("am_fenrir", "Fenrir", "US", "厚实男声，适合旁白"),
    VoiceOption("am_liam", "Liam", "US", "利落播报，适合演示"),
    VoiceOption("am_michael", "Michael", "US", "沉稳专业，适合正式口播"),
    VoiceOption("am_onyx", "Onyx", "US", "成熟厚重，适合总结"),
    VoiceOption("am_puck", "Puck", "US", "轻松自然，适合教程"),
    VoiceOption("am_santa", "Santa", "US", "厚重男声，适合旁白"),
    VoiceOption("bf_alice", "Alice", "UK", "英式柔和，适合课程"),
    VoiceOption("bf_emma", "Emma", "UK", "英式亲和，适合培训"),
    VoiceOption("bf_isabella", "Isabella", "UK", "英式优雅，适合品牌介绍"),
    VoiceOption("bf_lily", "Lily", "UK", "英式轻柔，适合讲解"),
    VoiceOption("bm_daniel", "Daniel", "UK", "英式沉稳，适合解说"),
    VoiceOption("bm_fable", "Fable", "UK", "故事感强，适合叙述"),
    VoiceOption("bm_george", "George", "UK", "英式正式，适合商务展示"),
    VoiceOption("bm_lewis", "Lewis", "UK", "英式磁性，适合旁白"),
    VoiceOption("ef_dora", "Dora", "ES", "Female"),
    VoiceOption("em_alex", "Alex", "ES", "Male"),
    VoiceOption("ff_siwis", "Siwis", "FR", "Female"),
    VoiceOption("hf_alpha", "Alpha", "HI", "Female"),
    VoiceOption("hf_beta", "Beta", "HI", "Female"),
    VoiceOption("hm_omega", "Omega", "HI", "Male"),
    VoiceOption("hm_psi", "Psi", "HI", "Male"),
    VoiceOption("if_sara", "Sara", "IT", "Female"),
    VoiceOption("im_nicola", "Nicola", "IT", "Male"),
    VoiceOption("jf_alpha", "Alpha", "JP", "Female"),
    VoiceOption("jf_gongitsune", "Gongitsune", "JP", "Female"),
    VoiceOption("jf_nezumi", "Nezumi", "JP", "Female"),
    VoiceOption("jf_tebukuro", "Tebukuro", "JP", "Female"),
    VoiceOption("jm_kumo", "Kumo", "JP", "Male"),
    VoiceOption("pf_dora", "Dora", "BR", "Female"),
    VoiceOption("pm_alex", "Alex", "BR", "Male"),
    VoiceOption("pm_santa", "Santa", "BR", "Male"),
    VoiceOption("zf_xiaobei", "小贝", "ZH", "轻快亲和，适合培训"),
    VoiceOption("zf_xiaoni", "小妮", "ZH", "温柔自然，适合说明"),
    VoiceOption("zf_xiaoxiao", "小晓", "ZH", "清晰播报，适合正式口播"),
    VoiceOption("zf_xiaoyi", "小艺", "ZH", "成熟讲解，适合课程"),
    VoiceOption("zm_yunjian", "云健", "ZH", "稳重说明，适合汇报"),
    VoiceOption("zm_yunxi", "云希", "ZH", "清澈自然，适合教程"),
    VoiceOption("zm_yunxia", "云夏", "ZH", "年轻轻快，适合轻松内容"),
    VoiceOption("zm_yunyang", "云扬", "ZH", "正式播报，适合演示"),
]

KOKORO_MULTI_LANG_VOICE_SIDS = {
    voice.voice_id: voice_index for voice_index, voice in enumerate(KOKORO_MULTI_LANG_V1_VOICES)
}


def _build_kokoro_multi_lang_v1_1_voices() -> list[VoiceOption]:
    voices = [
        VoiceOption("af_maple", "Maple", "US", "明亮自然"),
        VoiceOption("af_sol", "Sol", "US", "温暖清晰"),
        VoiceOption("bf_vale", "Vale", "UK", "英式沉稳"),
    ]
    voices.extend(
        VoiceOption(f"zf_{index:03d}", f"中文女声 {index:03d}", "ZH", "中文女声，建议先试听")
        for index in range(1, 56)
    )
    voices.extend(
        VoiceOption(f"zm_{index:03d}", f"中文男声 {index:03d}", "ZH", "中文男声，建议先试听")
        for index in range(1, 46)
    )
    return voices


KOKORO_MULTI_LANG_V1_1_VOICES = _build_kokoro_multi_lang_v1_1_voices()
KOKORO_MULTI_LANG_V1_1_VOICE_SIDS = {
    voice.voice_id: voice_index for voice_index, voice in enumerate(KOKORO_MULTI_LANG_V1_1_VOICES)
}

KOKORO_LOCAL_VOICES = [
    VoiceOption("af", "AF", "美式", "基础女声"),
    VoiceOption("af_bella", "Bella", "美式", "温柔女声"),
    VoiceOption("af_nicole", "Nicole", "美式", "温暖女声"),
    VoiceOption("af_sarah", "Sarah", "美式", "清晰女声"),
    VoiceOption("af_sky", "Sky", "美式", "明亮女声"),
    VoiceOption("am_adam", "Adam", "美式", "男声"),
    VoiceOption("am_michael", "Michael", "美式", "沉稳男声"),
    VoiceOption("bf_emma", "Emma", "英式", "柔和女声"),
    VoiceOption("bf_isabella", "Isabella", "英式", "优雅女声"),
    VoiceOption("bm_george", "George", "英式", "男声"),
    VoiceOption("bm_lewis", "Lewis", "英式", "沉稳男声"),
]

MELO_LOCAL_VOICES = [
    VoiceOption("melo_zh_female", "Melo 中文女声", "ZH", "中文专用女声"),
]

KOKORO_DEFAULT_VOICE_ID = "af_bella"
MELO_DEFAULT_VOICE_ID = "melo_zh_female"
KOKORO_VOICE_SIDS = {
    voice.voice_id: voice_index for voice_index, voice in enumerate(KOKORO_LOCAL_VOICES)
}

TTS_PROVIDER_LABELS = {
    KOKORO_LOCAL_PROVIDER: "Kokoro 多语言 v1.1 离线音色",
    BUILTIN_PROVIDER: "Windows 系统语音",
}

TTS_PROVIDER_LABELS[MELO_LOCAL_PROVIDER] = "Melo 中文专用音色"


def _kokoro_model_is_multilang(model_dir: Path | None) -> bool:
    return model_dir is not None and "multi-lang" in model_dir.name.lower()


def _kokoro_model_is_multilang_v1_1(model_dir: Path | None) -> bool:
    return model_dir is not None and "multi-lang-v1_1" in model_dir.name.lower()


def _kokoro_all_voices(model_dir: Path | None = None) -> list[VoiceOption]:
    resolved_model_dir = model_dir or find_kokoro_model_dir()
    if _kokoro_model_is_multilang_v1_1(resolved_model_dir):
        return list(KOKORO_MULTI_LANG_V1_1_VOICES)
    if _kokoro_model_is_multilang(resolved_model_dir):
        return list(KOKORO_MULTI_LANG_V1_VOICES)
    return list(KOKORO_LOCAL_VOICES)


def _kokoro_voice_sid_map(model_dir: Path | None = None) -> dict[str, int]:
    resolved_model_dir = model_dir or find_kokoro_model_dir()
    if _kokoro_model_is_multilang_v1_1(resolved_model_dir):
        return dict(KOKORO_MULTI_LANG_V1_1_VOICE_SIDS)
    if _kokoro_model_is_multilang(resolved_model_dir):
        return dict(KOKORO_MULTI_LANG_VOICE_SIDS)
    return dict(KOKORO_VOICE_SIDS)


def _contains_latin_letters(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", str(text or "")))


def _kokoro_supports_mixed_zh_en(model_dir: Path) -> bool:
    return all(
        (model_dir / filename).exists()
        for filename in ("lexicon-zh.txt", "lexicon-us-en.txt", "lexicon-gb-en.txt")
    )


def _melo_all_voices() -> list[VoiceOption]:
    return list(MELO_LOCAL_VOICES)


def _controller_checkpoint(controller: object | None) -> None:
    if controller is None:
        return
    checkpoint = getattr(controller, "checkpoint", None)
    if callable(checkpoint):
        checkpoint()


def _controller_attach_process(
    controller: object | None,
    process: subprocess.Popen[str],
) -> None:
    if controller is None:
        return
    attach_process = getattr(controller, "attach_process", None)
    if callable(attach_process):
        attach_process(process)


def _controller_detach_process(
    controller: object | None,
    process: subprocess.Popen[str],
) -> None:
    if controller is None:
        return
    detach_process = getattr(controller, "detach_process", None)
    if callable(detach_process):
        detach_process(process)


def _run_command(
    command: list[str],
    error_message: str,
    controller: object | None = None,
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    _controller_checkpoint(controller)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
        env=env,
        **subprocess_windowless_kwargs(),
    )
    _controller_attach_process(controller, process)
    try:
        stdout_text, stderr_text = process.communicate()
    finally:
        _controller_detach_process(controller, process)
    _controller_checkpoint(controller)

    if process.returncode != 0:
        cleaned_stderr = _decode_powershell_clixml(stderr_text or "")
        stderr_tail = "\n".join((cleaned_stderr or "").strip().splitlines()[-12:])
        raise TTSProviderError(stderr_tail or error_message)
    return (stdout_text or "", stderr_text or "")


def _powershell_utf8_expression(value: str) -> str:
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return "[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('" + encoded + "'))"


def _powershell_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _decode_powershell_clixml(stderr_text: str) -> str:
    normalized = (stderr_text or "").strip()
    if not normalized.startswith("#< CLIXML"):
        return normalized

    text = normalized.replace("#< CLIXML", "")
    text = re.sub(r"</?Objs[^>]*>", "", text)
    text = re.sub(r"</?Obj[^>]*>", "", text)
    text = re.sub(r"<S[^>]*>", "", text)
    text = text.replace("</S>", "\n")

    def _replace_xml_escape(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    text = re.sub(r"_x([0-9A-Fa-f]{4})_", _replace_xml_escape, text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)


def _accent_label(culture: str) -> str:
    normalized = culture.lower()
    if normalized == "en-us":
        return "US"
    if normalized == "en-gb":
        return "UK"
    if normalized == "en-au":
        return "AU"
    return culture or "System"


_WINDOWS_UTF8_MANIFEST = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<assembly manifestVersion="1.0" xmlns="urn:schemas-microsoft-com:asm.v1">
  <assemblyIdentity version="1.0.0.0" processorArchitecture="*" name="VCutStudio.SherpaOnnxTts" type="win32"/>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">UTF-8</activeCodePage>
    </windowsSettings>
  </application>
</assembly>
"""


def _ensure_windows_utf8_manifest(executable: Path | None) -> None:
    if sys.platform != "win32" or executable is None:
        return
    manifest_path = executable.with_name(executable.name + ".manifest")
    try:
        existing = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    except Exception:
        existing = ""
    if existing == _WINDOWS_UTF8_MANIFEST:
        return
    try:
        manifest_path.write_text(_WINDOWS_UTF8_MANIFEST, encoding="utf-8")
    except Exception:
        # The UTF-8 manifest is a compatibility optimization for Chinese TTS on Windows.
        # If it cannot be written, keep the original behavior instead of blocking TTS entirely.
        return


@lru_cache(maxsize=1)
def installed_windows_voices() -> tuple[InstalledVoice, ...]:
    if sys.platform != "win32":
        return ()

    script = """
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voices = $synth.GetInstalledVoices() |
        Where-Object { $_.Enabled } |
        ForEach-Object {
            [pscustomobject]@{
                name = $_.VoiceInfo.Name
                culture = $_.VoiceInfo.Culture.Name
            }
        }
    $voices | ConvertTo-Json -Compress
} finally {
    $synth.Dispose()
}
""".strip()
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **subprocess_windowless_kwargs(),
    )
    if completed.returncode != 0:
        return ()

    stdout = (completed.stdout or "").strip()
    if not stdout:
        return ()

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return ()

    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return ()

    voices: list[InstalledVoice] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        culture = str(item.get("culture", "")).strip()
        if name:
            voices.append(InstalledVoice(name=name, culture=culture))
    return tuple(voices)


def windows_voices_for_language(language: str | None = None) -> tuple[InstalledVoice, ...]:
    normalized = str(language or "").strip().lower()
    if normalized == "zh":
        return tuple(
            voice
            for voice in installed_windows_voices()
            if voice.culture.lower().startswith(("zh", "cmn"))
        )
    if normalized == "en":
        return tuple(voice for voice in installed_windows_voices() if voice.culture.lower().startswith("en"))
    return installed_windows_voices()


def english_windows_voices() -> tuple[InstalledVoice, ...]:
    return windows_voices_for_language("en")


def builtin_voices(language: str = "en") -> list[VoiceOption]:
    normalized = str(language or "en").strip().lower()
    if normalized == "zh":
        return [
            VoiceOption(
                voice_id=f"system:{voice.name}",
                label=voice.name,
                accent=_accent_label(voice.culture),
                style="系统已安装",
            )
            for voice in windows_voices_for_language("zh")
        ]

    options = list(PRESET_VOICES)
    installed_names = {option.label for option in options}
    for voice in english_windows_voices():
        if voice.name in installed_names:
            continue
        options.append(
            VoiceOption(
                voice_id=f"system:{voice.name}",
                label=voice.name,
                accent=_accent_label(voice.culture),
                style="系统已安装",
            )
        )
    return options


def builtin_english_voices() -> list[VoiceOption]:
    return builtin_voices(language="en")


def melo_local_voices() -> list[VoiceOption]:
    return _melo_all_voices()


def kokoro_local_voices(language: str = "en") -> list[VoiceOption]:
    normalized = str(language or "").strip().lower()
    model_dir = find_kokoro_model_dir(language=normalized)
    if model_dir is None:
        return []
    voices = _kokoro_all_voices(model_dir)
    if normalized == "zh":
        if not _kokoro_model_is_multilang(model_dir):
            return []
        return [voice for voice in voices if voice.voice_id.startswith(("zf_", "zm_"))]
    if normalized == "en":
        if _kokoro_model_is_multilang(model_dir):
            return [voice for voice in voices if voice.voice_id.startswith(("af_", "am_", "bf_", "bm_"))]
        return voices
    return voices


def available_tts_voices(provider_type: str, language: str = "en") -> list[VoiceOption]:
    normalized_provider = str(provider_type or "").strip()
    if normalized_provider == KOKORO_LOCAL_PROVIDER:
        return kokoro_local_voices(language=language)
    if normalized_provider == MELO_LOCAL_PROVIDER:
        return melo_local_voices()
    return builtin_voices(language=language)


def all_tts_voices() -> list[VoiceOption]:
    merged: list[VoiceOption] = []
    seen: set[str] = set()
    for voice in (
        kokoro_local_voices(language="")
        + melo_local_voices()
        + builtin_voices(language="")
        + builtin_voices(language="zh")
    ):
        if voice.voice_id in seen:
            continue
        merged.append(voice)
        seen.add(voice.voice_id)
    return merged


def kokoro_local_ready(language: str = "") -> bool:
    normalized = str(language or "").strip().lower()
    if normalized == "zh":
        return find_kokoro_model_dir(language="zh") is not None and find_sherpa_onnx_tts_executable() is not None
    if normalized == "en":
        return find_kokoro_model_dir(language="en") is not None and find_sherpa_onnx_tts_executable() is not None
    return kokoro_local_assets_available()


def melo_local_ready() -> bool:
    return melo_local_assets_available()


def tts_provider_choices(language: str = "en") -> list[tuple[str, str]]:
    normalized = str(language or "en").strip().lower()
    if normalized == "zh":
        kokoro_label = "Kokoro 多语言 v1.1 中文音色（推荐）"
        builtin_label = "Windows 系统中文语音"
        choices: list[tuple[str, str]] = []
        if kokoro_local_ready("zh"):
            choices.append((kokoro_label, KOKORO_LOCAL_PROVIDER))
        if melo_local_ready():
            choices.append(("Melo 中文专用音色（女声）", MELO_LOCAL_PROVIDER))
        if windows_voices_for_language("zh"):
            choices.append((builtin_label, BUILTIN_PROVIDER))
        return choices or [(builtin_label, BUILTIN_PROVIDER)]
    elif normalized == "en":
        kokoro_label = "Kokoro 多语言 v1.1 英文音色（推荐）"
        builtin_label = "Windows 系统英文语音"
    else:
        kokoro_label = "Kokoro 多语言 v1.1 音色（推荐）"
        builtin_label = "Windows 系统语音"
    if not kokoro_local_ready("en" if normalized == "en" else ""):
        return [(builtin_label, BUILTIN_PROVIDER)]
    return [(kokoro_label, KOKORO_LOCAL_PROVIDER)]


def tts_provider_display_name(provider_type: str) -> str:
    normalized = str(provider_type or "").strip()
    return TTS_PROVIDER_LABELS.get(normalized, normalized or "未设置")


def tts_provider_runtime_status(provider_type: str, language: str = "en") -> tuple[bool, str]:
    normalized = str(provider_type or "").strip()
    normalized_language = str(language or "en").strip().lower()
    if normalized == MELO_LOCAL_PROVIDER:
        executable = find_sherpa_onnx_tts_executable()
        model_dir = find_melo_model_dir()
        missing_parts: list[str] = []
        if executable is None:
            missing_parts.append("sherpa-onnx 引擎")
        if model_dir is None:
            missing_parts.append("Melo 模型")
        if missing_parts:
            return (False, "缺少 " + "、".join(missing_parts))
        return (True, f"{model_dir.name} / {len(melo_local_voices())} 个音色")

    if normalized == KOKORO_LOCAL_PROVIDER:
        executable = find_sherpa_onnx_tts_executable()
        model_dir = find_kokoro_model_dir(language=normalized_language)
        missing_parts: list[str] = []
        if executable is None:
            missing_parts.append("sherpa-onnx 引擎")
        if model_dir is None:
            missing_parts.append("Kokoro 模型")
        if missing_parts:
            return (False, "缺少 " + "、".join(missing_parts))
        voice_count = len(available_tts_voices(provider_type, language=normalized_language))
        return (True, f"{model_dir.name} / {voice_count} 个音色")

    voices = windows_voices_for_language(normalized_language)
    if voices:
        if normalized_language == "zh":
            return (True, f"检测到 {len(voices)} 个中文系统语音")
        if normalized_language == "en":
            return (True, f"检测到 {len(voices)} 个英文系统语音")
        return (True, f"检测到 {len(voices)} 个系统语音")

    fallback_voices = installed_windows_voices()
    if fallback_voices:
        if normalized_language == "zh":
            return (False, f"未找到中文系统语音，但检测到 {len(fallback_voices)} 个其他系统语音")
        if normalized_language == "en":
            return (False, f"未找到英文系统语音，但检测到 {len(fallback_voices)} 个其他系统语音")
        return (True, f"检测到 {len(fallback_voices)} 个系统语音")

    return (False, "当前电脑没有可用的 Windows 系统语音")


def _preset_voice_assignments() -> dict[str, InstalledVoice]:
    available_voices = list(english_windows_voices())
    if not available_voices:
        available_voices = list(installed_windows_voices())
    if not available_voices:
        return {}
    assignments: dict[str, InstalledVoice] = {}
    for preset_index, preset in enumerate(PRESET_VOICES):
        assignments[preset.voice_id] = available_voices[preset_index % len(available_voices)]
    return assignments


def _sapi_rate(rate_multiplier: float, rate_offset: int = 0) -> int:
    base_rate = round((rate_multiplier - 1.0) * 8)
    return max(-10, min(10, base_rate + rate_offset))


class BaseTTSProvider(ABC):
    provider_name = "base"

    @abstractmethod
    def list_voices(self) -> list[VoiceOption]:
        raise NotImplementedError

    @abstractmethod
    def synthesize_segment(
        self,
        text: str,
        voice_id: str,
        output_path: str | Path,
        controller: object | None = None,
    ) -> Path:
        raise NotImplementedError


class BuiltinVoiceCatalogProvider(BaseTTSProvider):
    provider_name = BUILTIN_PROVIDER

    def __init__(self, rate: float = 1.0) -> None:
        self.rate = rate

    def list_voices(self) -> list[VoiceOption]:
        return builtin_voices(language="")

    def _resolve_voice_name(self, voice_id: str) -> tuple[str, int]:
        inventory = list(installed_windows_voices())
        english_inventory = list(english_windows_voices())
        available_voices = english_inventory or inventory
        if not available_voices:
            raise TTSProviderError("No Windows speech voices are available on this computer.")

        requested_voice_id = (voice_id or "").strip()
        if requested_voice_id.startswith("system:"):
            requested_name = requested_voice_id.split(":", 1)[1].strip()
            for voice in inventory:
                if voice.name == requested_name:
                    return voice.name, 0

        for voice in available_voices:
            if voice.name == requested_voice_id:
                return voice.name, 0

        assigned_voice = _preset_voice_assignments().get(requested_voice_id)
        if assigned_voice is not None:
            return assigned_voice.name, PRESET_RATE_OFFSETS.get(requested_voice_id, 0)

        preferred_voice = available_voices[0]
        return preferred_voice.name, PRESET_RATE_OFFSETS.get(requested_voice_id, 0)

    def synthesize_segment(
        self,
        text: str,
        voice_id: str,
        output_path: str | Path,
        controller: object | None = None,
    ) -> Path:
        normalized_text = text.strip()
        if not normalized_text:
            raise TTSProviderError("Cannot synthesize an empty segment.")
        if sys.platform != "win32":
            raise TTSProviderError("Built-in Windows voice synthesis is only available on Windows.")

        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        voice_name, rate_offset = self._resolve_voice_name(voice_id)
        rate_value = _sapi_rate(self.rate, rate_offset)
        script = (
            "Add-Type -AssemblyName System.Speech\n"
            f"$path = {_powershell_utf8_expression(str(output_path))}\n"
            f"$voice = {_powershell_utf8_expression(voice_name)}\n"
            f"$text = {_powershell_utf8_expression(normalized_text)}\n"
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            "try {\n"
            "    $synth.SelectVoice($voice)\n"
            f"    $synth.Rate = {rate_value}\n"
            "    $synth.Volume = 100\n"
            "    $synth.SetOutputToWaveFile($path)\n"
            "    $synth.Speak($text)\n"
            "} finally {\n"
            "    $synth.Dispose()\n"
            "}\n"
        )
        encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        _run_command(
            ["powershell", "-NoProfile", "-EncodedCommand", encoded_script],
            "Windows voice synthesis failed.",
            controller=controller,
        )
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise TTSProviderError("Windows voice synthesis did not create an output file.")
        return output_path


class MeloLocalProvider(BaseTTSProvider):
    provider_name = MELO_LOCAL_PROVIDER

    def __init__(self, rate: float = 1.0, preferred_voice_id: str = "") -> None:
        self.rate = rate
        self.preferred_voice_id = str(preferred_voice_id or MELO_DEFAULT_VOICE_ID).strip() or MELO_DEFAULT_VOICE_ID
        executable = find_sherpa_onnx_tts_executable()
        if executable is not None:
            staged_bin_dir = prepare_ascii_safe_runtime_path(executable.parent, "melo_bin")
            executable = staged_bin_dir / executable.name
        self.executable = executable
        _ensure_windows_utf8_manifest(self.executable)
        self.model_dir = self._resolve_model_dir()
        missing_parts: list[str] = []
        if self.executable is None:
            missing_parts.append("sherpa-onnx-offline-tts.exe")
        if self.model_dir is None:
            missing_parts.append("Melo 妯″瀷鐩綍")
        if missing_parts:
            raise TTSProviderError("??? Melo ???????" + "?".join(missing_parts))

    def list_voices(self) -> list[VoiceOption]:
        return melo_local_voices()

    def _resolve_model_dir(self) -> Path | None:
        model_dir = find_melo_model_dir()
        if model_dir is None:
            return None
        return prepare_ascii_safe_runtime_path(model_dir, "melo_model")

    def _resolve_voice(self, voice_id: str) -> str:
        requested_voice_id = str(voice_id or self.preferred_voice_id or "").strip()
        available_voice_ids = {voice.voice_id for voice in melo_local_voices()}
        if requested_voice_id in available_voice_ids:
            return requested_voice_id
        return MELO_DEFAULT_VOICE_ID

    def _vits_length_scale(self) -> float:
        rate = max(0.5, min(2.0, float(self.rate or 1.0)))
        return max(0.5, min(2.0, 1.0 / rate))

    def _command_env(self) -> dict[str, str]:
        env = dict(os.environ)
        executable_dir = str(self.executable.parent) if self.executable is not None else ""
        if executable_dir:
            env["PATH"] = executable_dir + os.pathsep + env.get("PATH", "")
        return env

    def synthesize_segment(
        self,
        text: str,
        voice_id: str,
        output_path: str | Path,
        controller: object | None = None,
    ) -> Path:
        normalized_text = " ".join(text.strip().split())
        if not normalized_text:
            raise TTSProviderError("????? Melo ???")
        if sys.platform != "win32":
            raise TTSProviderError("????? Melo ????? Windows ?????")

        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_voice_id = self._resolve_voice(voice_id)
        model_dir = self.model_dir
        executable = self.executable
        if model_dir is None or executable is None:
            raise TTSProviderError("Melo ????????")

        staging_dir = Path(tempfile.mkdtemp(prefix="vcut_melo_"))
        staged_output_path = staging_dir / "output.wav"
        rule_fst_paths = [
            str(model_dir / filename)
            for filename in ("date.fst", "number.fst", "phone.fst", "new_heteronym.fst")
            if (model_dir / filename).exists()
        ]
        command = [
            f"--vits-model={model_dir / 'model.onnx'}",
            f"--vits-tokens={model_dir / 'tokens.txt'}",
            f"--vits-lexicon={model_dir / 'lexicon.txt'}",
            f"--speed={self.rate:.3f}",
            f"--vits-length-scale={self._vits_length_scale():.3f}",
            "--provider=cpu",
            "--num-threads=2",
            "--sid=0",
            f"--output-filename={staged_output_path}",
        ]
        if rule_fst_paths:
            command.append("--tts-rule-fsts=" + ",".join(rule_fst_paths))
        command.append(normalized_text)
        powershell_command = "& " + _powershell_literal(str(executable))
        for argument in command:
            powershell_command += " " + _powershell_literal(argument)
        try:
            _run_command(
                ["powershell", "-NoProfile", "-Command", powershell_command],
                f"Melo ??????{resolved_voice_id}?",
                controller=controller,
                cwd=str(executable.parent),
                env=self._command_env(),
            )
            if not staged_output_path.exists() or staged_output_path.stat().st_size <= 0:
                raise TTSProviderError("Melo ???????????????")
            shutil.move(str(staged_output_path), str(output_path))
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise TTSProviderError("Melo ????????????????")
        return output_path


class KokoroLocalProvider(BaseTTSProvider):
    provider_name = KOKORO_LOCAL_PROVIDER

    def __init__(self, rate: float = 1.0, preferred_voice_id: str = "") -> None:
        self.rate = rate
        self.preferred_voice_id = str(preferred_voice_id or "").strip()
        executable = find_sherpa_onnx_tts_executable()
        if executable is not None:
            staged_bin_dir = prepare_ascii_safe_runtime_path(executable.parent, "kokoro_bin")
            executable = staged_bin_dir / executable.name
        self.executable = executable
        _ensure_windows_utf8_manifest(self.executable)
        self.model_dir = self._resolve_model_dir(self.preferred_voice_id)
        missing_parts: list[str] = []
        if self.executable is None:
            missing_parts.append("sherpa-onnx-offline-tts.exe")
        if self.model_dir is None:
            missing_parts.append("Kokoro 妯″瀷鐩綍")
        if missing_parts:
            raise TTSProviderError(
                "?? Kokoro ?????"
                + "?".join(missing_parts)
                + "???? models/tts ???"
            )

    def list_voices(self) -> list[VoiceOption]:
        if self.preferred_voice_id.startswith(("zf_", "zm_")):
            return kokoro_local_voices(language="zh")
        if self.preferred_voice_id.startswith(("af_", "am_", "bf_", "bm_")):
            return kokoro_local_voices(language="en")
        return kokoro_local_voices(language="")

    def _resolve_model_dir(self, voice_id: str) -> Path | None:
        model_dir = find_kokoro_model_dir(preferred_voice_id=voice_id or self.preferred_voice_id)
        if model_dir is None:
            return None
        return prepare_ascii_safe_runtime_path(model_dir, "kokoro_model")

    def _resolve_voice(self, voice_id: str) -> tuple[Path, str, int]:
        requested_voice_id = str(voice_id or self.preferred_voice_id or "").strip()
        model_dir = self._resolve_model_dir(requested_voice_id)
        if model_dir is None:
            raise TTSProviderError("Kokoro ????????")
        voice_sid_map = _kokoro_voice_sid_map(model_dir)
        if not voice_sid_map:
            raise TTSProviderError("Kokoro ?????????????")
        if requested_voice_id in voice_sid_map:
            return (model_dir, requested_voice_id, voice_sid_map[requested_voice_id])

        if requested_voice_id.startswith(("zf_", "zm_")):
            fallback_voice_id = "zf_xiaoxiao"
        elif requested_voice_id.startswith(("af_", "am_", "bf_", "bm_")):
            fallback_voice_id = "af_bella"
        else:
            fallback_voice_id = KOKORO_DEFAULT_VOICE_ID
        if fallback_voice_id not in voice_sid_map:
            fallback_voice_id = next(iter(voice_sid_map))
        return (model_dir, fallback_voice_id, voice_sid_map[fallback_voice_id])

    def _kokoro_length_scale(self) -> float:
        rate = max(0.5, min(2.0, float(self.rate or 1.0)))
        return max(0.5, min(2.0, 1.0 / rate))

    def _command_env(self) -> dict[str, str]:
        env = dict(os.environ)
        executable_dir = str(self.executable.parent) if self.executable is not None else ""
        if executable_dir:
            env["PATH"] = executable_dir + os.pathsep + env.get("PATH", "")
        return env

    def _optional_model_args(self, model_dir: Path, voice_id: str, text: str = "") -> list[str]:
        optional_args: list[str] = []
        lexicon_candidates: list[Path]
        use_mixed_zh_en_lexicons = (
            voice_id.startswith(("zf_", "zm_"))
            and _contains_latin_letters(text)
            and _kokoro_supports_mixed_zh_en(model_dir)
        )
        if voice_id.startswith(("zf_", "zm_")):
            if not use_mixed_zh_en_lexicons:
                optional_args.append("--kokoro-lang=zh")
            lexicon_candidates = [model_dir / "lexicon-zh.txt"]
            if use_mixed_zh_en_lexicons:
                lexicon_candidates.extend(
                    [
                        model_dir / "lexicon-us-en.txt",
                        model_dir / "lexicon-gb-en.txt",
                    ]
                )
        elif voice_id.startswith("bf_"):
            optional_args.append("--kokoro-lang=en-gb")
            lexicon_candidates = [model_dir / "lexicon-gb-en.txt"]
        elif voice_id.startswith(("af_", "am_")):
            optional_args.append("--kokoro-lang=en-us")
            lexicon_candidates = [model_dir / "lexicon-us-en.txt"]
        else:
            lexicon_candidates = [
                model_dir / "lexicon-gb-en.txt",
                model_dir / "lexicon-us-en.txt",
                model_dir / "lexicon-zh.txt",
            ]
        existing_lexicons = [path for path in lexicon_candidates if path.exists()]
        if existing_lexicons:
            optional_args.append(
                "--kokoro-lexicon=" + ",".join(str(path) for path in existing_lexicons)
            )
        if voice_id.startswith(("zf_", "zm_")):
            rule_fst_candidates = [
                model_dir / "date-zh.fst",
                model_dir / "number-zh.fst",
                model_dir / "phone-zh.fst",
            ]
            existing_rule_fsts = [path for path in rule_fst_candidates if path.exists()]
            if existing_rule_fsts:
                optional_args.append(
                    "--tts-rule-fsts=" + ",".join(str(path) for path in existing_rule_fsts)
                )
        return optional_args

    def synthesize_segment(
        self,
        text: str,
        voice_id: str,
        output_path: str | Path,
        controller: object | None = None,
    ) -> Path:
        normalized_text = " ".join(text.strip().split())
        if not normalized_text:
            raise TTSProviderError("不能为一段空文本生成试听。")
        if sys.platform != "win32":
            raise TTSProviderError("Kokoro 离线配音当前仅支持 Windows。")

        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        model_dir, resolved_voice_id, sid = self._resolve_voice(voice_id)
        executable = self.executable
        if model_dir is None or executable is None:
            raise TTSProviderError("Kokoro 离线资源未就绪。")

        # Some Windows TTS tools fail on non-ASCII output paths, so render to an
        # ASCII-only staging folder first and move the file afterwards.
        staging_dir = Path(tempfile.mkdtemp(prefix="vcut_kokoro_"))
        staged_output_path = staging_dir / "output.wav"

        command = [
            str(executable),
            f"--kokoro-model={model_dir / 'model.onnx'}",
            f"--kokoro-voices={model_dir / 'voices.bin'}",
            f"--kokoro-tokens={model_dir / 'tokens.txt'}",
            f"--kokoro-data-dir={model_dir / 'espeak-ng-data'}",
            f"--sid={sid}",
            f"--kokoro-length-scale={self._kokoro_length_scale():.3f}",
            "--provider=cpu",
            "--num-threads=2",
            f"--output-filename={staged_output_path}",
        ]
        command.extend(self._optional_model_args(model_dir, resolved_voice_id, normalized_text))
        command.append(normalized_text)
        try:
            _run_command(
                command,
                f"Kokoro 配音失败（音色：{resolved_voice_id}）。",
                controller=controller,
                cwd=str(executable.parent),
                env=self._command_env(),
            )
            if not staged_output_path.exists() or staged_output_path.stat().st_size <= 0:
                raise TTSProviderError("Kokoro 没有生成有效的音频文件。")
            shutil.move(str(staged_output_path), str(output_path))
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise TTSProviderError("Kokoro 生成完成后，移动到目标路径失败。")
        return output_path


def create_tts_provider(settings: TTSSettings) -> BaseTTSProvider:
    provider_type = str(settings.provider_type or BUILTIN_PROVIDER).strip() or BUILTIN_PROVIDER
    if provider_type == MELO_LOCAL_PROVIDER:
        return MeloLocalProvider(rate=settings.rate, preferred_voice_id=settings.default_voice)
    if provider_type == KOKORO_LOCAL_PROVIDER:
        return KokoroLocalProvider(rate=settings.rate, preferred_voice_id=settings.default_voice)
    if provider_type == BUILTIN_PROVIDER:
        return BuiltinVoiceCatalogProvider(rate=settings.rate)
    raise TTSProviderError(f"TTS provider type '{settings.provider_type}' is not supported yet.")
