from __future__ import annotations

import re
from uuid import uuid4

from .models import CutSuggestion, Segment, WordTiming
from .settings import CuttingSettings

_TOKEN_CLEAN_RE = re.compile(r"[\s\u3000,.;:!?，。！？；：“”\"'、\-\(\)\[\]\{\}]")
_CONSERVATIVE_FILLERS = {"嗯", "啊", "呃"}
_MEANINGFUL_FILLERS = {"这个", "那个", "就是", "然后"}


def normalize_token(text: str) -> str:
    return _TOKEN_CLEAN_RE.sub("", text).strip().lower()


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))


def _filler_score(normalized_word: str, settings: CuttingSettings) -> float:
    mode_bonus = {
        "conservative": -0.08,
        "standard": 0.0,
        "aggressive": 0.08,
    }.get(settings.mode, 0.0)
    base = 0.92 if normalized_word in _CONSERVATIVE_FILLERS else 0.76
    if normalized_word in _MEANINGFUL_FILLERS and settings.mode == "conservative":
        base -= 0.06
    return max(0.1, min(0.99, base + mode_bonus))


def _pause_score(gap_ms: int, settings: CuttingSettings) -> float:
    ratio = gap_ms / max(1, settings.pause_threshold_ms)
    return max(0.1, min(0.99, 0.55 + min(0.35, (ratio - 1.0) * 0.18)))


def make_filler_suggestion(
    segment: Segment,
    word: WordTiming,
    normalized_word: str,
    settings: CuttingSettings,
) -> CutSuggestion:
    start_ms = _clamp(word.start_ms - settings.cut_padding_ms, segment.start_ms, segment.end_ms)
    end_ms = _clamp(word.end_ms + settings.cut_padding_ms, segment.start_ms, segment.end_ms)
    if end_ms <= start_ms:
        end_ms = min(segment.end_ms, start_ms + max(50, word.end_ms - word.start_ms))
    return CutSuggestion(
        suggestion_id=uuid4().hex,
        start_ms=start_ms,
        end_ms=end_ms,
        reason="filler_word",
        score=_filler_score(normalized_word, settings),
        accepted=False,
        details=word.text.strip(),
    )


def make_pause_suggestion(
    start_ms: int,
    end_ms: int,
    gap_ms: int,
    settings: CuttingSettings,
    details: str,
) -> CutSuggestion | None:
    trim_margin = min(settings.cut_padding_ms, max(0, gap_ms // 4))
    trimmed_start = start_ms + trim_margin
    trimmed_end = end_ms - trim_margin
    if trimmed_end <= trimmed_start:
        trimmed_start = start_ms
        trimmed_end = end_ms
    if trimmed_end <= trimmed_start:
        return None
    return CutSuggestion(
        suggestion_id=uuid4().hex,
        start_ms=trimmed_start,
        end_ms=trimmed_end,
        reason="long_pause",
        score=_pause_score(gap_ms, settings),
        accepted=False,
        details=details,
    )


def make_silence_suggestion(
    start_ms: int,
    end_ms: int,
    gap_ms: int,
    settings: CuttingSettings,
    details: str,
) -> CutSuggestion | None:
    trim_margin = min(settings.cut_padding_ms, max(0, gap_ms // 4))
    trimmed_start = start_ms + trim_margin
    trimmed_end = end_ms - trim_margin
    if trimmed_end <= trimmed_start:
        trimmed_start = start_ms
        trimmed_end = end_ms
    if trimmed_end <= trimmed_start:
        return None
    return CutSuggestion(
        suggestion_id=uuid4().hex,
        start_ms=trimmed_start,
        end_ms=trimmed_end,
        reason="silent_range",
        score=min(0.99, _pause_score(gap_ms, settings) + 0.05),
        accepted=False,
        details=details,
    )


def merge_adjacent_suggestions(
    suggestions: list[CutSuggestion],
    merge_gap_ms: int,
) -> list[CutSuggestion]:
    if not suggestions:
        return []
    sorted_items = sorted(suggestions, key=lambda item: (item.start_ms, item.end_ms))
    merged: list[CutSuggestion] = []

    for suggestion in sorted_items:
        if not merged:
            merged.append(suggestion)
            continue
        previous = merged[-1]
        if suggestion.reason != previous.reason:
            merged.append(suggestion)
            continue
        if suggestion.start_ms - previous.end_ms > merge_gap_ms:
            merged.append(suggestion)
            continue
        previous.end_ms = max(previous.end_ms, suggestion.end_ms)
        previous.score = max(previous.score, suggestion.score)
        details = [previous.details.strip(), suggestion.details.strip()]
        previous.details = " | ".join(part for part in details if part)
    return merged


def _inter_word_gaps(segment: Segment) -> list[tuple[int, int, int, str]]:
    if len(segment.words) < 2:
        return []
    gaps: list[tuple[int, int, int, str]] = []
    for left, right in zip(segment.words, segment.words[1:]):
        gap_ms = right.start_ms - left.end_ms
        if gap_ms > 0:
            details = f"{left.text.strip()} -> {right.text.strip()}"
            gaps.append((left.end_ms, right.start_ms, gap_ms, details))
    return gaps


def _segment_excerpt(segment: Segment) -> str:
    text = segment.zh_text.strip()
    if text:
        return text[:12]
    return segment.segment_id[:8]


def _timeline_silence_gaps(
    segments: list[Segment],
    total_duration_ms: int,
    *,
    preserve_intro_pause: bool,
    long_intro_threshold_ms: int,
) -> list[tuple[int, int, int, str]]:
    ordered_segments = sorted(segments, key=lambda item: item.start_ms)
    safe_total_duration_ms = max(0, int(total_duration_ms))
    gaps: list[tuple[int, int, int, str]] = []

    if not ordered_segments:
        if safe_total_duration_ms > 0:
            gaps.append((0, safe_total_duration_ms, safe_total_duration_ms, "整段未检测到语音"))
        return gaps

    first_segment = ordered_segments[0]
    include_intro_gap = (
        first_segment.start_ms > 0
        and (
            not preserve_intro_pause
            or first_segment.start_ms >= max(0, int(long_intro_threshold_ms))
        )
    )
    if include_intro_gap:
        gaps.append(
            (
                0,
                first_segment.start_ms,
                first_segment.start_ms,
                f"片头 -> {_segment_excerpt(first_segment)}",
            )
        )

    for left, right in zip(ordered_segments, ordered_segments[1:]):
        gap_ms = right.start_ms - left.end_ms
        if gap_ms > 0:
            details = f"{_segment_excerpt(left)} -> {_segment_excerpt(right)}"
            gaps.append((left.end_ms, right.start_ms, gap_ms, details))

    last_segment = ordered_segments[-1]
    if safe_total_duration_ms > last_segment.end_ms:
        gap_ms = safe_total_duration_ms - last_segment.end_ms
        gaps.append(
            (
                last_segment.end_ms,
                safe_total_duration_ms,
                gap_ms,
                f"{_segment_excerpt(last_segment)} -> 片尾",
            )
        )
    return gaps


def generate_cut_suggestions(
    segments: list[Segment],
    settings: CuttingSettings,
    *,
    total_duration_ms: int = 0,
) -> list[CutSuggestion]:
    filler_terms = {normalize_token(word) for word in settings.filler_words if normalize_token(word)}
    suggestions: list[CutSuggestion] = []

    for segment in segments:
        for word in segment.words:
            normalized = normalize_token(word.text)
            if normalized and normalized in filler_terms:
                suggestions.append(make_filler_suggestion(segment, word, normalized, settings))

        for gap_start, gap_end, gap_ms, details in _inter_word_gaps(segment):
            if gap_ms >= settings.pause_threshold_ms:
                pause_suggestion = make_pause_suggestion(gap_start, gap_end, gap_ms, settings, details)
                if pause_suggestion is not None:
                    suggestions.append(pause_suggestion)

    for gap_start, gap_end, gap_ms, details in _timeline_silence_gaps(
        segments,
        total_duration_ms,
        preserve_intro_pause=settings.preserve_intro_pause,
        long_intro_threshold_ms=max(settings.pause_threshold_ms * 3, 2000),
    ):
        if gap_ms >= settings.pause_threshold_ms:
            silence_suggestion = make_silence_suggestion(gap_start, gap_end, gap_ms, settings, details)
            if silence_suggestion is not None:
                suggestions.append(silence_suggestion)

    return merge_adjacent_suggestions(suggestions, settings.merge_gap_ms)
