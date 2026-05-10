from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .models import SlidePage
from .providers.translation import (
    TranslationProviderError,
    create_translation_provider,
    extract_json_object,
)
from .settings import TranslationSettings


def _truncate(value: str, max_chars: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(0, max_chars - 3)].rstrip() + "..."


def _deck_outline(slides: list[SlidePage]) -> list[dict[str, object]]:
    outline: list[dict[str, object]] = []
    for slide in slides:
        outline.append(
            {
                "slide_index": slide.slide_index,
                "title": _truncate(slide.title, 180),
                "source_text": _truncate(slide.source_text, 900),
                "notes_text": _truncate(slide.notes_text, 500),
            }
        )
    return outline


def _request_model_json(
    settings: TranslationSettings,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    provider = create_translation_provider(settings)
    request_model_text = getattr(provider, "_request_model_text", None)
    if not callable(request_model_text):
        raise TranslationProviderError("The current translation provider does not support custom PPT scripting prompts.")

    payload = {
        "model": settings.model,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    response_text = request_model_text(payload)
    parsed = extract_json_object(response_text)
    if not isinstance(parsed, dict):
        raise TranslationProviderError("The model did not return a JSON object for PPT scripting.")
    return parsed


def generate_deck_summary(
    settings: TranslationSettings,
    deck_name: str,
    slides: list[SlidePage],
) -> str:
    if not slides:
        return ""
    outline = _deck_outline(slides)
    system_prompt = (
        "You are an expert training-course presenter and instructional designer. "
        "Read the presentation structure and produce a concise Chinese explanation strategy."
    )
    user_prompt = (
        "Please analyze this presentation and return JSON only.\n"
        "Output schema:\n"
        '{"deck_summary":"一段简体中文总结，说明课程主题、讲解口吻和整体讲解策略"}\n'
        "Requirements:\n"
        "- Use simplified Chinese.\n"
        "- Keep it concise and useful for generating slide-by-slide speaking scripts.\n"
        "- Focus on the target audience, tone, and how the narration should connect slides.\n"
        f"Deck name: {deck_name}\n"
        f"Slides outline:\n{json.dumps(outline, ensure_ascii=False)}"
    )
    response = _request_model_json(settings, system_prompt, user_prompt)
    return str(response.get("deck_summary", "")).strip()


def generate_chinese_slide_scripts(
    settings: TranslationSettings,
    deck_name: str,
    all_slides: list[SlidePage],
    target_indices: list[int],
    *,
    deck_summary: str = "",
    batch_size: int = 6,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> list[tuple[int, str, int]]:
    if not target_indices:
        return []

    slide_by_index = {slide.slide_index: slide for slide in all_slides}
    deck_summary = deck_summary.strip() or generate_deck_summary(settings, deck_name, all_slides)
    results: list[tuple[int, str, int]] = []
    total_items = len(target_indices)
    completed_items = 0
    for batch_start in range(0, len(target_indices), max(1, batch_size)):
        batch_indices = target_indices[batch_start : batch_start + max(1, batch_size)]
        batch_payload = []
        for slide_index in batch_indices:
            slide = slide_by_index.get(slide_index)
            if slide is None:
                continue
            batch_payload.append(
                {
                    "slide_index": slide.slide_index,
                    "title": _truncate(slide.title, 220),
                    "source_text": _truncate(slide.source_text, 1800),
                    "notes_text": _truncate(slide.notes_text, 1200),
                }
            )
        if not batch_payload:
            continue

        system_prompt = (
            "You are a senior bilingual training presenter. "
            "Write natural Chinese spoken scripts for PPT training slides. "
            "Return JSON only."
        )
        user_prompt = (
            "Generate a Chinese narration draft for each slide.\n"
            "Output schema:\n"
            '{"slides":[{"slide_index":1,"zh_script":"...","estimated_duration_sec":18}]}\n'
            "Requirements:\n"
            "- Use simplified Chinese.\n"
            "- The script should sound like a trainer speaking to learners, not like literal translation.\n"
            "- Keep the script faithful to the slide content.\n"
            "- If notes_text exists, treat it as a strong hint.\n"
            "- Each slide should read naturally on its own and also connect to neighboring slides.\n"
            "- Avoid markdown and explanations.\n"
            f"Deck name: {deck_name}\n"
            f"Deck speaking strategy: {deck_summary}\n"
            f"Target slides:\n{json.dumps(batch_payload, ensure_ascii=False)}"
        )
        response = _request_model_json(settings, system_prompt, user_prompt)
        items = response.get("slides", [])
        if not isinstance(items, list):
            raise TranslationProviderError("The PPT script response did not contain a usable slides array.")

        item_by_index: dict[int, tuple[str, int]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                slide_index = int(item.get("slide_index", 0))
            except (TypeError, ValueError):
                continue
            zh_script = str(item.get("zh_script", "")).strip()
            estimated_duration_sec = max(1, int(float(item.get("estimated_duration_sec", 15))))
            if zh_script:
                item_by_index[slide_index] = (zh_script, estimated_duration_sec * 1000)

        missing = [slide_index for slide_index in batch_indices if slide_index not in item_by_index]
        if missing:
            raise TranslationProviderError(
                "The PPT Chinese draft response was missing slides: "
                + ", ".join(str(item) for item in missing)
            )

        for slide_index in batch_indices:
            zh_script, estimated_duration_ms = item_by_index[slide_index]
            results.append((slide_index, zh_script, estimated_duration_ms))
            completed_items += 1
            if progress_callback is not None:
                progress_callback(completed_items, total_items, slide_index)
    return results


def _extract_english_script_items(
    response: dict[str, Any],
    batch_payload: list[dict[str, object]],
) -> dict[int, str]:
    item_by_index: dict[int, str] = {}
    items = response.get("slides", [])

    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                slide_index = int(item.get("slide_index", 0))
            except (TypeError, ValueError):
                continue
            en_script = str(item.get("en_script", "")).strip()
            if en_script:
                item_by_index[slide_index] = en_script
    elif isinstance(items, dict):
        for raw_key, raw_value in items.items():
            try:
                slide_index = int(raw_key)
            except (TypeError, ValueError):
                continue
            en_script = str(raw_value or "").strip()
            if en_script:
                item_by_index[slide_index] = en_script

    if len(batch_payload) == 1 and not item_by_index:
        fallback_slide_index = int(batch_payload[0].get("slide_index", 0) or 0)
        fallback_candidates: list[dict[str, Any]] = [response]
        nested_slide = response.get("slide")
        if isinstance(nested_slide, dict):
            fallback_candidates.append(nested_slide)
        for candidate in fallback_candidates:
            try:
                slide_index = int(candidate.get("slide_index", fallback_slide_index) or fallback_slide_index)
            except (TypeError, ValueError):
                slide_index = fallback_slide_index
            for key in ("en_script", "translated_text", "translation", "text"):
                en_script = str(candidate.get(key, "")).strip()
                if en_script:
                    item_by_index[slide_index] = en_script
                    break
            if item_by_index:
                break
    return item_by_index


def _retry_translate_single_slide(
    settings: TranslationSettings,
    deck_name: str,
    deck_summary: str,
    slide: SlidePage,
) -> str:
    system_prompt = (
        "You are a professional training-course translator. "
        "Translate one Chinese speaking script into natural spoken English. "
        "Return JSON only."
    )
    user_prompt = (
        "Translate this one Chinese narration script into English.\n"
        "Output schema:\n"
        '{"slide_index":3,"en_script":"..."}\n'
        "You may also return {\"en_script\":\"...\"} if there is only one slide.\n"
        "Requirements:\n"
        "- Use natural spoken English for a trainer or narrator.\n"
        "- Preserve the meaning and teaching intent.\n"
        "- Keep terminology consistent with the whole deck.\n"
        "- Do not add markdown or explanations.\n"
        f"Deck name: {deck_name}\n"
        f"Deck speaking strategy: {deck_summary}\n"
        f"Target slide:\n{json.dumps([{'slide_index': slide.slide_index, 'title': _truncate(slide.title, 220), 'zh_script': _truncate(slide.zh_script.strip(), 2000)}], ensure_ascii=False)}"
    )
    response = _request_model_json(settings, system_prompt, user_prompt)
    item_by_index = _extract_english_script_items(
        response,
        [
            {
                "slide_index": slide.slide_index,
                "title": _truncate(slide.title, 220),
                "zh_script": _truncate(slide.zh_script.strip(), 2000),
            }
        ],
    )
    en_script = item_by_index.get(slide.slide_index, "").strip()
    if not en_script:
        raise TranslationProviderError(f"The PPT English translation response was missing slides: {slide.slide_index}")
    return en_script


def translate_chinese_scripts_to_english(
    settings: TranslationSettings,
    deck_name: str,
    all_slides: list[SlidePage],
    target_indices: list[int],
    *,
    deck_summary: str = "",
    batch_size: int = 8,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> list[tuple[int, str]]:
    if not target_indices:
        return []

    slide_by_index = {slide.slide_index: slide for slide in all_slides}
    deck_summary = deck_summary.strip() or generate_deck_summary(settings, deck_name, all_slides)
    results: list[tuple[int, str]] = []
    total_items = len(target_indices)
    completed_items = 0
    for batch_start in range(0, len(target_indices), max(1, batch_size)):
        batch_indices = target_indices[batch_start : batch_start + max(1, batch_size)]
        batch_payload = []
        for slide_index in batch_indices:
            slide = slide_by_index.get(slide_index)
            if slide is None:
                continue
            zh_script = slide.zh_script.strip()
            if not zh_script:
                continue
            batch_payload.append(
                {
                    "slide_index": slide.slide_index,
                    "title": _truncate(slide.title, 220),
                    "zh_script": _truncate(zh_script, 2000),
                }
            )
        if not batch_payload:
            continue

        system_prompt = (
            "You are a professional training-course translator. "
            "Translate Chinese speaking scripts into natural spoken English. "
            "Return JSON only."
        )
        user_prompt = (
            "Translate each Chinese narration script into English.\n"
            "Output schema:\n"
            '{"slides":[{"slide_index":1,"en_script":"..."}]}\n'
            "Requirements:\n"
            "- Use natural spoken English for a trainer or narrator.\n"
            "- Preserve the meaning and teaching intent.\n"
            "- Keep terminology consistent across slides.\n"
            "- Do not add markdown or explanations.\n"
            f"Deck name: {deck_name}\n"
            f"Deck speaking strategy: {deck_summary}\n"
            f"Target slides:\n{json.dumps(batch_payload, ensure_ascii=False)}"
        )
        response = _request_model_json(settings, system_prompt, user_prompt)
        item_by_index = _extract_english_script_items(response, batch_payload)

        missing = [
            slide_index
            for slide_index in batch_indices
            if slide_by_index.get(slide_index) is not None
            and slide_by_index[slide_index].zh_script.strip()
            and slide_index not in item_by_index
        ]
        if missing:
            if len(missing) == 1:
                missing_slide = slide_by_index.get(missing[0])
                if missing_slide is not None and missing_slide.zh_script.strip():
                    recovered_script = _retry_translate_single_slide(
                        settings,
                        deck_name,
                        deck_summary,
                        missing_slide,
                    )
                    if recovered_script.strip():
                        item_by_index[missing[0]] = recovered_script.strip()
                missing = [
                    slide_index
                    for slide_index in batch_indices
                    if slide_by_index.get(slide_index) is not None
                    and slide_by_index[slide_index].zh_script.strip()
                    and slide_index not in item_by_index
                ]
            elif len(missing) <= len(batch_indices):
                recovered_items = translate_chinese_scripts_to_english(
                    settings,
                    deck_name,
                    all_slides,
                    missing,
                    deck_summary=deck_summary,
                    batch_size=max(1, min(len(missing), max(1, batch_size // 2))),
                    progress_callback=None,
                )
                for recovered_slide_index, recovered_script in recovered_items:
                    item_by_index[recovered_slide_index] = recovered_script
                missing = [
                    slide_index
                    for slide_index in batch_indices
                    if slide_by_index.get(slide_index) is not None
                    and slide_by_index[slide_index].zh_script.strip()
                    and slide_index not in item_by_index
                ]
            if missing:
                raise TranslationProviderError(
                    "The PPT English translation response was missing slides: "
                    + ", ".join(str(item) for item in missing)
                )

        for slide_index in batch_indices:
            en_script = item_by_index.get(slide_index)
            if en_script:
                results.append((slide_index, en_script))
                completed_items += 1
                if progress_callback is not None:
                    progress_callback(completed_items, total_items, slide_index)
    return results
