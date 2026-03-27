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
    find_sherpa_onnx_tts_executable,
    kokoro_local_assets_available,
    prepare_ascii_safe_runtime_path,
)
from ..settings import TTSSettings

BUILTIN_PROVIDER = "builtin_voice_catalog"
KOKORO_LOCAL_PROVIDER = "kokoro_local"


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

KOKORO_DEFAULT_VOICE_ID = "af_bella"
KOKORO_VOICE_SIDS = {
    voice.voice_id: voice_index for voice_index, voice in enumerate(KOKORO_LOCAL_VOICES)
}

TTS_PROVIDER_LABELS = {
    KOKORO_LOCAL_PROVIDER: "离线 Kokoro 英文音色",
    BUILTIN_PROVIDER: "Windows 系统语音",
}


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


def english_windows_voices() -> tuple[InstalledVoice, ...]:
    return tuple(voice for voice in installed_windows_voices() if voice.culture.lower().startswith("en"))


def builtin_english_voices() -> list[VoiceOption]:
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
                style="Installed",
            )
        )
    return options


def kokoro_local_voices() -> list[VoiceOption]:
    return list(KOKORO_LOCAL_VOICES)


def available_tts_voices(provider_type: str) -> list[VoiceOption]:
    if str(provider_type or "").strip() == KOKORO_LOCAL_PROVIDER:
        return kokoro_local_voices()
    return builtin_english_voices()


def all_tts_voices() -> list[VoiceOption]:
    merged: list[VoiceOption] = []
    seen: set[str] = set()
    for voice in kokoro_local_voices() + builtin_english_voices():
        if voice.voice_id in seen:
            continue
        merged.append(voice)
        seen.add(voice.voice_id)
    return merged


def kokoro_local_ready() -> bool:
    return kokoro_local_assets_available()


def tts_provider_choices() -> list[tuple[str, str]]:
    kokoro_label = "离线 Kokoro 英文音色（推荐）"
    if not kokoro_local_ready():
        kokoro_label = "离线 Kokoro 英文音色（需本地模型）"
    return [
        (kokoro_label, KOKORO_LOCAL_PROVIDER),
        ("内置音色目录（Windows 系统语音）", BUILTIN_PROVIDER),
    ]


def tts_provider_display_name(provider_type: str) -> str:
    normalized = str(provider_type or "").strip()
    return TTS_PROVIDER_LABELS.get(normalized, normalized or "未设置")


def tts_provider_runtime_status(provider_type: str) -> tuple[bool, str]:
    normalized = str(provider_type or "").strip()
    if normalized == KOKORO_LOCAL_PROVIDER:
        executable = find_sherpa_onnx_tts_executable()
        model_dir = find_kokoro_model_dir()
        missing_parts: list[str] = []
        if executable is None:
            missing_parts.append("sherpa-onnx 引擎")
        if model_dir is None:
            missing_parts.append("Kokoro 模型")
        if missing_parts:
            return (False, "缺少 " + "、".join(missing_parts))
        return (True, f"{model_dir.name} / {len(KOKORO_LOCAL_VOICES)} 个音色")

    voices = english_windows_voices()
    if voices:
        return (True, f"检测到 {len(voices)} 个英文系统音色")

    fallback_voices = installed_windows_voices()
    if fallback_voices:
        return (True, f"检测到 {len(fallback_voices)} 个系统音色")

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
        return builtin_english_voices()

    def _resolve_voice_name(self, voice_id: str) -> tuple[str, int]:
        inventory = list(installed_windows_voices())
        english_inventory = list(english_windows_voices())
        available_voices = english_inventory or inventory
        if not available_voices:
            raise TTSProviderError(
                "No Windows speech voices are available. Install an English system voice and try again."
            )

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
            raise TTSProviderError("Cannot synthesize an empty English segment.")
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


class KokoroLocalProvider(BaseTTSProvider):
    provider_name = KOKORO_LOCAL_PROVIDER

    def __init__(self, rate: float = 1.0) -> None:
        self.rate = rate
        executable = find_sherpa_onnx_tts_executable()
        model_dir = find_kokoro_model_dir()
        if executable is not None:
            staged_bin_dir = prepare_ascii_safe_runtime_path(executable.parent, "kokoro_bin")
            executable = staged_bin_dir / executable.name
        if model_dir is not None:
            model_dir = prepare_ascii_safe_runtime_path(model_dir, "kokoro_model")
        self.executable = executable
        self.model_dir = model_dir
        missing_parts: list[str] = []
        if self.executable is None:
            missing_parts.append("sherpa-onnx-offline-tts.exe")
        if self.model_dir is None:
            missing_parts.append("kokoro-en-v0_19 模型目录")
        if missing_parts:
            raise TTSProviderError(
                "未找到离线配音资源："
                + "、".join(missing_parts)
                + "。请把下载的文件放到程序目录下的 models/tts。"
            )

    def list_voices(self) -> list[VoiceOption]:
        return kokoro_local_voices()

    def _resolve_voice(self, voice_id: str) -> tuple[str, int]:
        normalized = str(voice_id or "").strip()
        if normalized in KOKORO_VOICE_SIDS:
            return (normalized, KOKORO_VOICE_SIDS[normalized])
        return (KOKORO_DEFAULT_VOICE_ID, KOKORO_VOICE_SIDS[KOKORO_DEFAULT_VOICE_ID])

    def _kokoro_length_scale(self) -> float:
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
            raise TTSProviderError("不能为一段空的英文文本生成配音。")
        if sys.platform != "win32":
            raise TTSProviderError("离线 Kokoro 配音当前仅支持 Windows。")

        output_path = Path(output_path).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_voice_id, sid = self._resolve_voice(voice_id)
        model_dir = self.model_dir
        executable = self.executable
        if model_dir is None or executable is None:
            raise TTSProviderError("离线 Kokoro 资源未就绪。")

        # sherpa-onnx 在部分 Windows 环境下写入中文/非 ASCII 路径会失败，
        # 这里统一先写到纯英文临时目录，再由 Python 挪到最终目标路径。
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
            normalized_text,
        ]
        try:
            _run_command(
                command,
                f"离线 Kokoro 配音失败（音色：{resolved_voice_id}）。",
                controller=controller,
                cwd=str(executable.parent),
                env=self._command_env(),
            )
            if not staged_output_path.exists() or staged_output_path.stat().st_size <= 0:
                raise TTSProviderError("离线 Kokoro 没有生成有效的音频文件。")
            shutil.move(str(staged_output_path), str(output_path))
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise TTSProviderError("离线 Kokoro 生成完成后，移动到目标目录失败。")
        return output_path


def create_tts_provider(settings: TTSSettings) -> BaseTTSProvider:
    provider_type = str(settings.provider_type or BUILTIN_PROVIDER).strip() or BUILTIN_PROVIDER
    if provider_type == KOKORO_LOCAL_PROVIDER:
        return KokoroLocalProvider(rate=settings.rate)
    if provider_type == BUILTIN_PROVIDER:
        return BuiltinVoiceCatalogProvider(rate=settings.rate)
    raise TTSProviderError(f"TTS provider type '{settings.provider_type}' is not supported yet.")
