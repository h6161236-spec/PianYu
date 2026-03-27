from __future__ import annotations

import array
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from .models import MediaInfo
from .process_utils import subprocess_windowless_kwargs


class MediaProbeError(RuntimeError):
    """Raised when ffprobe cannot inspect a media file."""


@dataclass(slots=True)
class ProbeResult:
    media_path: Path
    media_info: MediaInfo
    raw_payload: dict[str, Any]


_PREVIEW_SAFE_VIDEO_CODECS = {
    "h264",
    "hevc",
    "h265",
    "mpeg4",
    "mjpeg",
    "wmv",
    "wmv2",
    "wmv3",
}
_PREVIEW_SAFE_AUDIO_CODECS = {
    "aac",
    "mp3",
    "ac3",
    "eac3",
    "flac",
    "wav",
    "wma",
    "wmav2",
    "alac",
    "pcm_s16le",
    "pcm_s24le",
}


def _runtime_binary_dirs() -> list[Path]:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    runtime_unpack_dir = getattr(sys, "_MEIPASS", None)
    if runtime_unpack_dir:
        candidates.append(Path(runtime_unpack_dir))

    unique_dirs: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.exists():
            continue
        unique_dirs.append(resolved)
        seen.add(resolved)
    return unique_dirs


def _binary_name_candidates(binary_name: str) -> list[str]:
    normalized = str(binary_name or "").strip()
    if not normalized:
        return []
    candidates = [normalized]
    if Path(normalized).suffix == "" and sys.platform == "win32":
        candidates.append(f"{normalized}.exe")
    return candidates


def resolve_binary_path(binary_name: str) -> str | None:
    normalized = str(binary_name or "").strip()
    if not normalized:
        return None

    binary_path = Path(normalized)
    has_explicit_directory = binary_path.is_absolute() or binary_path.parent != Path()
    if has_explicit_directory:
        return str(binary_path) if binary_path.exists() else None

    for runtime_dir in _runtime_binary_dirs():
        for candidate_name in _binary_name_candidates(normalized):
            candidate_path = runtime_dir / candidate_name
            if candidate_path.exists():
                return str(candidate_path)

    return shutil.which(normalized)


def resolve_ffmpeg_path(ffmpeg_path: str) -> str | None:
    return resolve_binary_path(ffmpeg_path)


def resolve_ffprobe_path(ffmpeg_path: str) -> str | None:
    resolved_ffmpeg = resolve_ffmpeg_path(ffmpeg_path) or ffmpeg_path
    ffmpeg_candidate = Path(resolved_ffmpeg)
    if ffmpeg_candidate.name.lower() == "ffmpeg.exe":
        sibling = ffmpeg_candidate.with_name("ffprobe.exe")
        if sibling.exists():
            return str(sibling)
    if ffmpeg_candidate.name.lower() == "ffmpeg":
        sibling = ffmpeg_candidate.with_name("ffprobe")
        if sibling.exists():
            return str(sibling)
    return resolve_binary_path("ffprobe")


def preview_proxy_required(media_path: str | Path, media_info: MediaInfo) -> bool:
    path_text = str(media_path or "")
    if any(ord(character) > 127 for character in path_text):
        return True

    video_codec = str(media_info.video_codec or "").strip().lower()
    audio_codec = str(media_info.audio_codec or "").strip().lower()

    if not video_codec or video_codec not in _PREVIEW_SAFE_VIDEO_CODECS:
        return True
    if media_info.has_audio and (not audio_codec or audio_codec not in _PREVIEW_SAFE_AUDIO_CODECS):
        return True
    return False


def build_preview_proxy_media(
    media_path: str | Path,
    media_info: MediaInfo,
    ffmpeg_path: str,
    output_dir: str | Path,
) -> Path:
    source_path = Path(media_path)
    if not source_path.exists():
        raise MediaProbeError(f"Media file does not exist: {source_path}")

    resolved_ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    if not resolved_ffmpeg:
        raise MediaProbeError(f"无法为视频监看找到 ffmpeg：{ffmpeg_path}")

    stat = source_path.stat()
    fingerprint = hashlib.sha1(
        f"{source_path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{media_info.video_codec}|{media_info.audio_codec}".encode(
            "utf-8",
            errors="ignore",
        )
    ).hexdigest()[:16]
    preview_dir = Path(output_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"preview_{fingerprint}.mp4"
    if preview_path.exists():
        return preview_path

    copy_compatible_streams = (
        str(media_info.video_codec or "").strip().lower() == "h264"
        and (
            not media_info.has_audio
            or str(media_info.audio_codec or "").strip().lower() == "aac"
        )
    )

    command = [
        resolved_ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
    ]
    if media_info.has_audio:
        command.extend(["-map", "0:a:0"])

    if copy_compatible_streams:
        command.extend(["-c:v", "copy"])
        if media_info.has_audio:
            command.extend(["-c:a", "copy"])
        else:
            command.append("-an")
    else:
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
            ]
        )
        if media_info.has_audio:
            command.extend(["-c:a", "aac", "-b:a", "160k"])
        else:
            command.append("-an")

    command.extend(["-movflags", "+faststart", str(preview_path)])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **subprocess_windowless_kwargs(),
    )
    if completed.returncode != 0 or not preview_path.exists():
        stderr_text = (completed.stderr or completed.stdout or "").strip()
        raise MediaProbeError(stderr_text or "准备视频监看预览副本失败。")
    return preview_path


def parse_frame_rate(raw_rate: str) -> float:
    if not raw_rate or raw_rate in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(raw_rate))
    except (ValueError, ZeroDivisionError):
        return 0.0


def parse_media_info(payload: dict[str, Any]) -> MediaInfo:
    streams = payload.get("streams", [])
    format_block = payload.get("format", {})
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    duration_seconds = float(format_block.get("duration") or 0.0)
    return MediaInfo(
        duration_ms=max(0, int(duration_seconds * 1000)),
        has_video=bool(video_stream),
        has_audio=bool(audio_stream),
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        fps=parse_frame_rate(str(video_stream.get("avg_frame_rate") or "0/0")),
        video_codec=str(video_stream.get("codec_name") or ""),
        audio_codec=str(audio_stream.get("codec_name") or ""),
        sample_rate=int(audio_stream.get("sample_rate") or 0),
    )


def parse_duration_to_ms(raw_duration: str) -> int:
    match = re.search(r"(\d+):(\d+):(\d+(?:\.\d+)?)", raw_duration)
    if not match:
        return 0
    hours = int(match.group(1))
    minutes = int(match.group(2))
    seconds = float(match.group(3))
    return int(((hours * 60 + minutes) * 60 + seconds) * 1000)


def parse_media_info_from_ffmpeg(stderr_text: str) -> MediaInfo:
    duration_match = re.search(r"Duration:\s*([0-9:.]+)", stderr_text)
    video_match = re.search(
        r"Video:\s*([^,\s]+).*?(\d{2,5})x(\d{2,5}).*?(\d+(?:\.\d+)?)\s+fps",
        stderr_text,
        re.DOTALL,
    )
    audio_match = re.search(
        r"Audio:\s*([^,\s]+).*?(\d{4,6})\s+Hz",
        stderr_text,
        re.DOTALL,
    )

    return MediaInfo(
        duration_ms=parse_duration_to_ms(duration_match.group(1)) if duration_match else 0,
        has_video=video_match is not None,
        has_audio=audio_match is not None,
        width=int(video_match.group(2)) if video_match else 0,
        height=int(video_match.group(3)) if video_match else 0,
        fps=float(video_match.group(4)) if video_match else 0.0,
        video_codec=video_match.group(1) if video_match else "",
        audio_codec=audio_match.group(1) if audio_match else "",
        sample_rate=int(audio_match.group(2)) if audio_match else 0,
    )


def probe_media_with_ffmpeg(media_path: Path, ffmpeg_path: str) -> ProbeResult:
    command = [resolve_ffmpeg_path(ffmpeg_path) or ffmpeg_path, "-hide_banner", "-i", str(media_path)]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **subprocess_windowless_kwargs(),
    )
    output_text = completed.stderr or completed.stdout
    if not output_text.strip():
        raise MediaProbeError("ffmpeg did not return any media information.")
    media_info = parse_media_info_from_ffmpeg(output_text)
    return ProbeResult(media_path=media_path, media_info=media_info, raw_payload={"stderr": output_text})


def probe_media(media_path: str | Path, ffmpeg_path: str = "ffmpeg") -> ProbeResult:
    media_file = Path(media_path)
    if not media_file.exists():
        raise MediaProbeError(f"Media file does not exist: {media_file}")

    ffprobe_path = resolve_ffprobe_path(ffmpeg_path)
    if not ffprobe_path:
        return probe_media_with_ffmpeg(media_file, ffmpeg_path)

    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(media_file),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **subprocess_windowless_kwargs(),
    )
    if completed.returncode != 0:
        return probe_media_with_ffmpeg(media_file, ffmpeg_path)

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError(f"Could not parse ffprobe JSON output: {exc}") from exc
    return ProbeResult(media_path=media_file, media_info=parse_media_info(payload), raw_payload=payload)


def build_waveform_points(
    samples: list[int] | array.array[int],
    point_count: int,
) -> list[float]:
    if point_count <= 0 or not samples:
        return []

    sample_count = len(samples)
    point_count = min(point_count, sample_count)
    points: list[float] = []

    for point_index in range(point_count):
        start = (point_index * sample_count) // point_count
        end = ((point_index + 1) * sample_count) // point_count
        if end <= start:
            end = min(sample_count, start + 1)
        chunk = samples[start:end]
        peak = max(abs(int(sample)) for sample in chunk) if chunk else 0
        points.append(min(1.0, peak / 32768.0))
    return points


def extract_audio_waveform(
    media_path: str | Path,
    ffmpeg_path: str = "ffmpeg",
    point_count: int = 900,
    sample_rate: int = 1200,
) -> list[float]:
    resolved_ffmpeg = resolve_ffmpeg_path(ffmpeg_path) or ffmpeg_path
    command = [
        resolved_ffmpeg,
        "-v",
        "error",
        "-i",
        str(media_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(max(200, sample_rate)),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        **subprocess_windowless_kwargs(),
    )
    if completed.returncode != 0:
        stderr_text = completed.stderr.decode("utf-8", errors="replace")
        raise MediaProbeError(stderr_text.strip() or "无法提取音轨波形。")

    if not completed.stdout:
        return []

    samples = array.array("h")
    samples.frombytes(completed.stdout)
    return build_waveform_points(samples, point_count)
