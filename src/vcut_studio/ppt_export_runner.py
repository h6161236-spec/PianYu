from __future__ import annotations

import argparse
import ctypes
import json
import sys
from pathlib import Path

from .exporting import ExportPlan
from .models import Project
from .ppt_exporting import export_ppt_voiceover_video
from .settings import AppSettings


def _lower_process_priority() -> None:
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        below_normal_priority = 0x00004000
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), below_normal_priority)
    except Exception:
        return


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run PPT voiceover export in a dedicated process.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--settings", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--subtitle-mode", required=True)
    parser.add_argument("--voiceover-language", required=True)
    parser.add_argument("--selected-voice-id", default="")
    parser.add_argument("--burn-subtitles", action="store_true")
    parser.add_argument("--export-sidecar-srt", action="store_true")
    return parser.parse_args()


def _emit_progress(value: int, message: str) -> None:
    sys.stdout.write(
        json.dumps(
            {
                "type": "progress",
                "value": int(value),
                "message": str(message or ""),
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    sys.stdout.flush()


def _load_project(path: str | Path) -> Project:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return Project.from_dict(payload)


def _load_settings(path: str | Path) -> AppSettings:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return AppSettings.from_dict(payload)


def _write_result(path: str | Path, plan: ExportPlan, project: Project) -> None:
    Path(path).write_text(
        json.dumps(
            {
                "output_path": str(plan.output_path),
                "project_snapshot": project.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    _lower_process_priority()
    args = _parse_args()
    project = _load_project(args.project)
    settings = _load_settings(args.settings)
    voiceover_language = str(args.voiceover_language or "zh").strip().lower()
    settings.tts.provider_type = settings.tts.provider_type_for_language(voiceover_language)
    settings.tts.default_voice = settings.tts.default_voice_for_language(voiceover_language)
    selected_voice_id = str(args.selected_voice_id or "").strip()
    if selected_voice_id:
        settings.tts.default_voice = selected_voice_id
    plan = export_ppt_voiceover_video(
        project=project,
        settings=settings,
        output_dir=args.output_dir,
        container=args.container,
        burn_subtitles=bool(args.burn_subtitles),
        export_sidecar_srt=bool(args.export_sidecar_srt),
        subtitle_mode=args.subtitle_mode,
        voiceover_language=voiceover_language,
        selected_voice_id=selected_voice_id,
        progress_callback=_emit_progress,
    )
    _write_result(args.result, plan, project)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
