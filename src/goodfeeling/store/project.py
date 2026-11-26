"""A project: a footage folder's working state under the data root. Never next to the footage."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from goodfeeling.schemas.edl import EDL, write_atomic
from goodfeeling.store.db import Database

log = logging.getLogger(__name__)

SUBDIRS = ("proxies", "frames", "logs", "edits", "renders", "analysis")
DEFAULT_EDIT = "default"
_EDIT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def check_edit_name(name: str) -> str:
    if not _EDIT_NAME.match(name) or name in {"revisions"}:
        raise ValueError(f"edit name {name!r} must be lowercase letters, digits and dashes (max 40)")
    return name


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "project"


class UnsafeWrite(RuntimeError):
    pass


def ensure_outside(path: Path, protected: list[Path]) -> Path:
    resolved = path.expanduser().resolve()
    for folder in protected:
        folder = folder.resolve()
        if resolved == folder or folder in resolved.parents:
            raise UnsafeWrite(f"refusing to write {resolved}: it is inside the footage folder {folder}")
    return resolved


@dataclass
class Project:
    data_root: Path
    slug: str
    footage_dir: Path
    db: Database
    id: int

    @classmethod
    def open(cls, data_root: Path, footage_dir: Path, name: str | None = None) -> Project:
        footage_dir = footage_dir.expanduser().resolve()
        data_root = data_root.expanduser().resolve()
        ensure_outside(data_root, [footage_dir])
        slug = slugify(name or footage_dir.name)
        db = Database(data_root / "app.db")
        project_id = db.project_id(slug, name or footage_dir.name, str(footage_dir))
        project = cls(data_root=data_root, slug=slug, footage_dir=footage_dir, db=db, id=project_id)
        for sub in SUBDIRS:
            (project.root / sub).mkdir(parents=True, exist_ok=True)
        return project

    @property
    def root(self) -> Path:
        return self.data_root / "projects" / self.slug

    @property
    def shot_cache(self) -> Path:
        return self.data_root / "cache" / "shots"

    def path(self, *parts: str) -> Path:
        return ensure_outside(self.root.joinpath(*parts), [self.footage_dir])

    def write_json(self, rel: str, data: object) -> Path:
        return write_atomic(self.path(rel), json.dumps(data, indent=2) + "\n")

    def read_json(self, rel: str) -> object | None:
        target = self.root / rel
        return json.loads(target.read_text()) if target.exists() else None

    def edit_path(self, edit: str, area: str, *parts: str) -> Path:
        """`edits/...` or `renders/...` for the default edit; `edits/<name>/...`, `renders/<name>/...` for others."""
        check_edit_name(edit)
        return self.path(area, *parts) if edit == DEFAULT_EDIT else self.path(area, edit, *parts)

    def save_edl(self, edl: EDL, origin: str, edit: str = DEFAULT_EDIT, edit_format: str = "landscape") -> Path:
        """Write the edit's edl.json plus a numbered revision, and move that edit's revision pointer."""
        row = self.db.execute(
            "SELECT COALESCE(MAX(revision),0) AS r FROM edl_revisions WHERE project_id=? AND edit=?", (self.id, edit)
        ).fetchone()
        revision = int(row["r"]) + 1
        rev_path = edl.save(self.edit_path(edit, "edits", "revisions", f"{revision:04d}.json"))
        target = edl.save(self.edit_path(edit, "edits", "edl.json"))
        now = time.time()
        self.db.execute(
            "INSERT INTO edl_revisions (project_id, edit, revision, path, origin, created_at) VALUES (?,?,?,?,?,?)",
            (self.id, edit, revision, str(rev_path), origin, now),
        )
        self.db.execute(
            "INSERT INTO edits (project_id, name, format, current_revision, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(project_id, name) DO UPDATE SET format=excluded.format, "
            "current_revision=excluded.current_revision, updated_at=excluded.updated_at",
            (self.id, edit, edit_format, revision, now),
        )
        return target
