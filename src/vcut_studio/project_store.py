from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Project, utc_now_iso


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*]+", "_", name).strip()
    return cleaned or "untitled-project"


class ProjectStore:
    extension = ".vcutproj"

    def load(self, path: str | Path) -> Project:
        project_path = Path(path)
        data = json.loads(project_path.read_text(encoding="utf-8"))
        return Project.from_dict(data)

    def save(self, project: Project, path: str | Path) -> Path:
        project_path = Path(path)
        project_path.parent.mkdir(parents=True, exist_ok=True)
        project.updated_at = utc_now_iso()
        payload = json.dumps(project.to_dict(), indent=2, ensure_ascii=False)
        project_path.write_text(payload, encoding="utf-8")
        return project_path

    def default_path(self, workspace_dir: str | Path, project_name: str) -> Path:
        workspace_path = Path(workspace_dir)
        return workspace_path / "projects" / f"{sanitize_filename(project_name)}{self.extension}"

    def autosave_path(
        self,
        workspace_dir: str | Path,
        project_name: str,
        project_id: str,
    ) -> Path:
        workspace_path = Path(workspace_dir)
        return (
            workspace_path
            / "autosave"
            / f"{sanitize_filename(project_name)}_{sanitize_filename(project_id)}.autosave{self.extension}"
        )
