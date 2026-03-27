from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class WordTiming:
    text: str
    start_ms: int
    end_ms: int
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WordTiming":
        return cls(
            text=str(data.get("text", "")),
            start_ms=int(data.get("start_ms", 0)),
            end_ms=int(data.get("end_ms", 0)),
            confidence=float(data.get("confidence", 1.0)),
        )


@dataclass(slots=True)
class MediaInfo:
    duration_ms: int = 0
    has_video: bool = False
    has_audio: bool = False
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    sample_rate: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_ms": self.duration_ms,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "sample_rate": self.sample_rate,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MediaInfo":
        return cls(
            duration_ms=int(data.get("duration_ms", 0)),
            has_video=bool(data.get("has_video", False)),
            has_audio=bool(data.get("has_audio", False)),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            fps=float(data.get("fps", 0.0)),
            video_codec=str(data.get("video_codec", "")),
            audio_codec=str(data.get("audio_codec", "")),
            sample_rate=int(data.get("sample_rate", 0)),
        )


@dataclass(slots=True)
class Segment:
    segment_id: str
    start_ms: int
    end_ms: int
    zh_text: str = ""
    en_text: str = ""
    words: list[WordTiming] = field(default_factory=list)
    keep: bool = True
    dub_selected: bool = False
    translation_status: str = "pending"
    tts_status: str = "pending"
    dub_audio_path: str | None = None
    dub_text: str = ""
    dub_voice_id: str | None = None
    dub_rate: float | None = None
    voice_id: str | None = None
    notes: str = ""

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "zh_text": self.zh_text,
            "en_text": self.en_text,
            "words": [word.to_dict() for word in self.words],
            "keep": self.keep,
            "dub_selected": self.dub_selected,
            "translation_status": self.translation_status,
            "tts_status": self.tts_status,
            "dub_audio_path": self.dub_audio_path,
            "dub_text": self.dub_text,
            "dub_voice_id": self.dub_voice_id,
            "dub_rate": self.dub_rate,
            "voice_id": self.voice_id,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(
            segment_id=str(data.get("segment_id", uuid4().hex)),
            start_ms=int(data.get("start_ms", 0)),
            end_ms=int(data.get("end_ms", 0)),
            zh_text=str(data.get("zh_text", "")),
            en_text=str(data.get("en_text", "")),
            words=[WordTiming.from_dict(item) for item in data.get("words", [])],
            keep=bool(data.get("keep", True)),
            dub_selected=bool(data.get("dub_selected", False)),
            translation_status=str(data.get("translation_status", "pending")),
            tts_status=str(data.get("tts_status", "pending")),
            dub_audio_path=data.get("dub_audio_path"),
            dub_text=str(data.get("dub_text", "")),
            dub_voice_id=data.get("dub_voice_id"),
            dub_rate=(
                float(data.get("dub_rate"))
                if data.get("dub_rate") is not None
                else None
            ),
            voice_id=data.get("voice_id"),
            notes=str(data.get("notes", "")),
        )


@dataclass(slots=True)
class CutSuggestion:
    suggestion_id: str
    start_ms: int
    end_ms: int
    reason: str
    score: float
    accepted: bool = False
    details: str = ""

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "reason": self.reason,
            "score": self.score,
            "accepted": self.accepted,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CutSuggestion":
        return cls(
            suggestion_id=str(data.get("suggestion_id", uuid4().hex)),
            start_ms=int(data.get("start_ms", 0)),
            end_ms=int(data.get("end_ms", 0)),
            reason=str(data.get("reason", "unknown")),
            score=float(data.get("score", 0.0)),
            accepted=bool(data.get("accepted", False)),
            details=str(data.get("details", "")),
        )


@dataclass(slots=True)
class ExportJob:
    export_id: str
    export_type: str
    output_path: str
    status: str
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "export_id": self.export_id,
            "export_type": self.export_type,
            "output_path": self.output_path,
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExportJob":
        return cls(
            export_id=str(data.get("export_id", uuid4().hex)),
            export_type=str(data.get("export_type", "unknown")),
            output_path=str(data.get("output_path", "")),
            status=str(data.get("status", "pending")),
            created_at=str(data.get("created_at", utc_now_iso())),
        )


@dataclass(slots=True)
class Project:
    project_id: str
    name: str
    video_path: str = ""
    audio_path: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    source_duration_ms: int = 0
    analysis_completed: bool = False
    media_info: MediaInfo = field(default_factory=MediaInfo)
    segments: list[Segment] = field(default_factory=list)
    cut_suggestions: list[CutSuggestion] = field(default_factory=list)
    exports: list[ExportJob] = field(default_factory=list)

    @classmethod
    def new(cls, name: str) -> "Project":
        return cls(project_id=uuid4().hex, name=name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "video_path": self.video_path,
            "audio_path": self.audio_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_duration_ms": self.source_duration_ms,
            "analysis_completed": self.analysis_completed,
            "media_info": self.media_info.to_dict(),
            "segments": [segment.to_dict() for segment in self.segments],
            "cut_suggestions": [suggestion.to_dict() for suggestion in self.cut_suggestions],
            "exports": [job.to_dict() for job in self.exports],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        media_info_data = data.get("media_info", {"duration_ms": data.get("source_duration_ms", 0)})
        media_info = MediaInfo.from_dict(media_info_data)
        source_duration_ms = int(data.get("source_duration_ms", media_info.duration_ms))
        return cls(
            project_id=str(data.get("project_id", uuid4().hex)),
            name=str(data.get("name", "Untitled Project")),
            video_path=str(data.get("video_path", "")),
            audio_path=str(data.get("audio_path", "")),
            created_at=str(data.get("created_at", utc_now_iso())),
            updated_at=str(data.get("updated_at", utc_now_iso())),
            source_duration_ms=source_duration_ms,
            analysis_completed=bool(data.get("analysis_completed", False)),
            media_info=media_info,
            segments=[Segment.from_dict(item) for item in data.get("segments", [])],
            cut_suggestions=[CutSuggestion.from_dict(item) for item in data.get("cut_suggestions", [])],
            exports=[ExportJob.from_dict(item) for item in data.get("exports", [])],
        )
