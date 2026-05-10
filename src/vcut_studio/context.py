from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .models import Project
from .project_store import ProjectStore
from .settings import AppSettings, SettingsStore


@dataclass(slots=True)
class AppContext:
    settings_store: SettingsStore
    project_store: ProjectStore
    settings: AppSettings
    current_project: Project = field(
        default_factory=lambda: Project.new("未命名演示", project_kind="ppt")
    )
    current_project_path: Path | None = None
    project_dirty: bool = False

    def set_project(self, project: Project, path: Path | None = None) -> None:
        self.current_project = project
        self.current_project_path = path
        self.project_dirty = False

    def mark_project_dirty(self) -> None:
        self.project_dirty = True

    def mark_project_saved(self, path: Path | None = None) -> None:
        if path is not None:
            self.current_project_path = path
        self.project_dirty = False
