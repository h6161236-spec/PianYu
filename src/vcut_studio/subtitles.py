from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


TIMESTAMP_RE = re.compile(
    r"^(?P<hours>\d{1,2}):(?P<minutes>\d{2}):(?P<seconds>\d{2})[,.](?P<milliseconds>\d{1,3})$"
)


@dataclass(frozen=True, slots=True)
class SubtitleCueData:
    index: int
    start_ms: int
    end_ms: int
    text: str


def parse_timestamp(value: str) -> int:
    match = TIMESTAMP_RE.match(value.strip())
    if match is None:
        raise ValueError(f"无法解析字幕时间戳：{value}")
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    milliseconds = int(match.group("milliseconds").ljust(3, "0"))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def parse_subtitle_text(text: str) -> list[SubtitleCueData]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()
    if not normalized:
        return []

    cues: list[SubtitleCueData] = []
    blocks = re.split(r"\n\s*\n", normalized)
    next_index = 1

    for block in blocks:
        raw_lines = [line.rstrip() for line in block.split("\n")]
        lines = [line for line in raw_lines if line.strip()]
        if not lines:
            continue

        first_line = lines[0].strip()
        upper_first = first_line.upper()
        if upper_first.startswith("NOTE") or upper_first.startswith("STYLE") or upper_first.startswith("REGION"):
            continue

        time_line_index = 0
        if "-->" not in lines[0]:
            if len(lines) < 2 or "-->" not in lines[1]:
                if upper_first == "WEBVTT":
                    continue
                continue
            time_line_index = 1

        if lines[time_line_index].strip().upper() == "WEBVTT":
            continue

        start_text, end_text = [part.strip() for part in lines[time_line_index].split("-->", 1)]
        end_text = end_text.split()[0]
        start_ms = parse_timestamp(start_text)
        end_ms = parse_timestamp(end_text)
        if end_ms <= start_ms:
            continue

        text_lines = [line.strip() for line in lines[time_line_index + 1 :] if line.strip()]
        if not text_lines:
            continue

        cues.append(
            SubtitleCueData(
                index=next_index,
                start_ms=start_ms,
                end_ms=end_ms,
                text="\n".join(text_lines),
            )
        )
        next_index += 1

    return cues


def read_subtitle_file(path: str | Path) -> list[SubtitleCueData]:
    source = Path(path)
    last_error: UnicodeDecodeError | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = source.read_text(encoding=encoding)
            return parse_subtitle_text(text)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return []


def _format_timestamp(value_ms: int, *, millisecond_separator: str) -> str:
    total_ms = max(0, int(value_ms))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{millisecond_separator}{milliseconds:03d}"


def write_subtitle_file(cues: list[SubtitleCueData], path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = destination.suffix.lower()

    if suffix == ".vtt":
        lines = ["WEBVTT", ""]
        for cue in cues:
            lines.extend(
                [
                    str(cue.index),
                    f"{_format_timestamp(cue.start_ms, millisecond_separator='.')} --> "
                    f"{_format_timestamp(cue.end_ms, millisecond_separator='.')}",
                    cue.text,
                    "",
                ]
            )
    else:
        lines = []
        for cue in cues:
            lines.extend(
                [
                    str(cue.index),
                    f"{_format_timestamp(cue.start_ms, millisecond_separator=',')} --> "
                    f"{_format_timestamp(cue.end_ms, millisecond_separator=',')}",
                    cue.text,
                    "",
                ]
            )

    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination
