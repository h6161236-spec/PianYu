from __future__ import annotations

import re
import subprocess
import tempfile
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .media import probe_media, resolve_ffmpeg_path
from .models import CutSuggestion, Project
from .process_utils import subprocess_windowless_kwargs
from .providers.tts import create_tts_provider
from .project_store import sanitize_filename
from .settings import AppSettings
from .subtitles import SubtitleCueData, read_subtitle_file


class ExportError(RuntimeError):
    """Raised when media export cannot be completed."""


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    index: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class DubCue:
    index: int
    segment_index: int
    start_ms: int
    end_ms: int
    text: str
    voice_id: str


@dataclass(frozen=True, slots=True)
class DubAudioInput:
    cue: DubCue
    clip_path: Path
    clip_duration_ms: int


@dataclass(frozen=True, slots=True)
class TimelineSegment:
    source_start_ms: int
    source_end_ms: int
    output_start_ms: int
    output_end_ms: int

    @property
    def source_duration_ms(self) -> int:
        return max(0, self.source_end_ms - self.source_start_ms)

    @property
    def output_duration_ms(self) -> int:
        return max(0, self.output_end_ms - self.output_start_ms)


@dataclass(frozen=True, slots=True)
class TimeRange:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(slots=True)
class ExportPlan:
    cut_ranges: list[TimeRange]
    keep_ranges: list[TimeRange]
    output_path: Path


ProgressCallback = Callable[[int, str], None] | None


def _effective_ffmpeg_path(settings: AppSettings) -> str:
    return resolve_ffmpeg_path(settings.media.ffmpeg_path) or settings.media.ffmpeg_path


def _notify_progress(progress_callback: ProgressCallback, progress: int, message: str) -> None:
    if progress_callback is not None:
        progress_callback(progress, message)


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
) -> tuple[str, str]:
    _controller_checkpoint(controller)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **subprocess_windowless_kwargs(),
    )
    _controller_attach_process(controller, process)
    try:
        stdout_text, stderr_text = process.communicate()
    finally:
        _controller_detach_process(controller, process)
    _controller_checkpoint(controller)

    if process.returncode != 0:
        stderr_tail = "\n".join((stderr_text or "").strip().splitlines()[-12:])
        raise ExportError(stderr_tail or error_message)
    return (stdout_text or "", stderr_text or "")


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))


def accepted_cut_ranges(
    suggestions: list[CutSuggestion],
    total_duration_ms: int,
) -> list[TimeRange]:
    accepted = [
        TimeRange(
            start_ms=_clamp(suggestion.start_ms, 0, total_duration_ms),
            end_ms=_clamp(suggestion.end_ms, 0, total_duration_ms),
        )
        for suggestion in suggestions
        if suggestion.accepted
    ]
    accepted = [item for item in accepted if item.end_ms > item.start_ms]
    if not accepted:
        return []

    accepted.sort(key=lambda item: (item.start_ms, item.end_ms))
    merged: list[TimeRange] = []
    for item in accepted:
        if not merged:
            merged.append(item)
            continue
        previous = merged[-1]
        if item.start_ms <= previous.end_ms:
            merged[-1] = TimeRange(previous.start_ms, max(previous.end_ms, item.end_ms))
        else:
            merged.append(item)
    return merged


def keep_ranges_from_cuts(total_duration_ms: int, cut_ranges: list[TimeRange]) -> list[TimeRange]:
    if total_duration_ms <= 0:
        return []
    if not cut_ranges:
        return [TimeRange(0, total_duration_ms)]

    keep_ranges: list[TimeRange] = []
    cursor = 0
    for cut_range in cut_ranges:
        if cut_range.start_ms > cursor:
            keep_ranges.append(TimeRange(cursor, cut_range.start_ms))
        cursor = max(cursor, cut_range.end_ms)
    if cursor < total_duration_ms:
        keep_ranges.append(TimeRange(cursor, total_duration_ms))
    return [item for item in keep_ranges if item.duration_ms > 0]


def ms_to_seconds(value_ms: int) -> str:
    return f"{value_ms / 1000:.6f}"


def ms_to_srt_timestamp(value_ms: int) -> str:
    total_ms = max(0, value_ms)
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def build_filter_complex(keep_ranges: list[TimeRange], has_video: bool, has_audio: bool) -> str:
    if not keep_ranges:
        raise ExportError("No media remains after applying accepted cut suggestions.")
    if not has_video and not has_audio:
        raise ExportError("The source media does not contain audio or video streams.")

    parts: list[str] = []
    concat_inputs: list[str] = []

    for index, time_range in enumerate(keep_ranges):
        start = ms_to_seconds(time_range.start_ms)
        end = ms_to_seconds(time_range.end_ms)
        if has_video:
            parts.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{index}]")
            concat_inputs.append(f"[v{index}]")
        if has_audio:
            parts.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{index}]")
            concat_inputs.append(f"[a{index}]")

    concat_v = 1 if has_video else 0
    concat_a = 1 if has_audio else 0
    outputs = []
    if has_video:
        outputs.append("[outv]")
    if has_audio:
        outputs.append("[outa]")
    parts.append(
        f"{''.join(concat_inputs)}concat=n={len(keep_ranges)}:v={concat_v}:a={concat_a}{''.join(outputs)}"
    )
    return ";\n".join(parts)


def render_keep_ranges_to_video(
    project: Project,
    settings: AppSettings,
    keep_ranges: list[TimeRange],
    output_path: str | Path,
    controller: object | None = None,
) -> Path:
    output_path = Path(output_path)
    _controller_checkpoint(controller)
    filter_complex = build_filter_complex(
        keep_ranges,
        has_video=project.media_info.has_video,
        has_audio=project.media_info.has_audio,
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".fftxt",
        encoding="utf-8",
        delete=False,
    ) as script_file:
        script_file.write(filter_complex)
        script_path = Path(script_file.name)

    command = [
        _effective_ffmpeg_path(settings),
        "-y" if settings.media.overwrite_existing else "-n",
        "-hide_banner",
        "-i",
        project.video_path,
        "-filter_complex_script",
        str(script_path),
    ]
    if project.media_info.has_video:
        command.extend(["-map", "[outv]", "-c:v", settings.media.video_codec])
    if project.media_info.has_audio:
        command.extend(["-map", "[outa]", "-c:a", settings.media.audio_codec])
    command.append(str(output_path))

    try:
        _run_command(command, "ffmpeg export failed.", controller=controller)
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass
    return output_path


def choose_output_path(
    project_name: str,
    output_dir: str | Path,
    export_type: str,
    container: str,
    overwrite_existing: bool,
) -> Path:
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    base_name = sanitize_filename(project_name)
    suffix = f"_{export_type}"
    container = container.lstrip(".") or "mp4"
    candidate = output_root / f"{base_name}{suffix}.{container}"
    if overwrite_existing or not candidate.exists():
        return candidate

    index = 2
    while True:
        candidate = output_root / f"{base_name}{suffix}_{index}.{container}"
        if not candidate.exists():
            return candidate
        index += 1


def _subtitle_text_for_segment(segment: object, subtitle_mode: str) -> str:
    english_text = str(getattr(segment, "en_text", "") or "").strip()
    chinese_text = str(getattr(segment, "zh_text", "") or "").strip()
    normalized_mode = str(subtitle_mode or "en").strip().lower()
    if normalized_mode == "bilingual":
        lines = [line for line in (chinese_text, english_text) if line]
        return "\n".join(lines)
    if normalized_mode == "zh":
        return chinese_text
    return english_text


def build_subtitle_cues(
    project: Project,
    keep_ranges: list[TimeRange],
    subtitle_mode: str = "en",
) -> list[SubtitleCue]:
    timeline_offsets = build_timeline_offsets(
        project.source_duration_ms or project.media_info.duration_ms,
        keep_ranges,
    )

    cues: list[SubtitleCue] = []
    cue_index = 1
    for segment in project.segments:
        subtitle_text = _subtitle_text_for_segment(segment, subtitle_mode)
        if not subtitle_text:
            continue
        for keep_range, offset_ms in timeline_offsets:
            overlap_start = max(segment.start_ms, keep_range.start_ms)
            overlap_end = min(segment.end_ms, keep_range.end_ms)
            if overlap_end <= overlap_start:
                continue
            edited_start = offset_ms + (overlap_start - keep_range.start_ms)
            edited_end = offset_ms + (overlap_end - keep_range.start_ms)
            if edited_end <= edited_start:
                continue
            cues.append(
                SubtitleCue(
                    index=cue_index,
                    start_ms=edited_start,
                    end_ms=edited_end,
                    text=subtitle_text,
                )
            )
            cue_index += 1
    return cues


def build_timeline_offsets(
    total_duration_ms: int,
    keep_ranges: list[TimeRange],
) -> list[tuple[TimeRange, int]]:
    active_keep_ranges = keep_ranges or [TimeRange(0, total_duration_ms)]
    timeline_offsets: list[tuple[TimeRange, int]] = []
    running_total = 0
    for time_range in active_keep_ranges:
        timeline_offsets.append((time_range, running_total))
        running_total += time_range.duration_ms
    return timeline_offsets


def retime_range(
    start_ms: int,
    end_ms: int,
    timeline_offsets: list[tuple[TimeRange, int]],
) -> TimeRange | None:
    edited_start: int | None = None
    edited_end: int | None = None
    for keep_range, offset_ms in timeline_offsets:
        overlap_start = max(start_ms, keep_range.start_ms)
        overlap_end = min(end_ms, keep_range.end_ms)
        if overlap_end <= overlap_start:
            continue
        current_start = offset_ms + (overlap_start - keep_range.start_ms)
        current_end = offset_ms + (overlap_end - keep_range.start_ms)
        if edited_start is None:
            edited_start = current_start
        edited_end = current_end
    if edited_start is None or edited_end is None or edited_end <= edited_start:
        return None
    return TimeRange(start_ms=edited_start, end_ms=edited_end)


def build_dub_cues(
    project: Project,
    keep_ranges: list[TimeRange],
    default_voice: str,
) -> list[DubCue]:
    timeline_offsets = build_timeline_offsets(
        project.source_duration_ms or project.media_info.duration_ms,
        keep_ranges,
    )
    cues: list[DubCue] = []
    cue_index = 1
    for segment_index, segment in enumerate(project.segments):
        english_text = segment.en_text.strip()
        if not english_text:
            continue
        edited_range = retime_range(segment.start_ms, segment.end_ms, timeline_offsets)
        if edited_range is None:
            continue
        cues.append(
            DubCue(
                index=cue_index,
                segment_index=segment_index,
                start_ms=edited_range.start_ms,
                end_ms=edited_range.end_ms,
                text=english_text,
                voice_id=(segment.voice_id or default_voice or "").strip(),
            )
        )
        cue_index += 1
    return cues


def build_dub_video_timeline(
    total_duration_ms: int,
    dub_inputs: list[DubAudioInput],
) -> list[TimelineSegment]:
    if total_duration_ms <= 0:
        return []

    timeline_segments: list[TimelineSegment] = []
    ordered_inputs = sorted(
        dub_inputs,
        key=lambda item: (item.cue.start_ms, item.cue.end_ms, item.cue.index),
    )
    source_cursor = 0
    output_cursor = 0

    for item in ordered_inputs:
        cue = item.cue
        cue_start_ms = max(source_cursor, int(cue.start_ms))
        cue_end_ms = min(total_duration_ms, int(cue.end_ms))
        if cue_end_ms <= cue_start_ms:
            continue
        if cue_start_ms > source_cursor:
            passthrough_duration_ms = cue_start_ms - source_cursor
            timeline_segments.append(
                TimelineSegment(
                    source_start_ms=source_cursor,
                    source_end_ms=cue_start_ms,
                    output_start_ms=output_cursor,
                    output_end_ms=output_cursor + passthrough_duration_ms,
                )
            )
            output_cursor += passthrough_duration_ms

        cue_duration_ms = cue_end_ms - cue_start_ms
        target_duration_ms = max(cue_duration_ms, int(item.clip_duration_ms))
        timeline_segments.append(
            TimelineSegment(
                source_start_ms=cue_start_ms,
                source_end_ms=cue_end_ms,
                output_start_ms=output_cursor,
                output_end_ms=output_cursor + target_duration_ms,
            )
        )
        source_cursor = cue_end_ms
        output_cursor += target_duration_ms

    if source_cursor < total_duration_ms:
        tail_duration_ms = total_duration_ms - source_cursor
        timeline_segments.append(
            TimelineSegment(
                source_start_ms=source_cursor,
                source_end_ms=total_duration_ms,
                output_start_ms=output_cursor,
                output_end_ms=output_cursor + tail_duration_ms,
            )
        )
    return [segment for segment in timeline_segments if segment.source_duration_ms > 0]


def has_stretched_dub_video_timeline(timeline_segments: list[TimelineSegment]) -> bool:
    return any(segment.output_duration_ms != segment.source_duration_ms for segment in timeline_segments)


def retime_range_with_timeline_segments(
    start_ms: int,
    end_ms: int,
    timeline_segments: list[TimelineSegment],
) -> TimeRange | None:
    edited_start: int | None = None
    edited_end: int | None = None

    for segment in timeline_segments:
        overlap_start = max(start_ms, segment.source_start_ms)
        overlap_end = min(end_ms, segment.source_end_ms)
        if overlap_end <= overlap_start:
            continue

        if segment.source_duration_ms <= 0:
            continue
        relative_start = overlap_start - segment.source_start_ms
        relative_end = overlap_end - segment.source_start_ms
        current_start = segment.output_start_ms + round(
            relative_start * segment.output_duration_ms / segment.source_duration_ms
        )
        current_end = segment.output_start_ms + round(
            relative_end * segment.output_duration_ms / segment.source_duration_ms
        )
        if edited_start is None:
            edited_start = current_start
        edited_end = current_end

    if edited_start is None or edited_end is None or edited_end <= edited_start:
        return None
    return TimeRange(start_ms=edited_start, end_ms=edited_end)


def build_stretched_dub_cues(
    dub_inputs: list[DubAudioInput],
    timeline_segments: list[TimelineSegment],
) -> list[DubCue]:
    stretched_cues: list[DubCue] = []
    for item in dub_inputs:
        stretched_range = retime_range_with_timeline_segments(
            item.cue.start_ms,
            item.cue.end_ms,
            timeline_segments,
        )
        if stretched_range is None:
            continue
        stretched_cues.append(
            DubCue(
                index=item.cue.index,
                segment_index=item.cue.segment_index,
                start_ms=stretched_range.start_ms,
                end_ms=stretched_range.end_ms,
                text=item.cue.text,
                voice_id=item.cue.voice_id,
            )
        )
    return stretched_cues


def build_subtitle_cues_from_dub_cues(
    project: Project,
    dub_cues: list[DubCue],
    subtitle_mode: str = "en",
) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    cue_index = 1
    for dub_cue in dub_cues:
        if not (0 <= dub_cue.segment_index < len(project.segments)):
            continue
        segment = project.segments[dub_cue.segment_index]
        subtitle_text = _subtitle_text_for_segment(segment, subtitle_mode)
        if not subtitle_text:
            continue
        cues.append(
            SubtitleCue(
                index=cue_index,
                start_ms=dub_cue.start_ms,
                end_ms=dub_cue.end_ms,
                text=subtitle_text,
            )
        )
        cue_index += 1
    return cues


def write_srt(cues: list[SubtitleCue], output_path: str | Path) -> Path:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for cue in cues:
        lines.extend(
            [
                str(cue.index),
                f"{ms_to_srt_timestamp(cue.start_ms)} --> {ms_to_srt_timestamp(cue.end_ms)}",
                cue.text,
                "",
            ]
        )
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination


def subtitle_codec_for_container(container: str) -> str:
    normalized = container.lower().lstrip(".")
    if normalized in {"mp4", "mov"}:
        return "mov_text"
    return "srt"


def ffmpeg_filter_path(path: str | Path) -> str:
    value = Path(path).as_posix()
    value = value.replace("\\", "/")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    return value


_HEX_COLOR_PATTERN = re.compile(r"^#?(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")
_DEFAULT_SUBTITLE_ENGLISH_COLOR = "#FFFFFF"
_DEFAULT_SUBTITLE_CHINESE_COLOR = "#FFD966"
_SUBTITLE_SAFE_MARGIN_X_PERCENT = 8.0
_SUBTITLE_SAFE_MARGIN_Y_PERCENT = 6.0
_SUBTITLE_FONT_REFERENCE_HEIGHT = 480.0


def _normalize_subtitle_hex_color(value: str | None, fallback: str) -> str:
    normalized = str(value or "").strip()
    if _HEX_COLOR_PATTERN.fullmatch(normalized):
        return f"#{normalized.lstrip('#').upper()}"
    return fallback


def _hex_color_to_ass(value: str) -> str:
    normalized = value.upper().lstrip("#")
    if len(normalized) == 6:
        red = int(normalized[0:2], 16)
        green = int(normalized[2:4], 16)
        blue = int(normalized[4:6], 16)
        alpha = 0
    else:
        alpha_value = int(normalized[0:2], 16)
        red = int(normalized[2:4], 16)
        green = int(normalized[4:6], 16)
        blue = int(normalized[6:8], 16)
        alpha = max(0, min(255, 255 - alpha_value))
    return f"&H{alpha:02X}{blue:02X}{green:02X}{red:02X}"


def _subtitle_ass_color(settings: AppSettings, subtitle_language: str) -> str:
    export_settings = getattr(settings, "export", None)
    if subtitle_language == "zh":
        configured = getattr(export_settings, "subtitle_chinese_color", _DEFAULT_SUBTITLE_CHINESE_COLOR)
        color_value = _normalize_subtitle_hex_color(configured, _DEFAULT_SUBTITLE_CHINESE_COLOR)
    else:
        configured = getattr(export_settings, "subtitle_english_color", _DEFAULT_SUBTITLE_ENGLISH_COLOR)
        color_value = _normalize_subtitle_hex_color(configured, _DEFAULT_SUBTITLE_ENGLISH_COLOR)
    return _hex_color_to_ass(color_value)


def _subtitle_font_size_for_canvas(
    configured_font_size: int,
    *,
    canvas_height: int,
) -> int:
    normalized_size = max(1, int(configured_font_size or 28))
    normalized_height = max(1, int(canvas_height or 1080))
    scaled_size = normalized_size * (normalized_height / _SUBTITLE_FONT_REFERENCE_HEIGHT)
    return max(1, int(round(scaled_size)))


def _subtitle_position_percent(settings: AppSettings, subtitle_language: str) -> tuple[float, float]:
    export_settings = getattr(settings, "export", None)
    if subtitle_language == "zh":
        x_percent = getattr(export_settings, "subtitle_chinese_x_percent", 50.0)
        y_percent = getattr(export_settings, "subtitle_chinese_y_percent", 82.0)
    else:
        x_percent = getattr(export_settings, "subtitle_english_x_percent", 50.0)
        y_percent = getattr(export_settings, "subtitle_english_y_percent", 90.0)
    try:
        normalized_x = float(x_percent)
    except (TypeError, ValueError):
        normalized_x = 50.0
    try:
        normalized_y = float(y_percent)
    except (TypeError, ValueError):
        normalized_y = 90.0 if subtitle_language != "zh" else 82.0
    return (
        max(0.0, min(100.0, normalized_x)),
        max(0.0, min(100.0, normalized_y)),
    )


def _subtitle_safe_area_enabled(settings: AppSettings) -> bool:
    export_settings = getattr(settings, "export", None)
    return bool(getattr(export_settings, "subtitle_safe_area_enabled", True))


def _subtitle_position_pixels(
    settings: AppSettings,
    subtitle_language: str,
    *,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
) -> tuple[int, int]:
    x_percent, y_percent = _subtitle_position_percent(settings, subtitle_language)
    if _subtitle_safe_area_enabled(settings):
        x_percent = min(max(x_percent, _SUBTITLE_SAFE_MARGIN_X_PERCENT), 100.0 - _SUBTITLE_SAFE_MARGIN_X_PERCENT)
        y_percent = min(max(y_percent, _SUBTITLE_SAFE_MARGIN_Y_PERCENT), 100.0 - _SUBTITLE_SAFE_MARGIN_Y_PERCENT)
    return (
        int(round(play_res_x * (x_percent / 100.0))),
        int(round(play_res_y * (y_percent / 100.0))),
    )


def _ms_to_ass_timestamp(value_ms: int) -> str:
    total_ms = max(0, int(value_ms))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    centiseconds = int(round(milliseconds / 10.0))
    if centiseconds >= 100:
        seconds += 1
        centiseconds = 0
    if seconds >= 60:
        minutes += 1
        seconds = 0
    if minutes >= 60:
        hours += 1
        minutes = 0
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _subtitle_char_width_units(character: str) -> float:
    if not character:
        return 0.0
    if character.isspace():
        return 0.35
    if unicodedata.east_asian_width(character) in {"F", "W"}:
        return 1.0
    if character in ",.;:!?'\"`|/\\[](){}<>":
        return 0.38
    if character.isdigit():
        return 0.56
    return 0.62


def _subtitle_wrap_capacity_units(
    *,
    play_res_x: int,
    margin_x: int,
    font_size: int,
) -> float:
    available_width = max(180.0, float(play_res_x - (margin_x * 2) - 40))
    return max(8.0, available_width / max(10.0, float(font_size) * 0.92))


def _subtitle_tokens_for_wrapping(text: str) -> list[str]:
    tokens: list[str] = []
    current_word = ""
    for character in text:
        if character.isspace():
            if current_word:
                tokens.append(current_word)
                current_word = ""
            tokens.append(character)
            continue
        if character.isascii() and (character.isalnum() or character in {"'", "-", "_"}):
            current_word += character
            continue
        if current_word:
            tokens.append(current_word)
            current_word = ""
        tokens.append(character)
    if current_word:
        tokens.append(current_word)
    return tokens


def _break_subtitle_token(token: str, max_units: float) -> list[str]:
    if not token:
        return []
    parts: list[str] = []
    current = ""
    current_units = 0.0
    for character in token:
        char_units = _subtitle_char_width_units(character)
        if current and current_units + char_units > max_units:
            parts.append(current)
            current = character
            current_units = char_units
            continue
        current += character
        current_units += char_units
    if current:
        parts.append(current)
    return parts


def _wrap_subtitle_line(text: str, max_units: float) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""

    lines: list[str] = []
    current_parts: list[str] = []
    current_units = 0.0

    def flush_current() -> None:
        nonlocal current_parts, current_units
        line_text = "".join(current_parts).strip()
        if line_text:
            lines.append(line_text)
        current_parts = []
        current_units = 0.0

    for token in _subtitle_tokens_for_wrapping(normalized):
        if token.isspace():
            if current_parts:
                current_parts.append(" ")
                current_units += _subtitle_char_width_units(" ")
            continue

        token_units = sum(_subtitle_char_width_units(character) for character in token)
        candidate_units = current_units + token_units
        if current_parts and candidate_units > max_units:
            flush_current()

        if token_units > max_units:
            broken_parts = _break_subtitle_token(token, max_units)
            for index, broken_part in enumerate(broken_parts):
                part_units = sum(_subtitle_char_width_units(character) for character in broken_part)
                if current_parts and current_units + part_units > max_units:
                    flush_current()
                current_parts.append(broken_part)
                current_units += part_units
                if index < len(broken_parts) - 1:
                    flush_current()
            continue

        current_parts.append(token)
        current_units += token_units

    flush_current()
    return "\n".join(lines)


def _wrap_subtitle_text_for_ass(
    text: str,
    *,
    play_res_x: int,
    margin_x: int,
    font_size: int,
) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""

    max_units = _subtitle_wrap_capacity_units(
        play_res_x=play_res_x,
        margin_x=margin_x,
        font_size=font_size,
    )
    wrapped_lines: list[str] = []
    for raw_line in normalized.split("\n"):
        wrapped = _wrap_subtitle_line(raw_line, max_units)
        if wrapped:
            wrapped_lines.extend(line for line in wrapped.split("\n") if line.strip())
    return "\n".join(wrapped_lines)


def _escape_ass_text(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\\", r"\\")
    normalized = normalized.replace("{", r"\{").replace("}", r"\}")
    return normalized.replace("\n", r"\N")


def write_ass_subtitle_file(
    cues: list[SubtitleCueData],
    output_path: str | Path,
    *,
    settings: AppSettings,
    subtitle_language: str,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
) -> Path:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    play_res_x = max(1, int(play_res_x or 1920))
    play_res_y = max(1, int(play_res_y or 1080))
    font_size = _subtitle_font_size_for_canvas(
        int(getattr(settings.export, "subtitle_font_size", 28) or 28),
        canvas_height=play_res_y,
    )
    position_x, position_y = _subtitle_position_pixels(
        settings,
        subtitle_language,
        play_res_x=play_res_x,
        play_res_y=play_res_y,
    )
    margin_x = max(32, int(round(play_res_x * 0.08)))

    header_lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,"
        f"{font_size},{_subtitle_ass_color(settings, subtitle_language)},{_subtitle_ass_color(settings, subtitle_language)},"
        "&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.2,0,5,"
        f"{margin_x},{margin_x},24,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    event_lines = []
    for cue in cues:
        wrapped_text = _wrap_subtitle_text_for_ass(
            cue.text,
            play_res_x=play_res_x,
            margin_x=margin_x,
            font_size=font_size,
        )
        escaped_text = _escape_ass_text(wrapped_text)
        event_lines.append(
            "Dialogue: 0,"
            f"{_ms_to_ass_timestamp(cue.start_ms)},"
            f"{_ms_to_ass_timestamp(cue.end_ms)},"
            f"Default,,0,0,0,,{{\\an5\\pos({position_x},{position_y})}}{escaped_text}"
        )

    destination.write_text("\n".join(header_lines + event_lines) + "\n", encoding="utf-8")
    return destination


def _subtitle_filter_for_ass(ass_path: str | Path) -> str:
    return f"subtitles='{ffmpeg_filter_path(ass_path)}'"


def subtitle_filter_expression(
    settings: AppSettings,
    srt_path: str | Path,
    *,
    subtitle_language: str = "en",
    margin_v: int | None = None,
) -> str:
    filter_value = f"subtitles='{ffmpeg_filter_path(srt_path)}'"
    font_size = max(1, int(getattr(settings.export, "subtitle_font_size", 28) or 28))
    style_fields = [
        f"FontSize={font_size}",
        "Alignment=2",
        f"PrimaryColour={_subtitle_ass_color(settings, subtitle_language)}",
        "Outline=0",
        "Shadow=0",
        "BorderStyle=1",
        "OutlineColour=&H00000000",
        "BackColour=&H00000000",
    ]
    if margin_v is not None:
        style_fields.append(f"MarginV={max(0, int(margin_v))}")
    return f"{filter_value}:force_style='{','.join(style_fields)}'"


def bilingual_subtitle_filter_expression(
    settings: AppSettings,
    chinese_srt_path: str | Path,
    english_srt_path: str | Path,
) -> str:
    font_size = max(1, int(getattr(settings.export, "subtitle_font_size", 28) or 28))
    english_margin = max(18, int(round(font_size * 1.2)))
    chinese_margin = english_margin + max(font_size + 8, int(round(font_size * 1.6)))
    chinese_filter = subtitle_filter_expression(
        settings,
        chinese_srt_path,
        subtitle_language="zh",
        margin_v=chinese_margin,
    )
    english_filter = subtitle_filter_expression(
        settings,
        english_srt_path,
        subtitle_language="en",
        margin_v=english_margin,
    )
    return f"{chinese_filter},{english_filter}"


def render_video_with_subtitles(
    base_video_path: str | Path,
    output_path: str | Path,
    *,
    settings: AppSettings,
    container: str,
    burn_subtitles: bool,
    has_audio: bool,
    srt_path: str | Path,
    subtitle_mode: str = "en",
    chinese_srt_path: str | Path | None = None,
    english_srt_path: str | Path | None = None,
    controller: object | None = None,
) -> Path:
    destination = Path(output_path)
    if burn_subtitles:
        normalized_mode = str(subtitle_mode or "en").strip().lower()
        play_res_x = 1920
        play_res_y = 1080
        try:
            probe_result = probe_media(base_video_path, settings.media.ffmpeg_path)
            if probe_result.media_info.width > 0 and probe_result.media_info.height > 0:
                play_res_x = probe_result.media_info.width
                play_res_y = probe_result.media_info.height
        except Exception:
            pass
        with tempfile.TemporaryDirectory(prefix="vcut_ass_subtitles_") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            subtitle_filter = ""
            if (
                normalized_mode == "bilingual"
                and chinese_srt_path is not None
                and english_srt_path is not None
            ):
                chinese_ass_path = write_ass_subtitle_file(
                    read_subtitle_file(chinese_srt_path),
                    temp_dir / "subtitle_zh.ass",
                    settings=settings,
                    subtitle_language="zh",
                    play_res_x=play_res_x,
                    play_res_y=play_res_y,
                )
                english_ass_path = write_ass_subtitle_file(
                    read_subtitle_file(english_srt_path),
                    temp_dir / "subtitle_en.ass",
                    settings=settings,
                    subtitle_language="en",
                    play_res_x=play_res_x,
                    play_res_y=play_res_y,
                )
                subtitle_filter = ",".join(
                    [
                        _subtitle_filter_for_ass(chinese_ass_path),
                        _subtitle_filter_for_ass(english_ass_path),
                    ]
                )
            else:
                subtitle_language = "zh" if normalized_mode == "zh" else "en"
                ass_path = write_ass_subtitle_file(
                    read_subtitle_file(srt_path),
                    temp_dir / f"subtitle_{subtitle_language}.ass",
                    settings=settings,
                    subtitle_language=subtitle_language,
                    play_res_x=play_res_x,
                    play_res_y=play_res_y,
                )
                subtitle_filter = _subtitle_filter_for_ass(ass_path)
            command = [
                _effective_ffmpeg_path(settings),
                "-y" if settings.media.overwrite_existing else "-n",
                "-hide_banner",
                "-i",
                str(base_video_path),
                "-vf",
                subtitle_filter,
                "-c:v",
                settings.media.video_codec,
            ]
            if has_audio:
                command.extend(["-c:a", "copy"])
            command.append(str(destination))
            _run_command(command, "Subtitle export failed.", controller=controller)
    else:
        command = [
            _effective_ffmpeg_path(settings),
            "-y" if settings.media.overwrite_existing else "-n",
            "-hide_banner",
            "-i",
            str(base_video_path),
            "-i",
            str(srt_path),
            "-map",
            "0:v:0",
        ]
        if has_audio:
            command.extend(["-map", "0:a:0"])
        command.extend(
            [
                "-map",
                "1:0",
                "-c:v",
                "copy",
            ]
        )
        if has_audio:
            command.extend(["-c:a", "copy"])
        command.extend(["-c:s", subtitle_codec_for_container(container), str(destination)])
        _run_command(command, "Subtitle export failed.", controller=controller)
    return destination


def write_sidecar_srt(source_srt_path: str | Path, output_path: str | Path) -> Path:
    destination = Path(output_path)
    destination.write_text(Path(source_srt_path).read_text(encoding="utf-8"), encoding="utf-8")
    return destination


def render_video_with_audio_track(
    base_video_path: str | Path,
    audio_path: str | Path,
    output_path: str | Path,
    *,
    settings: AppSettings,
    controller: object | None = None,
) -> Path:
    destination = Path(output_path)
    command = [
        _effective_ffmpeg_path(settings),
        "-y" if settings.media.overwrite_existing else "-n",
        "-hide_banner",
        "-i",
        str(base_video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        settings.media.audio_codec,
        "-shortest",
        str(destination),
    ]
    _run_command(command, "English dub export failed.", controller=controller)
    return destination


def render_stretched_video(
    base_video_path: str | Path,
    settings: AppSettings,
    timeline_segments: list[TimelineSegment],
    output_path: str | Path,
    controller: object | None = None,
) -> Path:
    if not timeline_segments:
        raise ExportError("No video timeline is available for dub stretching.")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    filter_parts: list[str] = []
    concat_inputs: list[str] = []

    for index, segment in enumerate(timeline_segments):
        if segment.source_duration_ms <= 0:
            continue
        start = ms_to_seconds(segment.source_start_ms)
        end = ms_to_seconds(segment.source_end_ms)
        if segment.output_duration_ms != segment.source_duration_ms:
            stretch_factor = segment.output_duration_ms / max(1, segment.source_duration_ms)
            filter_parts.append(
                f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS,"
                f"setpts={stretch_factor:.6f}*PTS[v{index}]"
            )
        else:
            filter_parts.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{index}]")
        concat_inputs.append(f"[v{index}]")

    if not concat_inputs:
        raise ExportError("No video segments remain after applying dub stretch.")

    filter_parts.append(f"{''.join(concat_inputs)}concat=n={len(concat_inputs)}:v=1:a=0[outv]")

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".fftxt",
        encoding="utf-8",
        delete=False,
    ) as script_file:
        script_file.write(";\n".join(filter_parts))
        script_path = Path(script_file.name)

    command = [
        _effective_ffmpeg_path(settings),
        "-y" if settings.media.overwrite_existing else "-n",
        "-hide_banner",
        "-i",
        str(base_video_path),
        "-filter_complex_script",
        str(script_path),
        "-map",
        "[outv]",
        "-c:v",
        settings.media.video_codec,
        "-an",
        str(output_path),
    ]

    try:
        _run_command(command, "English video stretch export failed.", controller=controller)
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass
    return output_path


def build_export_plan(
    project: Project,
    settings: AppSettings,
    export_type: str,
    output_dir: str | Path,
    container: str,
) -> ExportPlan:
    if export_type not in {"clean_zh", "en_subtitle", "en_dub", "en_dub_subtitle"}:
        raise ExportError(f"Export type '{export_type}' is not implemented yet.")
    if not project.video_path:
        raise ExportError("There is no imported source video to export.")

    total_duration_ms = project.source_duration_ms or project.media_info.duration_ms
    if total_duration_ms <= 0:
        raise ExportError("Source duration is missing. Re-import the video and try again.")

    cut_ranges = accepted_cut_ranges(project.cut_suggestions, total_duration_ms)
    keep_ranges = keep_ranges_from_cuts(total_duration_ms, cut_ranges)
    output_path = choose_output_path(
        project_name=project.name,
        output_dir=output_dir,
        export_type=export_type,
        container=container,
        overwrite_existing=settings.media.overwrite_existing,
    )
    return ExportPlan(cut_ranges=cut_ranges, keep_ranges=keep_ranges, output_path=output_path)


def export_clean_video(
    project: Project,
    settings: AppSettings,
    output_dir: str | Path,
    container: str,
    controller: object | None = None,
    progress_callback: ProgressCallback = None,
) -> ExportPlan:
    _notify_progress(progress_callback, 10, "正在规划纯净导出...")
    plan = build_export_plan(
        project=project,
        settings=settings,
        export_type="clean_zh",
        output_dir=output_dir,
        container=container,
    )
    _notify_progress(progress_callback, 35, "正在按建议删除输出纯净视频...")
    render_keep_ranges_to_video(
        project,
        settings,
        plan.keep_ranges,
        plan.output_path,
        controller=controller,
    )
    _notify_progress(progress_callback, 100, "纯净视频导出完成。")
    return plan


def export_english_subtitle_video(
    project: Project,
    settings: AppSettings,
    output_dir: str | Path,
    container: str,
    burn_subtitles: bool,
    export_sidecar_srt: bool,
    subtitle_mode: str = "en",
    controller: object | None = None,
    progress_callback: ProgressCallback = None,
) -> ExportPlan:
    _notify_progress(progress_callback, 10, "正在规划字幕导出...")
    plan = build_export_plan(
        project=project,
        settings=settings,
        export_type="en_subtitle",
        output_dir=output_dir,
        container=container,
    )
    _controller_checkpoint(controller)
    _notify_progress(progress_callback, 20, "正在整理字幕内容...")
    subtitle_cues = build_subtitle_cues(project, plan.keep_ranges, subtitle_mode=subtitle_mode)
    if not subtitle_cues:
        raise ExportError("No English subtitle text is available. Add translated English text before exporting.")

    with tempfile.TemporaryDirectory(prefix="vcut_subtitle_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        srt_path = write_srt(subtitle_cues, temp_dir / "english_subtitles.srt")
        chinese_srt_path: Path | None = None
        english_srt_path: Path | None = None
        normalized_subtitle_mode = str(subtitle_mode or "en").strip().lower()
        if burn_subtitles and normalized_subtitle_mode == "bilingual":
            chinese_cues = build_subtitle_cues(project, plan.keep_ranges, subtitle_mode="zh")
            english_cues = build_subtitle_cues(project, plan.keep_ranges, subtitle_mode="en")
            if chinese_cues and english_cues:
                chinese_srt_path = write_srt(chinese_cues, temp_dir / "chinese_subtitles.srt")
                english_srt_path = write_srt(english_cues, temp_dir / "english_only_subtitles.srt")

        base_video_path = Path(project.video_path)
        if plan.cut_ranges:
            _notify_progress(progress_callback, 45, "正在应用建议删除到字幕视频...")
            base_video_path = temp_dir / f"clean_base.{container.lstrip('.') or 'mp4'}"
            render_keep_ranges_to_video(
                project,
                settings,
                plan.keep_ranges,
                base_video_path,
                controller=controller,
            )

        _notify_progress(progress_callback, 75, "正在封装字幕视频...")
        render_video_with_subtitles(
            base_video_path,
            plan.output_path,
            settings=settings,
            container=container,
            burn_subtitles=burn_subtitles,
            has_audio=project.media_info.has_audio,
            srt_path=srt_path,
            subtitle_mode=subtitle_mode,
            chinese_srt_path=chinese_srt_path,
            english_srt_path=english_srt_path,
            controller=controller,
        )

        if export_sidecar_srt:
            _controller_checkpoint(controller)
            _notify_progress(progress_callback, 90, "正在写出字幕文件...")
            write_sidecar_srt(srt_path, plan.output_path.with_suffix(".srt"))
    _notify_progress(progress_callback, 100, "字幕视频导出完成。")
    return plan


def wave_duration_ms(path: str | Path) -> int:
    with wave.open(str(path), "rb") as wav_file:
        frame_rate = wav_file.getframerate()
        frame_count = wav_file.getnframes()
    if frame_rate <= 0:
        return 0
    return int(round((frame_count / frame_rate) * 1000))


def build_atempo_filters(source_duration_ms: int, target_duration_ms: int) -> list[str]:
    if source_duration_ms <= 0 or target_duration_ms <= 0 or source_duration_ms <= target_duration_ms:
        return []

    speed_ratio = source_duration_ms / target_duration_ms
    filters: list[str] = []
    while speed_ratio > 2.0:
        filters.append("atempo=2.0")
        speed_ratio /= 2.0
    filters.append(f"atempo={speed_ratio:.5f}")
    filters.append(f"atrim=end={ms_to_seconds(target_duration_ms)}")
    return filters


def can_reuse_segment_dub(segment: object, voice_id: str, rate: float, text: str) -> Path | None:
    dub_audio_path = str(getattr(segment, "dub_audio_path", "") or "").strip()
    if not dub_audio_path:
        return None
    candidate = Path(dub_audio_path)
    if not candidate.exists():
        return None
    dub_voice_id = str(getattr(segment, "dub_voice_id", "") or "").strip()
    dub_text = str(getattr(segment, "dub_text", "") or "").strip()
    dub_rate = getattr(segment, "dub_rate", None)
    if dub_voice_id != voice_id:
        return None
    if dub_text != text.strip():
        return None
    if dub_rate is None or abs(float(dub_rate) - float(rate)) > 0.001:
        return None
    return candidate


def prepare_english_dub_inputs(
    project: Project,
    settings: AppSettings,
    keep_ranges: list[TimeRange],
    work_dir: str | Path,
    controller: object | None = None,
) -> list[DubAudioInput]:
    dub_cues = build_dub_cues(project, keep_ranges, settings.tts.default_voice)
    if not dub_cues:
        raise ExportError("No English translation text is available for dubbing.")

    _controller_checkpoint(controller)
    provider = create_tts_provider(settings.tts)
    temp_dir = Path(work_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    prepared_inputs: list[DubAudioInput] = []
    for cue in dub_cues:
        _controller_checkpoint(controller)
        segment = project.segments[cue.segment_index]
        clip_path = can_reuse_segment_dub(
            segment=segment,
            voice_id=cue.voice_id,
            rate=settings.tts.rate,
            text=cue.text,
        )
        if clip_path is None:
            clip_path = temp_dir / f"dub_{cue.index:04d}.wav"
            provider.synthesize_segment(
                cue.text,
                cue.voice_id,
                clip_path,
                controller=controller,
            )
        _controller_checkpoint(controller)
        prepared_inputs.append(
            DubAudioInput(
                cue=cue,
                clip_path=Path(clip_path),
                clip_duration_ms=wave_duration_ms(clip_path),
            )
        )
    return prepared_inputs


def render_english_dub_audio(
    input_specs: list[tuple[DubCue, Path, int]],
    settings: AppSettings,
    output_path: str | Path,
    total_duration_ms: int,
    controller: object | None = None,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if total_duration_ms <= 0:
        raise ExportError("No media remains after applying accepted cut suggestions.")

    if not input_specs:
        raise ExportError("No English translation text is available for dubbing.")

    synthesized_paths = [clip_path for _, clip_path, _ in input_specs]
    filter_parts = [
        f"[0:a]atrim=end={ms_to_seconds(total_duration_ms)},asetpts=N/SR/TB[base]"
    ]
    mix_inputs = ["[base]"]

    for input_index, (cue, _clip_path, clip_duration_ms) in enumerate(input_specs, start=1):
        clip_filters = build_atempo_filters(clip_duration_ms, cue.end_ms - cue.start_ms)
        clip_filters.append(f"adelay={cue.start_ms}|{cue.start_ms}")
        label = f"[dub{input_index}]"
        filter_parts.append(f"[{input_index}:a]{','.join(clip_filters)}{label}")
        mix_inputs.append(label)

    filter_parts.append(
        "".join(mix_inputs)
        + f"amix=inputs={len(mix_inputs)}:normalize=0,"
        + f"volume={settings.tts.volume:.3f},"
        + f"aresample={settings.tts.sample_rate},"
        + f"atrim=end={ms_to_seconds(total_duration_ms)},asetpts=N/SR/TB[aout]"
    )

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".fftxt",
        encoding="utf-8",
        delete=False,
    ) as script_file:
        script_file.write(";\n".join(filter_parts))
        script_path = Path(script_file.name)

    command = [
        _effective_ffmpeg_path(settings),
        "-y" if settings.media.overwrite_existing else "-n",
        "-hide_banner",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={settings.tts.sample_rate}:cl=mono:d={ms_to_seconds(total_duration_ms)}",
    ]
    for clip_path in synthesized_paths:
        command.extend(["-i", str(clip_path)])
    command.extend(
        [
            "-filter_complex_script",
            str(script_path),
            "-map",
            "[aout]",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )

    try:
        _run_command(command, "English dub audio rendering failed.", controller=controller)
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass
    return output_path


def export_english_dub_video(
    project: Project,
    settings: AppSettings,
    output_dir: str | Path,
    container: str,
    burn_subtitles: bool = False,
    export_sidecar_srt: bool = False,
    subtitle_mode: str = "en",
    export_type: str = "en_dub",
    controller: object | None = None,
    progress_callback: ProgressCallback = None,
) -> ExportPlan:
    if not project.media_info.has_video:
        raise ExportError("English dub export requires a source video stream.")

    include_subtitles = export_type == "en_dub_subtitle"
    _notify_progress(progress_callback, 10, "正在规划英文导出...")
    plan = build_export_plan(
        project=project,
        settings=settings,
        export_type=export_type,
        output_dir=output_dir,
        container=container,
    )

    with tempfile.TemporaryDirectory(prefix="vcut_dub_export_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        base_video_path = Path(project.video_path)
        if plan.cut_ranges:
            _notify_progress(progress_callback, 25, "正在应用建议删除到英文视频...")
            base_video_path = temp_dir / f"clean_base.{container.lstrip('.') or 'mp4'}"
            render_keep_ranges_to_video(
                project,
                settings,
                plan.keep_ranges,
                base_video_path,
                controller=controller,
            )

        edited_duration_ms = sum(item.duration_ms for item in plan.keep_ranges) or (
            project.source_duration_ms or project.media_info.duration_ms
        )
        _notify_progress(progress_callback, 45, "正在准备英文配音片段...")
        dub_inputs = prepare_english_dub_inputs(
            project=project,
            settings=settings,
            keep_ranges=plan.keep_ranges,
            work_dir=temp_dir / "dub_inputs",
            controller=controller,
        )
        video_timeline = build_dub_video_timeline(edited_duration_ms, dub_inputs)
        stretched_dub_cues = build_stretched_dub_cues(dub_inputs, video_timeline)
        if len(stretched_dub_cues) != len(dub_inputs):
            raise ExportError("Failed to retime one or more English dub segments after stretching the video.")
        stretched_duration_ms = video_timeline[-1].output_end_ms if video_timeline else edited_duration_ms

        if has_stretched_dub_video_timeline(video_timeline):
            _notify_progress(progress_callback, 58, "英文配音较长，正在自动拉长对应视频片段...")
            stretched_video_path = temp_dir / f"stretched_base.{container.lstrip('.') or 'mp4'}"
            render_stretched_video(
                base_video_path,
                settings,
                video_timeline,
                stretched_video_path,
                controller=controller,
            )
            base_video_path = stretched_video_path

        _notify_progress(progress_callback, 68, "正在生成英文配音音轨...")
        dubbed_audio_path = render_english_dub_audio(
            [
                (stretched_cue, dub_input.clip_path, dub_input.clip_duration_ms)
                for dub_input, stretched_cue in zip(dub_inputs, stretched_dub_cues)
            ],
            settings=settings,
            output_path=temp_dir / "english_dub.wav",
            total_duration_ms=stretched_duration_ms,
            controller=controller,
        )

        if include_subtitles:
            _notify_progress(progress_callback, 80, "正在合成英文配音视频...")
            dubbed_video_path = render_video_with_audio_track(
                base_video_path,
                dubbed_audio_path,
                temp_dir / f"english_dubbed_base.{container.lstrip('.') or 'mp4'}",
                settings=settings,
                controller=controller,
            )
            _notify_progress(progress_callback, 88, "正在生成导出字幕...")
            subtitle_cues = build_subtitle_cues_from_dub_cues(
                project,
                stretched_dub_cues,
                subtitle_mode=subtitle_mode,
            )
            if not subtitle_cues:
                raise ExportError("No English subtitle text is available. Add translated English text before exporting.")
            srt_path = write_srt(subtitle_cues, temp_dir / "english_dub_subtitles.srt")
            chinese_srt_path: Path | None = None
            english_srt_path: Path | None = None
            normalized_subtitle_mode = str(subtitle_mode or "en").strip().lower()
            if burn_subtitles and normalized_subtitle_mode == "bilingual":
                chinese_cues = build_subtitle_cues_from_dub_cues(project, stretched_dub_cues, subtitle_mode="zh")
                english_cues = build_subtitle_cues_from_dub_cues(project, stretched_dub_cues, subtitle_mode="en")
                if chinese_cues and english_cues:
                    chinese_srt_path = write_srt(chinese_cues, temp_dir / "chinese_dub_subtitles.srt")
                    english_srt_path = write_srt(english_cues, temp_dir / "english_dub_only_subtitles.srt")
            _notify_progress(progress_callback, 95, "正在封装英文配音字幕视频...")
            render_video_with_subtitles(
                dubbed_video_path,
                plan.output_path,
                settings=settings,
                container=container,
                burn_subtitles=burn_subtitles,
                has_audio=True,
                srt_path=srt_path,
                subtitle_mode=subtitle_mode,
                chinese_srt_path=chinese_srt_path,
                english_srt_path=english_srt_path,
                controller=controller,
            )
            if export_sidecar_srt:
                _notify_progress(progress_callback, 97, "正在写出字幕文件...")
                write_sidecar_srt(srt_path, plan.output_path.with_suffix(".srt"))
        else:
            _notify_progress(progress_callback, 88, "正在合成英文配音视频...")
            render_video_with_audio_track(
                base_video_path,
                dubbed_audio_path,
                plan.output_path,
                settings=settings,
                controller=controller,
            )
    _notify_progress(progress_callback, 100, "英文视频导出完成。")
    return plan
