from __future__ import annotations

from collections import Counter
from typing import Callable

from ..exporting import can_reuse_segment_dub
from ..settings import AppSettings


def segment_target_voice_id(segment: object, settings: AppSettings) -> str:
    return str(getattr(segment, "voice_id", None) or settings.tts.default_voice or "").strip()


def apply_voice_to_segments(
    segments: list[object],
    row_indices: list[int],
    voice_id: str,
    invalidate_segment_dub: Callable[[object], None] | None = None,
) -> int:
    normalized_voice_id = voice_id.strip()
    if not normalized_voice_id:
        return 0

    updated_count = 0
    for row_index in row_indices:
        if not (0 <= row_index < len(segments)):
            continue
        segment = segments[row_index]
        if str(getattr(segment, "voice_id", None) or "").strip() == normalized_voice_id:
            continue
        setattr(segment, "voice_id", normalized_voice_id)
        if invalidate_segment_dub is not None:
            invalidate_segment_dub(segment)
        updated_count += 1
    return updated_count


def dub_review_status(segment: object, settings: AppSettings) -> str:
    internal_status = str(getattr(segment, "tts_status", "") or "").strip().lower()
    if internal_status == "generating":
        return "generating"

    english_text = str(getattr(segment, "en_text", "") or "").strip()
    if not english_text:
        return "missing_text"

    voice_id = segment_target_voice_id(segment, settings)
    if voice_id and can_reuse_segment_dub(segment, voice_id=voice_id, rate=settings.tts.rate, text=english_text):
        return "reusable"

    if str(getattr(segment, "dub_audio_path", "") or "").strip():
        return "stale"
    if internal_status == "failed":
        return "failed"
    return "pending"


def dub_review_status_label(status: str) -> str:
    status_map = {
        "pending": "待生成",
        "reusable": "可复用",
        "stale": "缓存过期",
        "missing_text": "缺少英文",
        "generating": "生成中",
        "failed": "失败",
    }
    return status_map.get(status, status)


def build_dubbing_summary_text(project: object, settings: AppSettings) -> str:
    segments = list(getattr(project, "segments", []) or [])
    if not segments:
        return "当前还没有可配音片段。先执行 ASR 和翻译，再在这里确认英文配音。"

    counts = Counter(dub_review_status(segment, settings) for segment in segments)
    summary_parts = [f"共 {len(segments)} 个片段"]
    for status, label in [
        ("reusable", "可直接复用"),
        ("stale", "缓存过期"),
        ("pending", "待生成"),
        ("missing_text", "缺少英文"),
        ("generating", "生成中"),
        ("failed", "失败"),
    ]:
        count = counts.get(status, 0)
        if count:
            summary_parts.append(f"{count} 个{label}")

    return (
        " | ".join(summary_parts)
        + "。双击已生成的行可试听，试听音色时会优先使用当前选中片段的英文文本。"
    )


def pick_voice_preview_text(
    segments: list[object],
    selected_rows: list[int],
    fallback_text: str,
) -> tuple[str, str]:
    for row_index in selected_rows:
        if 0 <= row_index < len(segments):
            english_text = str(getattr(segments[row_index], "en_text", "") or "").strip()
            if english_text:
                return english_text, f"将使用第 {row_index + 1} 个片段的英文文本试听。"

    normalized_fallback = fallback_text.strip()
    if normalized_fallback:
        return normalized_fallback, "未选中可试听片段，已改用设置中的试听文本。"

    raise ValueError("没有可用于试听的英文文本，请先翻译片段，或在设置里填写试听文本。")
