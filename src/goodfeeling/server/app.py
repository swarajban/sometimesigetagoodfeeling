"""FastAPI app: projects, edits, segments and review choices, jobs with SSE progress, settings, the Claude log.

Binds to 127.0.0.1 only (see serve.py). The API key is never returned in full.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from goodfeeling import DISPLAY_NAME
from goodfeeling.jobs import queue
from goodfeeling.server import models as m
from goodfeeling.settings import Settings, config_path, load_settings
from goodfeeling.store.db import Database
from goodfeeling.store.project import DEFAULT_EDIT, Project, check_edit_name, ensure_outside, slugify

VIDEO_EXT = {".mp4", ".mov"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".aif", ".aiff", ".ogg", ".opus"}
WEB_DIST = Path(os.environ.get("GOODFEELING_WEB_DIST") or Path(__file__).resolve().parents[3] / "web" / "dist")


def fps_of(rate) -> float:
    return rate[0] / rate[1]


class Store:
    """The server's view of the data root, with one SQLite connection (WAL, busy timeout) per request thread."""

    def __init__(self, data_root: Path):
        self.root = data_root
        self.local = threading.local()
        self.music_lock = threading.Lock()
        Database(data_root / "app.db")

    @property
    def db(self) -> Database:
        db = getattr(self.local, "db", None)
        if db is None:
            db = self.local.db = Database(self.root / "app.db")
        return db

    def project(self, slug: str) -> Project:
        row = self.db.execute("SELECT id, slug, footage_dir FROM projects WHERE slug=?", (slug,)).fetchone()
        if row is None:
            raise HTTPException(404, f"no project {slug!r}")
        return Project(self.root, row["slug"], Path(row["footage_dir"]), self.db, row["id"])


def job_out(store: Store, row: dict[str, Any]) -> m.Job:
    slug = None
    cost = 0.0
    if row["project_id"] is not None:
        p = store.db.execute("SELECT slug FROM projects WHERE id=?", (row["project_id"],)).fetchone()
        slug = p["slug"] if p else None
        if row["started_at"]:
            until = row["finished_at"] or time.time()
            c = store.db.execute(
                "SELECT COALESCE(SUM(cost_usd),0) AS c FROM ai_log "
                "WHERE project_id=? AND created_at>=? AND created_at<=?",
                (row["project_id"], row["started_at"], until + 1),
            ).fetchone()
            cost = float(c["c"])
    return m.Job(
        id=row["id"],
        project=slug,
        kind=row["kind"],
        edit=row["edit"],
        status=row["status"],
        stage=row["stage"],
        step_done=row["step_done"],
        step_total=row["step_total"],
        estimate=json.loads(row["estimate_json"]) if row["estimate_json"] else None,
        error=row["error"],
        attempts=row["attempts"],
        cost_usd=round(cost, 4),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        updated_at=row["updated_at"],
        result=json.loads(row["result_json"]) if row["result_json"] else None,
    )


def file_url(project: Project, path: Path) -> str | None:
    if not path.exists():
        return None
    rel = path.relative_to(project.root).as_posix()
    return f"/api/projects/{project.slug}/files/{rel}?v={int(path.stat().st_mtime)}"


def edit_summary(project: Project, row: dict[str, Any]) -> m.EditSummary:
    name = row["name"]
    final = project.edit_path(name, "renders", "final.mp4")
    duration = None
    edl_path = project.edit_path(name, "edits", "edl.json")
    if edl_path.exists():
        from goodfeeling.schemas.edl import EDL

        edl = EDL.load(edl_path)
        duration = edl.total_frames() / fps_of(edl.output.fps)
    return m.EditSummary(
        name=name,
        format=row["format"],
        revision=row["current_revision"],
        updated_at=row["updated_at"],
        final_url=file_url(project, final),
        contact_sheet_url=file_url(project, project.edit_path(name, "renders", "contact_sheet.jpg")),
        duration_s=duration,
        options=json.loads(row["options_json"]) if row["options_json"] else None,
    )


def project_summary(store: Store, row: dict[str, Any]) -> m.ProjectSummary:
    project = Project(store.root, row["slug"], Path(row["footage_dir"]), store.db, row["id"])
    edits = [
        edit_summary(project, dict(r))
        for r in store.db.execute(
            "SELECT * FROM edits WHERE project_id=? ORDER BY name='default' DESC, updated_at DESC", (row["id"],)
        ).fetchall()
    ]
    clips = store.db.execute("SELECT COUNT(*) AS n FROM clips WHERE project_id=?", (row["id"],)).fetchone()["n"]
    segments = project.read_json("analysis/segments.json") or []
    active = store.db.execute(
        "SELECT * FROM jobs WHERE project_id=? AND status IN ('queued','running','approval') ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    thumb = None
    if edits:
        from goodfeeling.schemas.edl import EDL

        edl_path = project.edit_path(edits[0].name, "edits", "edl.json")
        if edl_path.exists():
            best = max(EDL.load(edl_path).shots, key=lambda s: s.score or 0)
            thumb = f"/api/projects/{project.slug}/frame/{best.clip}/{(best.in_ + best.out) // 2}.jpg?w=640"
    return m.ProjectSummary(
        slug=row["slug"],
        name=row["name"],
        footage_dir=row["footage_dir"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        cost_usd=round(store.db.project_cost(row["id"]), 4),
        clips=clips,
        segments=len(segments),
        edits=edits,
        thumb_url=thumb,
        active_job=job_out(store, dict(active)) if active else None,
    )


def music_info(music, length: float | None = None) -> m.MusicInfo:
    from goodfeeling.pipeline.stretch import fmt, suggestions

    energy = list(music.beat_energy)
    if len(energy) > 400:
        step = len(energy) / 400
        energy = [max(energy[int(i * step) : max(int(i * step) + 1, int((i + 1) * step))]) for i in range(400)]
    stretches = []
    if length:
        spb = 60 / music.tempo
        for why, start, beats in suggestions(music, length):
            begin = music.beats[start]
            stretches.append(m.Stretch(why=why, start_s=begin, end_s=begin + beats * spb, start=fmt(begin)))
    return m.MusicInfo(
        path=music.path,
        name=Path(music.path).name,
        tempo=music.tempo,
        duration_s=music.duration_s,
        beats=len(music.beats),
        grid=music.grid,
        sections=[
            m.Section(index=i, label=s.label, start_s=s.start_s, end_s=s.end_s, energy=s.energy)
            for i, s in enumerate(music.sections)
        ],
        energy=[round(e, 3) for e in energy],
        suggestions=stretches,
    )


def load_segments(project: Project) -> list:
    from goodfeeling.schemas.segment import Segment

    data = project.read_json("analysis/segments.json")
    if data is None:
        rows = project.db.execute(
            "SELECT segment_json FROM segments WHERE project_id=? ORDER BY seg_id", (project.id,)
        ).fetchall()
        data = [json.loads(r["segment_json"]) for r in rows]
    return [Segment.model_validate(d) for d in data]


def load_moments(project: Project) -> list:
    from goodfeeling.schemas.moment import Moment

    return [Moment.model_validate(d) for d in project.read_json("analysis/moments.json") or []]


def clip_rows(project: Project) -> dict[str, dict[str, Any]]:
    rows = project.db.execute(
        "SELECT clip_id, path, info_json, proxy_path FROM clips WHERE project_id=?", (project.id,)
    ).fetchall()
    return {r["clip_id"]: {**dict(r), "info": json.loads(r["info_json"])} for r in rows}


def plan_segments(project: Project, edit: str) -> list[str]:
    data = project.read_json("edits/plan.json" if edit == DEFAULT_EDIT else f"edits/{edit}/plan.json")
    return [s["segment_id"] for s in data["plan"]["shots"]] if data else []


def edit_options(project: Project, edit: str) -> dict[str, Any]:
    """How an edit was made, for re-planning it. Edits made by the CLI before options were stored are rebuilt from
    their EDL."""
    from goodfeeling.schemas.edl import EDL

    row = project.db.execute(
        "SELECT format, options_json FROM edits WHERE project_id=? AND name=?", (project.id, edit)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"no edit {edit!r}")
    if row["options_json"]:
        return json.loads(row["options_json"])
    edl = EDL.load(project.edit_path(edit, "edits", "edl.json"))
    fps = fps_of(edl.output.fps)
    short_side = min(edl.output.width, edl.output.height)
    return {
        "footage": str(project.footage_dir),
        "music": edl.music.path,
        "length": round(edl.total_frames() / fps, 2),
        "style": "fast",
        "format": row["format"],
        "edit": edit,
        "music_start": edl.music.start_frame / fps,
        "res": "4k" if short_side >= 2160 else "1080",
        "project": project.slug,
    }


def settings_out(settings: Settings, root: Path) -> m.SettingsOut:
    from goodfeeling.ai.pricing import PRICES

    env = os.environ.get("ANTHROPIC_API_KEY")
    stored = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    key = env or stored
    return m.SettingsOut(
        models=settings.models.model_dump(),
        effort=settings.effort.model_dump(),
        weights=settings.weights.model_dump(),
        style_note=settings.style_note,
        pace=settings.pace,
        max_cost_usd=settings.max_cost_usd,
        api_key=m.ApiKeyInfo(
            set=bool(key),
            source="env" if env else "settings" if stored else None,
            hint=f"…{key[-4:]}" if key and len(key) > 12 else None,
        ),
        data_root=str(root),
        config_path=str(config_path()),
        available_models=list(PRICES),
    )


def check_footage(path: str) -> m.PathCheck:
    from goodfeeling.pipeline.ingest import find_videos

    folder = Path(path).expanduser()
    if not path.strip():
        return m.PathCheck(ok=False, path=path, message="Enter the folder with your clips")
    if not folder.is_dir():
        return m.PathCheck(ok=False, path=str(folder), message="Not a folder")
    videos = find_videos(folder)
    if not videos:
        return m.PathCheck(ok=False, path=str(folder.resolve()), message="No .mp4 or .mov files in this folder")
    size = sum(v.stat().st_size for v in videos)
    return m.PathCheck(
        ok=True,
        path=str(folder.resolve()),
        message=f"{len(videos)} clip{'s' if len(videos) != 1 else ''}, {size / 1e9:.1f} GB",
        videos=len(videos),
        size_bytes=size,
    )


def check_music(path: str) -> m.PathCheck:
    file = Path(path).expanduser()
    if not path.strip():
        return m.PathCheck(ok=False, path=path, message="Enter the music file")
    if not file.is_file():
        return m.PathCheck(ok=False, path=str(file), message="File not found")
    if file.suffix.lower() not in AUDIO_EXT:
        return m.PathCheck(ok=False, path=str(file.resolve()), message=f"{file.suffix or 'This'} isn't an audio file")
    return m.PathCheck(ok=True, path=str(file.resolve()), message=f"{file.stat().st_size / 1e6:.1f} MB")


def create_app(data_root: Path | None = None) -> FastAPI:
    root = load_settings().resolved_data_root(data_root)
    store = Store(root)
    app = FastAPI(title=DISPLAY_NAME, version="0.2.0")
    app.state.store = store
    app.state.stopping = False
    extra_hosts = [h.strip() for h in os.environ.get("GOODFEELING_ALLOWED_HOSTS", "").split(",") if h.strip()]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", *extra_hosts])

    @app.middleware("http")
    async def require_app_header(request: Request, call_next):
        """Other web pages can send simple cross-site POSTs to localhost; only this app sends the header."""
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-goodfeeling") != "1":
            return JSONResponse({"detail": "requests that change things need the X-Goodfeeling header"}, 403)
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "data_root": str(root)}

    # Projects and edits

    @app.get("/api/projects", response_model=list[m.ProjectSummary])
    def list_projects():
        rows = store.db.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        return [project_summary(store, dict(r)) for r in rows]

    @app.get("/api/projects/{slug}", response_model=m.ProjectDetail)
    def get_project(slug: str):
        from goodfeeling.schemas.music import MusicAnalysis

        project = store.project(slug)
        row = store.db.execute("SELECT * FROM projects WHERE id=?", (project.id,)).fetchone()
        summary = project_summary(store, dict(row))
        music = project.read_json("analysis/music.json")
        jobs = store.db.execute(
            "SELECT * FROM jobs WHERE project_id=? ORDER BY id DESC LIMIT 10", (project.id,)
        ).fetchall()
        return m.ProjectDetail(
            **summary.model_dump(),
            summary=project.read_json("analysis/summary.json"),
            music=music_info(MusicAnalysis.model_validate(music)) if music else None,
            session_sheet_url=file_url(project, project.root / "frames" / "session_sheet.jpg"),
            jobs=[job_out(store, dict(j)) for j in jobs],
        )

    @app.get("/api/projects/{slug}/edits/{edit}", response_model=m.EditDetail)
    def get_edit(slug: str, edit: str):
        from goodfeeling.schemas.edl import EDL

        project = store.project(slug)
        row = store.db.execute("SELECT * FROM edits WHERE project_id=? AND name=?", (project.id, edit)).fetchone()
        if row is None:
            raise HTTPException(404, f"no edit {edit!r}")
        summary = edit_summary(project, dict(row))
        edl = EDL.load(project.edit_path(edit, "edits", "edl.json"))
        plan_data = project.read_json("edits/plan.json" if edit == DEFAULT_EDIT else f"edits/{edit}/plan.json") or {
            "plan": {"shots": []}
        }
        plan = plan_data["plan"]
        planned = plan.get("shots", [])
        fps = fps_of(edl.output.fps)
        shots = []
        for entry in edl.timeline():
            shot = entry.shot
            src_fps = fps_of(edl.sources[shot.clip].fps)
            p = planned[entry.index] if entry.index < len(planned) and len(planned) == len(edl.shots) else {}
            shots.append(
                m.PlanShot(
                    index=entry.index,
                    shot_id=shot.id,
                    segment_id=p.get("segment_id"),
                    clip=shot.clip,
                    start_s=entry.start / fps,
                    end_s=entry.end / fps,
                    source_in_s=shot.in_ / src_fps,
                    source_out_s=shot.out / src_fps,
                    speed=shot.constant_speed,
                    transition=shot.transition,
                    role=p.get("role", ""),
                    reason=shot.reason or p.get("reason", ""),
                    score=shot.score,
                    tags=shot.tags,
                    description=shot.description,
                    starred=shot.starred,
                    thumb_url=f"/api/projects/{slug}/frame/{shot.clip}/{(shot.in_ + shot.out) // 2}.jpg",
                )
            )
        start_s = edl.music.start_frame / fps
        plan_md = project.edit_path(edit, "edits", "plan.md")
        return m.EditDetail(
            **summary.model_dump(),
            title=plan.get("title", ""),
            concept=plan.get("concept", ""),
            order_note=plan.get("order_note", ""),
            closing_note=plan.get("closing_note", ""),
            source=plan.get("source", ""),
            fixed=bool(plan_data.get("fixed")),
            fallback=bool(plan_data.get("fallback")),
            validation_rounds=plan_data.get("validation_rounds", []),
            music_start_s=start_s,
            music_end_s=start_s + edl.total_frames() / fps,
            width=edl.output.width,
            height=edl.output.height,
            fps=f"{edl.output.fps[0]}/{edl.output.fps[1]}",
            shots=shots,
            plan_md=plan_md.read_text() if plan_md.exists() else None,
        )

    @app.post("/api/projects/{slug}/edits/{edit}/replan", response_model=m.Job)
    def replan(slug: str, edit: str):
        project = store.project(slug)
        options = edit_options(project, edit)
        options["data_root"] = str(root)
        return enqueue_run(project, options, edit)

    def enqueue_run(project: Project, options: dict[str, Any], edit: str) -> m.Job:
        busy = store.db.execute(
            "SELECT id FROM jobs WHERE project_id=? AND status IN ('queued','running','approval')", (project.id,)
        ).fetchone()
        if busy:
            raise HTTPException(409, f"job {busy['id']} is still active for this project")
        job_id = queue.enqueue(store.db, "run", {"options": options}, project_id=project.id, edit=edit)
        return job_out(store, queue.get(store.db, job_id))

    # Segments and review

    def used_by_edit(project: Project) -> dict[str, dict[str, int]]:
        used: dict[str, dict[str, int]] = {}
        for r in store.db.execute("SELECT name FROM edits WHERE project_id=?", (project.id,)).fetchall():
            for i, sid in enumerate(plan_segments(project, r["name"])):
                used.setdefault(sid, {})[r["name"]] = i
        return used

    def no_choice() -> dict[str, Any]:
        return {"include": None, "star": False, "taste": 0}

    @app.get("/api/projects/{slug}/moments", response_model=list[m.MomentOut])
    def list_moments(slug: str):
        from goodfeeling.pipeline.run import review_pool

        project = store.project(slug)
        clips = clip_rows(project)
        choices = store.db.choices(project.id)
        _, _, _, excluded = review_pool(load_segments(project), choices)
        used = used_by_edit(project)
        out = []
        for moment in load_moments(project):
            clip = clips.get(moment.clip)
            if clip is None:
                continue
            fps = fps_of(clip["info"]["fps"])
            blocked = sorted(excluded.intersection(moment.segment_ids))
            out.append(
                m.MomentOut(
                    id=moment.id,
                    clip=moment.clip,
                    flight=moment.flight,
                    kind=moment.kind,
                    hold=moment.hold,
                    worth=moment.worth,
                    start_s=moment.start / fps,
                    end_s=moment.end / fps,
                    duration_s=moment.frames / fps,
                    peak_s=moment.peak / fps if moment.peak is not None else None,
                    description=moment.description,
                    reason=moment.reason,
                    segment_ids=moment.segment_ids,
                    source=moment.source,
                    proxy_url=f"/api/projects/{slug}/proxy/{moment.clip}",
                    thumb_url=f"/api/projects/{slug}/frame/{moment.clip}/"
                    f"{moment.peak if moment.peak is not None else (moment.start + moment.end) // 2}.jpg",
                    available=moment.id not in excluded and not blocked,
                    blocked_by=blocked,
                    choice=m.Choice(**(choices.get(moment.id) or no_choice())),
                    used_in=used.get(moment.id, {}),
                )
            )
        return out

    @app.get("/api/projects/{slug}/segments", response_model=list[m.SegmentOut])
    def list_segments(slug: str):
        from goodfeeling.pipeline.direct import HIGH_SCORE_EDGE
        from goodfeeling.pipeline.run import review_pool

        project = store.project(slug)
        segments = load_segments(project)
        clips = clip_rows(project)
        choices = store.db.choices(project.id)
        pool = {s.id for s in review_pool(segments, choices)[0]}
        used = used_by_edit(project)
        covering: dict[str, list[str]] = {}
        for moment in load_moments(project):
            for sid in moment.segment_ids:
                covering.setdefault(sid, []).append(moment.id)
        out = []
        for seg in segments:
            clip = clips.get(seg.clip)
            if clip is None:
                continue
            fps = fps_of(clip["info"]["fps"])
            c = seg.claude
            flags = [
                name
                for name, on in (
                    ("crash", seg.signals.crash or (c and c.penalties.crash)),
                    ("blur", seg.signals.blur or (c and c.penalties.blur)),
                    ("props", c and c.penalties.props_in_view),
                    ("horizon", c and c.penalties.horizon_chaos),
                )
                if on
            ]
            rejected = None
            if seg.kind != "flight" and seg.final_score < HIGH_SCORE_EDGE:
                rejected = f"{seg.kind.title()} footage scoring under {HIGH_SCORE_EDGE:g} is left out automatically"
            choice = choices.get(seg.id, {})
            out.append(
                m.SegmentOut(
                    id=seg.id,
                    clip=seg.clip,
                    flight=seg.flight,
                    kind=seg.kind,
                    start_s=seg.start / fps,
                    end_s=seg.end / fps,
                    duration_s=seg.frames / fps,
                    proxy_url=f"/api/projects/{slug}/proxy/{seg.clip}",
                    thumb_url=f"/api/projects/{slug}/frame/{seg.clip}/{(seg.start + seg.end) // 2}.jpg",
                    score=seg.final_score,
                    claude_score=c.score if c else None,
                    local_score=seg.local_score,
                    tags=c.tags if c else [],
                    description=c.description if c else "",
                    reason=c.reason if c else "",
                    flags=flags,
                    auto_rejected=rejected,
                    available=seg.id in pool,
                    choice=m.Choice(**(choice or no_choice())),
                    used_in=used.get(seg.id, {}),
                    moments=covering.get(seg.id, []),
                )
            )
        return out

    @app.put("/api/projects/{slug}/segments/{seg_id}/choice", response_model=m.Choice)
    def set_choice(slug: str, seg_id: str, update: m.ChoiceUpdate):
        project = store.project(slug)
        segments = {s.id: s for s in load_segments(project)}
        moments = {x.id: x for x in load_moments(project)}
        seg = segments.get(seg_id)
        moment = moments.get(seg_id)
        if seg is None and moment is None:
            raise HTTPException(404, f"no segment or moment {seg_id!r}")
        before = store.db.choices(project.id).get(seg_id, {"include": None, "star": False, "taste": 0})
        after = dict(before)
        if update.reset_include:
            after["include"] = None
        elif update.include is not None:
            after["include"] = update.include
            if update.include is False:
                after["star"] = False
        if update.star is not None:
            after["star"] = update.star
            if update.star and after["include"] is False:
                after["include"] = None
        if update.taste is not None:
            after["taste"] = update.taste
        now = time.time()
        store.db.execute(
            "INSERT INTO segment_choices (project_id, seg_id, include, star, taste, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(project_id, seg_id) DO UPDATE SET include=excluded.include, star=excluded.star, "
            "taste=excluded.taste, updated_at=excluded.updated_at",
            (
                project.id,
                seg_id,
                None if after["include"] is None else int(after["include"]),
                int(after["star"]),
                after["taste"],
                now,
            ),
        )
        if seg is not None:
            clip = clip_rows(project).get(seg.clip, {})
            snapshot = {
                "type": "segment",
                "clip": seg.clip,
                "fingerprint": clip.get("info", {}).get("fingerprint"),
                "start": seg.start,
                "end": seg.end,
                "kind": seg.kind,
                "score": seg.final_score,
                "claude": seg.claude.model_dump() if seg.claude else None,
                "signals": seg.signals.model_dump(),
            }
        else:
            clip = clip_rows(project).get(moment.clip, {})
            snapshot = {
                "type": "moment",
                "fingerprint": clip.get("info", {}).get("fingerprint"),
                **moment.model_dump(),
            }
        context = json.dumps(snapshot)
        events = []
        if after["taste"] != before["taste"]:
            events.append(
                ("like" if after["taste"] > 0 else "dislike", 1)
                if after["taste"]
                else ("like" if before["taste"] > 0 else "dislike", 0)
            )
        if after["star"] != before["star"]:
            events.append(("star", int(after["star"])))
        if after["include"] != before["include"]:
            events.append(("include", -1 if after["include"] is None else int(after["include"])))
        for kind, value in events:
            store.db.execute(
                "INSERT INTO feedback (project_id, seg_id, kind, value, segment_json, created_at) VALUES (?,?,?,?,?,?)",
                (project.id, seg_id, kind, value, context, now),
            )
        return m.Choice(**after)

    # Media

    @app.get("/api/projects/{slug}/proxy/{clip_id}")
    def proxy(slug: str, clip_id: str):
        project = store.project(slug)
        row = store.db.execute(
            "SELECT proxy_path FROM clips WHERE project_id=? AND clip_id=?", (project.id, clip_id)
        ).fetchone()
        if row is None or not row["proxy_path"] or not Path(row["proxy_path"]).exists():
            raise HTTPException(404, "no proxy")
        return FileResponse(row["proxy_path"], media_type="video/mp4", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/projects/{slug}/frame/{clip_id}/{frame}.jpg")
    def frame(slug: str, clip_id: str, frame: int, w: int = Query(480, ge=64, le=1280)):
        from goodfeeling.media.frames import extract_frame

        project = store.project(slug)
        row = store.db.execute(
            "SELECT info_json, proxy_path, fingerprint FROM clips WHERE project_id=? AND clip_id=?",
            (project.id, clip_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "no clip")
        out = project.path("frames", "thumbs", f"{row['fingerprint']}-{frame}-{w}.jpg")
        if not out.exists():
            info = json.loads(row["info_json"])
            out.parent.mkdir(parents=True, exist_ok=True)
            image = extract_frame(row["proxy_path"], max(0, min(frame, info["frames"] - 1)), tuple(info["fps"]), w)
            tmp = out.with_name(out.stem + f".{threading.get_ident()}.tmp.jpg")
            image.save(tmp, quality=82)
            os.replace(tmp, out)
        return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/projects/{slug}/files/{rel:path}")
    def project_file(slug: str, rel: str):
        project = store.project(slug)
        target = (project.root / rel).resolve()
        if project.root.resolve() not in target.parents or rel.split("/")[0] not in {"renders", "frames", "edits"}:
            raise HTTPException(404, "not found")
        if not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target)

    # Claude log

    @app.get("/api/projects/{slug}/ai-log", response_model=m.AiLogPage)
    def ai_log(slug: str, limit: int = Query(200, le=1000), offset: int = 0):
        project = store.project(slug)
        total = store.db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS c FROM ai_log WHERE project_id=?", (project.id,)
        ).fetchone()
        rows = store.db.execute(
            "SELECT id, step, model, input_tokens, output_tokens, cache_read_tokens, cost_usd, created_at "
            "FROM ai_log WHERE project_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (project.id, limit, offset),
        ).fetchall()
        return m.AiLogPage(
            total=total["n"], total_cost_usd=round(total["c"], 4), entries=[m.AiLogEntry(**dict(r)) for r in rows]
        )

    @app.get("/api/projects/{slug}/ai-log/{entry_id}", response_model=m.AiLogDetail)
    def ai_log_entry(slug: str, entry_id: int):
        project = store.project(slug)
        row = store.db.execute("SELECT * FROM ai_log WHERE project_id=? AND id=?", (project.id, entry_id)).fetchone()
        if row is None:
            raise HTTPException(404, "no such entry")
        data = dict(row)
        return m.AiLogDetail(
            **{k: data[k] for k in m.AiLogEntry.model_fields},
            request=json.loads(data["request_json"]),
            response=json.loads(data["response_json"]),
        )

    # Import

    @app.post("/api/import/check", response_model=m.ImportCheck)
    def import_check(req: m.ImportRequest):
        footage = check_footage(req.footage)
        music = check_music(req.music)
        slug = slugify(req.project or Path(footage.path).name) if footage.ok else ""
        exists = False
        if footage.ok:
            row = store.db.execute("SELECT footage_dir FROM projects WHERE slug=?", (slug,)).fetchone()
            exists = row is not None
            if row and row["footage_dir"] != footage.path:
                message = f"A project named {slug!r} uses another folder; give this one a name"
                footage = footage.model_copy(update={"ok": False, "message": message})
            try:
                ensure_outside(root, [Path(footage.path)])
            except Exception:
                footage = footage.model_copy(update={"ok": False, "message": "The data folder is inside this folder"})
        edit = req.edit or ("reel" if req.format == "reel" else DEFAULT_EDIT)
        return m.ImportCheck(footage=footage, music=music, project_slug=slug, project_exists=exists, edit=edit)

    @app.post("/api/import", response_model=m.ImportResult)
    def start_import(req: m.ImportRequest):
        from goodfeeling.media.fingerprint import fingerprint
        from goodfeeling.pipeline.pacing import parse_pace
        from goodfeeling.pipeline.stretch import StretchError, parse_time

        check = import_check(req)
        if not check.footage.ok:
            raise HTTPException(422, f"Footage: {check.footage.message}")
        if not check.music.ok:
            raise HTTPException(422, f"Music: {check.music.message}")
        try:
            edit = check_edit_name(check.edit)
            if req.pace:
                parse_pace(req.pace)
            music_start = parse_time(req.music_start) if req.music_start else None
        except (ValueError, StretchError) as exc:
            raise HTTPException(422, str(exc)) from exc
        project = Project.open(root, Path(check.footage.path), req.project)
        cached = root / "cache" / "music" / f"{fingerprint(Path(check.music.path))}.json"
        if cached.exists() and not (project.root / "analysis" / "music.json").exists():
            project.write_json("analysis/music.json", json.loads(cached.read_text()))
        options = {
            "footage": check.footage.path,
            "music": check.music.path,
            "length": req.length,
            "style": req.style,
            "res": req.res,
            "format": req.format,
            "edit": edit,
            "music_start": music_start,
            "pace": parse_pace(req.pace) if req.pace else None,
            "project": req.project,
            "data_root": str(root),
            "title": [line.strip() for line in req.title or [] if line.strip()] or None,
        }
        return m.ImportResult(project=project.slug, job=enqueue_run(project, options, edit))

    @app.post("/api/music/analyze", response_model=m.MusicInfo)
    def analyze_music_route(req: m.MusicRequest):
        from goodfeeling.media.fingerprint import fingerprint
        from goodfeeling.pipeline.music import analyze_music
        from goodfeeling.schemas.music import MusicAnalysis

        check = check_music(req.path)
        if not check.ok:
            raise HTTPException(422, check.message)
        path = Path(check.path)
        cached = root / "cache" / "music" / f"{fingerprint(path)}.json"
        with store.music_lock:
            if cached.exists():
                music = MusicAnalysis.model_validate_json(cached.read_text())
            else:
                try:
                    music = analyze_music(path)
                except Exception as exc:
                    raise HTTPException(422, f"Couldn't analyse that track: {exc}") from exc
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(music.model_dump_json(indent=2))
        return music_info(music, req.length)

    @app.get("/api/fs", response_model=m.FsListing)
    def list_dir(path: str = "~"):
        folder = Path(path).expanduser()
        if not folder.is_dir():
            folder = folder.parent if folder.parent.is_dir() else Path.home()
        folder = folder.resolve()
        entries = []
        try:
            children = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            children = []
        for child in children[:1000]:
            if child.name.startswith("."):
                continue
            ext = child.suffix.lower()
            kind = (
                "dir" if child.is_dir() else "video" if ext in VIDEO_EXT else "audio" if ext in AUDIO_EXT else "other"
            )
            entries.append(m.FsEntry(name=child.name, path=str(child), kind=kind))
        parent = str(folder.parent) if folder.parent != folder else None
        return m.FsListing(path=str(folder), parent=parent, entries=entries)

    # Jobs

    @app.get("/api/jobs", response_model=list[m.Job])
    def list_jobs(project: str | None = None, active: bool = False, limit: int = Query(50, le=500)):
        sql, params = "SELECT * FROM jobs WHERE 1=1", []
        if project:
            sql += " AND project_id=?"
            params.append(store.project(project).id)
        if active:
            sql += " AND status IN ('queued','running','approval')"
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [job_out(store, dict(r)) for r in store.db.execute(sql, tuple(params)).fetchall()]

    def require_job(job_id: int) -> dict[str, Any]:
        job = queue.get(store.db, job_id)
        if job is None:
            raise HTTPException(404, f"no job {job_id}")
        return job

    @app.get("/api/jobs/{job_id}", response_model=m.Job)
    def get_job(job_id: int):
        return job_out(store, require_job(job_id))

    @app.get("/api/jobs/{job_id}/log", response_model=list[m.JobEvent])
    def job_log(job_id: int, after: int = 0):
        require_job(job_id)
        return queue.events_after(store.db, job_id, after, limit=5000)

    @app.post("/api/jobs/{job_id}/approve", response_model=m.Job)
    def approve_job(job_id: int):
        require_job(job_id)
        if not queue.approve(store.db, job_id):
            raise HTTPException(409, "this job isn't waiting for approval")
        return job_out(store, require_job(job_id))

    @app.post("/api/jobs/{job_id}/cancel", response_model=m.Job)
    def cancel_job(job_id: int):
        require_job(job_id)
        queue.cancel(store.db, job_id)
        return job_out(store, require_job(job_id))

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: int, request: Request, after: int = 0):
        """Server-sent events: `log` for each progress line, `job` whenever the job's state changes."""
        require_job(job_id)
        last = int(request.headers.get("last-event-id") or after)

        async def stream():
            nonlocal last
            seen = None
            idle = 0.0
            while not app.state.stopping and not await request.is_disconnected():
                for event in queue.events_after(store.db, job_id, last):
                    last = event["id"]
                    yield f"id: {last}\nevent: log\ndata: {json.dumps(event)}\n\n"
                job = queue.get(store.db, job_id)
                if job["updated_at"] != seen:
                    seen = job["updated_at"]
                    yield f"event: job\ndata: {job_out(store, job).model_dump_json()}\n\n"
                if job["status"] in queue.FINISHED:
                    for event in queue.events_after(store.db, job_id, last):
                        last = event["id"]
                        yield f"id: {last}\nevent: log\ndata: {json.dumps(event)}\n\n"
                    break
                idle += 0.4
                if idle >= 15:
                    idle = 0.0
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.4)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # Settings

    @app.get("/api/settings", response_model=m.SettingsOut)
    def get_settings():
        return settings_out(load_settings(), root)

    @app.put("/api/settings", response_model=m.SettingsOut)
    def put_settings(update: m.SettingsUpdate):
        from goodfeeling.pipeline.pacing import parse_pace

        if os.environ.get("GOODFEELING_LOCK_SPEND") and (update.api_key is not None or update.max_cost_usd is not None):
            raise HTTPException(403, "the API key and spend limit are locked on this server")
        path = config_path()
        raw = json.loads(path.read_text()) if path.exists() else {}
        for key in ("models", "effort", "weights"):
            value = getattr(update, key)
            if value is not None:
                raw[key] = {**raw.get(key, {}), **value}
        for key in ("style_note", "max_cost_usd"):
            value = getattr(update, key)
            if value is not None:
                raw[key] = value
        if update.pace is not None:
            try:
                parse_pace(update.pace)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            raw["pace"] = update.pace
        if update.api_key is not None:
            if update.api_key.strip():
                raw["anthropic_api_key"] = update.api_key.strip()
            else:
                raw.pop("anthropic_api_key", None)
        try:
            settings = Settings.model_validate(raw)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(raw, indent=2) + "\n")
        os.replace(tmp, path)
        return settings_out(settings, root)

    # The web app

    if (WEB_DIST / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{rest:path}", include_in_schema=False)
        def spa(rest: str):
            if rest.startswith("api/"):
                raise HTTPException(404, "not found")
            candidate = (WEB_DIST / rest).resolve()
            if rest and WEB_DIST.resolve() in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(WEB_DIST / "index.html", headers={"Cache-Control": "no-cache"})
    else:

        @app.get("/", include_in_schema=False)
        def no_build():
            return HTMLResponse(
                f"<body style='background:#0b0b0c;color:#ddd;font:15px system-ui;padding:3rem'><h2>{DISPLAY_NAME}</h2>"
                "<p>The web app isn't built yet. Run <code>npm install && npm run build</code> in <code>web/</code>, "
                "or start <code>goodfeeling serve --dev</code>.</p></body>"
            )

    return app
