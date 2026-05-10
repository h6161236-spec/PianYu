from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .exporting import (
    ExportError,
    ExportPlan,
    SubtitleCue,
    _controller_checkpoint,
    _effective_ffmpeg_path,
    _notify_progress,
    _run_command,
    choose_output_path,
    ms_to_seconds,
    render_video_with_subtitles,
    subtitle_filter_expression,
    wave_duration_ms,
    write_sidecar_srt,
    write_srt,
)
from .models import Project, SlidePage
from .project_store import sanitize_filename
from .providers.tts import create_tts_provider
from .settings import AppSettings

ProgressCallback = Callable[[int, str], None] | None


@dataclass(frozen=True, slots=True)
class PptSlideAudio:
    slide_index: int
    audio_path: Path
    duration_ms: int


def _normalize_voiceover_language(value: str | None) -> str:
    return "zh" if str(value or "").strip().lower() == "zh" else "en"


def _normalize_subtitle_mode(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"zh", "en", "bilingual"}:
        return normalized
    return "bilingual"


def _slide_script_text(slide: SlidePage, language: str) -> str:
    return slide.zh_script.strip() if language == "zh" else slide.en_script.strip()


def _project_has_any_script(project: Project, language: str) -> bool:
    return any(_slide_script_text(slide, language) for slide in project.slides)


def _project_has_complete_script(project: Project, language: str) -> bool:
    slides = list(project.slides)
    return bool(slides) and all(_slide_script_text(slide, language) for slide in slides)


def resolve_ppt_voiceover_language(project: Project, requested_language: str | None) -> str:
    normalized_language = _normalize_voiceover_language(requested_language)
    if normalized_language == "en":
        has_complete_english = _project_has_complete_script(project, "en")
        has_complete_chinese = _project_has_complete_script(project, "zh")
        if not has_complete_english and has_complete_chinese:
            return "zh"
    return normalized_language


def resolve_ppt_subtitle_mode(project: Project, requested_mode: str | None) -> str:
    normalized_mode = _normalize_subtitle_mode(requested_mode)
    if normalized_mode == "zh":
        return "zh"
    if normalized_mode == "en":
        if _project_has_complete_script(project, "en"):
            return "en"
        if _project_has_any_script(project, "zh"):
            return "zh"
        return "en"
    if _project_has_any_script(project, "en") and _project_has_any_script(project, "zh"):
        return "bilingual"
    if _project_has_any_script(project, "zh"):
        return "zh"
    if _project_has_any_script(project, "en"):
        return "en"
    return normalized_mode


def _ppt_subtitle_text(slide: SlidePage, subtitle_mode: str) -> str:
    normalized_mode = _normalize_subtitle_mode(subtitle_mode)
    zh_text = slide.zh_script.strip()
    en_text = slide.en_script.strip()
    if normalized_mode == "bilingual":
        return "\n".join(line for line in (zh_text, en_text) if line)
    if normalized_mode == "zh":
        return zh_text
    return en_text


_SUBTITLE_SENTENCE_PATTERN = re.compile(r"[^,，.。!！?？;；:：\n]+(?:[,，.。!！?？;；:：]+|$)")


def _split_subtitle_sentences(text: str) -> list[str]:
    normalized = str(text or "").strip()
    if not normalized:
        return []
    parts = [part.strip() for part in _SUBTITLE_SENTENCE_PATTERN.findall(normalized) if part.strip()]
    if parts:
        return parts
    return [normalized]


def _chunk_weight(text: str) -> int:
    normalized = str(text or "").strip()
    if not normalized:
        return 1
    return max(1, len(re.sub(r"\s+", "", normalized)))


def _slide_subtitle_chunks(slide: SlidePage, subtitle_mode: str) -> list[str]:
    normalized_mode = _normalize_subtitle_mode(subtitle_mode)
    zh_chunks = _split_subtitle_sentences(slide.zh_script)
    en_chunks = _split_subtitle_sentences(slide.en_script)

    if normalized_mode == "zh":
        return zh_chunks
    if normalized_mode == "en":
        return en_chunks

    total = max(len(zh_chunks), len(en_chunks))
    bilingual_chunks: list[str] = []
    for index in range(total):
        parts: list[str] = []
        if index < len(zh_chunks):
            parts.append(zh_chunks[index])
        if index < len(en_chunks):
            parts.append(en_chunks[index])
        text = "\n".join(part for part in parts if part.strip()).strip()
        if text:
            bilingual_chunks.append(text)
    return bilingual_chunks


def _build_subtitle_cues(
    project: Project,
    audio_items: list[PptSlideAudio],
    *,
    subtitle_mode: str,
) -> list[SubtitleCue]:
    cues: list[SubtitleCue] = []
    offset_ms = 0
    slide_by_index = {slide.slide_index: slide for slide in project.slides}
    cue_index = 1
    for audio_item in audio_items:
        slide = slide_by_index.get(audio_item.slide_index)
        if slide is None:
            continue
        chunks = _slide_subtitle_chunks(slide, subtitle_mode)
        if not chunks:
            offset_ms += audio_item.duration_ms
            continue

        if len(chunks) == 1:
            cues.append(
                SubtitleCue(
                    index=cue_index,
                    start_ms=offset_ms,
                    end_ms=offset_ms + audio_item.duration_ms,
                    text=chunks[0],
                )
            )
            cue_index += 1
            offset_ms += audio_item.duration_ms
            continue

        weights = [_chunk_weight(chunk) for chunk in chunks]
        total_weight = max(1, sum(weights))
        chunk_start = offset_ms
        remaining_ms = audio_item.duration_ms
        min_chunk_ms = 120 if audio_item.duration_ms < 450 * len(chunks) else 450
        for chunk_number, chunk in enumerate(chunks, start=1):
            is_last_chunk = chunk_number == len(chunks)
            if is_last_chunk:
                chunk_duration = remaining_ms
            else:
                remaining_chunk_count = len(chunks) - chunk_number
                chunk_duration = max(
                    min_chunk_ms,
                    int(audio_item.duration_ms * (_chunk_weight(chunk) / total_weight)),
                )
                max_allowed = max(
                    min_chunk_ms,
                    remaining_ms - (min_chunk_ms * remaining_chunk_count),
                )
                chunk_duration = min(chunk_duration, max_allowed)
            chunk_end = chunk_start + chunk_duration
            cues.append(
                SubtitleCue(
                    index=cue_index,
                    start_ms=chunk_start,
                    end_ms=chunk_end,
                    text=chunk,
                )
            )
            cue_index += 1
            remaining_ms = max(0, offset_ms + audio_item.duration_ms - chunk_end)
            chunk_start = chunk_end
        offset_ms += audio_item.duration_ms
    return cues


def _synthesize_slide_audios(
    project: Project,
    settings: AppSettings,
    *,
    voiceover_language: str = "en",
    selected_voice_id: str | None = None,
    controller: object | None = None,
    progress_callback: ProgressCallback = None,
) -> list[PptSlideAudio]:
    provider = create_tts_provider(settings.tts)
    dub_dir = (
        Path(settings.workspace.workspace_dir)
        / "ppt_dubs"
        / sanitize_filename(project.name or "presentation")
        / sanitize_filename(project.project_id)
    )
    dub_dir.mkdir(parents=True, exist_ok=True)

    normalized_language = resolve_ppt_voiceover_language(project, voiceover_language)
    is_chinese_voiceover = normalized_language == "zh"
    language_label = "中文" if is_chinese_voiceover else "英文"
    missing_scripts = [
        slide.slide_index
        for slide in project.slides
        if not (slide.zh_script.strip() if is_chinese_voiceover else slide.en_script.strip())
    ]
    if missing_scripts:
        preview = ", ".join(str(item) for item in missing_scripts[:12])
        raise ExportError(
            ("Some PPT slides are missing Chinese speaking scripts: " if is_chinese_voiceover else "Some PPT slides are missing English speaking scripts: ")
            + preview
            + (" ..." if len(missing_scripts) > 12 else "")
        )

    target_slides = list(project.slides)

    audio_items: list[PptSlideAudio] = []
    total = len(target_slides)
    for item_index, slide in enumerate(target_slides, start=1):
        _controller_checkpoint(controller)
        output_path = dub_dir / f"slide_{slide.slide_index:03d}_{normalized_language or 'en'}.wav"
        script_text = _slide_script_text(slide, normalized_language)
        voice_id = (
            selected_voice_id
            or slide.voice_id
            or settings.tts.default_voice_for_language(normalized_language)
        )
        _notify_progress(
            progress_callback,
            10 + int((item_index - 1) * 30 / max(1, total)),
            f"正在生成第 {item_index}/{total} 页的{language_label}配音...",
        )
        provider.synthesize_segment(
            script_text,
            voice_id,
            output_path,
            controller=controller,
        )
        duration_ms = wave_duration_ms(output_path)
        slide.tts_audio_path = str(output_path)
        slide.actual_tts_duration_ms = duration_ms
        slide.voice_id = voice_id
        slide.tts_status = "completed"
        audio_items.append(
            PptSlideAudio(
                slide_index=slide.slide_index,
                audio_path=output_path,
                duration_ms=duration_ms,
            )
        )
    return audio_items


def _render_slide_segment(
    image_path: str | Path,
    audio_path: str | Path,
    duration_ms: int,
    output_path: str | Path,
    *,
    settings: AppSettings,
    controller: object | None = None,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        _effective_ffmpeg_path(settings),
        "-y" if settings.media.overwrite_existing else "-n",
        "-hide_banner",
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-i",
        str(audio_path),
        "-t",
        ms_to_seconds(duration_ms),
        "-c:v",
        settings.media.video_codec,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        settings.media.audio_codec,
        "-shortest",
        str(output_path),
    ]
    _run_command(command, "Failed to render a PPT slide video segment.", controller=controller)
    return output_path


def _concat_segments(
    segment_paths: list[Path],
    output_path: str | Path,
    *,
    settings: AppSettings,
    controller: object | None = None,
) -> Path:
    if not segment_paths:
        raise ExportError("No PPT video segments were generated.")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        encoding="utf-8",
        delete=False,
    ) as concat_file:
        for segment_path in segment_paths:
            normalized_path = segment_path.as_posix().replace("'", "'\\''")
            concat_file.write(f"file '{normalized_path}'\n")
        concat_list_path = Path(concat_file.name)

    command = [
        _effective_ffmpeg_path(settings),
        "-y" if settings.media.overwrite_existing else "-n",
        "-hide_banner",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list_path),
        "-c",
        "copy",
        str(output_path),
    ]
    try:
        _run_command(command, "Failed to concatenate PPT video segments.", controller=controller)
    finally:
        try:
            concat_list_path.unlink(missing_ok=True)
        except OSError:
            pass
    return output_path


def export_ppt_voiceover_video(
    project: Project,
    settings: AppSettings,
    output_dir: str | Path,
    container: str,
    *,
    burn_subtitles: bool = True,
    export_sidecar_srt: bool = True,
    subtitle_mode: str = "bilingual",
    voiceover_language: str = "en",
    selected_voice_id: str | None = None,
    controller: object | None = None,
    progress_callback: ProgressCallback = None,
) -> ExportPlan:
    if project.project_kind != "ppt":
        raise ExportError("The current project is not a PPT project.")
    if not project.slides:
        raise ExportError("No PPT slides are available to export.")

    missing_previews = [
        slide.slide_index
        for slide in project.slides
        if not slide.preview_image_path or not Path(slide.preview_image_path).exists()
    ]
    if missing_previews:
        raise ExportError(
            "Some slide preview images are missing: "
            + ", ".join(str(item) for item in missing_previews[:10])
        )

    output_path = choose_output_path(
        project_name=project.name,
        output_dir=output_dir,
        export_type="ppt_voiceover",
        container=container,
        overwrite_existing=settings.media.overwrite_existing,
    )
    plan = ExportPlan(cut_ranges=[], keep_ranges=[], output_path=output_path)

    normalized_language = resolve_ppt_voiceover_language(project, voiceover_language)
    effective_subtitle_mode = resolve_ppt_subtitle_mode(project, subtitle_mode)
    language_label = "中文" if normalized_language == "zh" else "英文"
    _notify_progress(progress_callback, 5, f"正在准备{language_label}配音...")
    audio_items = _synthesize_slide_audios(
        project,
        settings,
        voiceover_language=normalized_language,
        selected_voice_id=selected_voice_id,
        controller=controller,
        progress_callback=progress_callback,
    )
    slide_audio_by_index = {item.slide_index: item for item in audio_items}

    ordered_export_slides = [
        slide for slide in project.slides if slide.slide_index in slide_audio_by_index
    ]
    if not ordered_export_slides:
        raise ExportError("No PPT slides contain usable voiceover scripts to export.")

    with tempfile.TemporaryDirectory(prefix="vcut_ppt_export_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        segment_paths: list[Path] = []
        total_segments = len(ordered_export_slides)
        for item_index, slide in enumerate(ordered_export_slides, start=1):
            _controller_checkpoint(controller)
            audio_item = slide_audio_by_index[slide.slide_index]
            segment_path = temp_dir / f"segment_{slide.slide_index:03d}.{container.lstrip('.') or 'mp4'}"
            _notify_progress(
                progress_callback,
                45 + int((item_index - 1) * 25 / max(1, total_segments)),
                f"正在渲染第 {item_index}/{total_segments} 页视频片段...",
            )
            _render_slide_segment(
                slide.preview_image_path,
                audio_item.audio_path,
                max(500, audio_item.duration_ms),
                segment_path,
                settings=settings,
                controller=controller,
            )
            segment_paths.append(segment_path)

        base_video_path = temp_dir / f"ppt_voiceover_base.{container.lstrip('.') or 'mp4'}"
        _notify_progress(progress_callback, 75, "正在拼接整套 PPT 视频...")
        _concat_segments(
            segment_paths,
            base_video_path,
            settings=settings,
            controller=controller,
        )

        subtitle_cues = _build_subtitle_cues(project, audio_items, subtitle_mode=effective_subtitle_mode)
        if not subtitle_cues:
            raise ExportError("No subtitle text is available for PPT export.")
        srt_path = write_srt(subtitle_cues, temp_dir / "ppt_voiceover_subtitles.srt")

        chinese_srt_path: Path | None = None
        english_srt_path: Path | None = None
        normalized_subtitle_mode = effective_subtitle_mode
        if burn_subtitles and normalized_subtitle_mode == "bilingual":
            chinese_cues = _build_subtitle_cues(project, audio_items, subtitle_mode="zh")
            english_cues = _build_subtitle_cues(project, audio_items, subtitle_mode="en")
            if chinese_cues and english_cues:
                chinese_srt_path = write_srt(chinese_cues, temp_dir / "ppt_voiceover_zh.srt")
                english_srt_path = write_srt(english_cues, temp_dir / "ppt_voiceover_en.srt")

        _notify_progress(progress_callback, 90, "正在封装字幕与成片...")
        if burn_subtitles or export_sidecar_srt:
            render_video_with_subtitles(
                base_video_path,
                plan.output_path,
                settings=settings,
                container=container,
                burn_subtitles=burn_subtitles,
                has_audio=True,
                srt_path=srt_path,
                subtitle_mode=effective_subtitle_mode,
                chinese_srt_path=chinese_srt_path,
                english_srt_path=english_srt_path,
                controller=controller,
            )
        else:
            shutil.copy2(base_video_path, plan.output_path)

        if export_sidecar_srt:
            _notify_progress(progress_callback, 96, "正在写出字幕文件...")
            write_sidecar_srt(srt_path, plan.output_path.with_suffix(".srt"))

    _notify_progress(progress_callback, 100, "PPT 视频导出完成。")
    return plan
