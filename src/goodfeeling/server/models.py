"""Request and response shapes of the HTTP API. The web app's TypeScript types are generated from these."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EstimateStep(BaseModel):
    step: str
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    cost: float


class Estimate(BaseModel):
    steps: list[EstimateStep]
    expected: float
    worst: float
    limit: float


JobStatus = Literal["queued", "running", "approval", "done", "failed", "cancelled"]


class Job(BaseModel):
    id: int
    project: str | None
    kind: str
    edit: str | None
    status: JobStatus
    stage: str | None
    step_done: int | None
    step_total: int | None
    estimate: Estimate | None
    error: str | None
    attempts: int
    cost_usd: float
    created_at: float
    started_at: float | None
    finished_at: float | None
    updated_at: float
    result: dict[str, Any] | None


class JobEvent(BaseModel):
    id: int
    message: str
    created_at: float


class RunOptionsIn(BaseModel):
    length: float | None = Field(default=None, ge=4, le=1200)
    style: Literal["fast", "cinematic"] = "fast"
    res: Literal["1080", "4k"] = "1080"
    format: Literal["landscape", "reel"] = "landscape"
    pace: str | None = None
    music_start: str | None = None
    edit: str | None = None
    title: list[str] | None = Field(default=None, max_length=4)


class ImportRequest(RunOptionsIn):
    footage: str
    music: str
    project: str | None = None


class PathCheck(BaseModel):
    ok: bool
    path: str
    message: str
    videos: int = 0
    size_bytes: int = 0


class ImportCheck(BaseModel):
    footage: PathCheck
    music: PathCheck
    project_slug: str
    project_exists: bool
    edit: str


class ImportResult(BaseModel):
    project: str
    job: Job


class EditSummary(BaseModel):
    name: str
    format: str
    revision: int | None
    updated_at: float
    final_url: str | None
    contact_sheet_url: str | None
    duration_s: float | None
    options: dict[str, Any] | None


class ProjectSummary(BaseModel):
    slug: str
    name: str
    footage_dir: str
    created_at: float
    updated_at: float
    cost_usd: float
    clips: int
    segments: int
    edits: list[EditSummary]
    thumb_url: str | None
    active_job: Job | None


class Section(BaseModel):
    index: int
    label: str
    start_s: float
    end_s: float
    energy: float


class Stretch(BaseModel):
    why: str
    start_s: float
    end_s: float
    start: str


class MusicInfo(BaseModel):
    path: str
    name: str
    tempo: float
    duration_s: float
    beats: int
    grid: str
    sections: list[Section]
    energy: list[float]
    suggestions: list[Stretch]


class ProjectDetail(ProjectSummary):
    summary: dict[str, Any] | None
    music: MusicInfo | None
    session_sheet_url: str | None
    jobs: list[Job]


class PlanShot(BaseModel):
    index: int
    shot_id: str
    segment_id: str | None
    clip: str
    start_s: float
    end_s: float
    source_in_s: float
    source_out_s: float
    speed: float
    transition: str
    role: str
    reason: str
    score: float | None
    tags: list[str]
    description: str
    starred: bool
    thumb_url: str


class EditDetail(EditSummary):
    title: str
    concept: str
    order_note: str
    closing_note: str
    source: str
    fixed: bool
    fallback: bool
    validation_rounds: list[list[str]]
    music_start_s: float
    music_end_s: float
    width: int
    height: int
    fps: str
    shots: list[PlanShot]
    plan_md: str | None


class Choice(BaseModel):
    include: bool | None
    star: bool
    taste: Literal[-1, 0, 1]


class ChoiceUpdate(BaseModel):
    """Fields left out are unchanged. include: true forces a segment in, false keeps it out, null is automatic."""

    include: bool | None = None
    star: bool | None = None
    taste: Literal[-1, 0, 1] | None = None
    reset_include: bool = False


class SegmentOut(BaseModel):
    id: str
    clip: str
    flight: int
    kind: str
    start_s: float
    end_s: float
    duration_s: float
    proxy_url: str
    thumb_url: str
    score: float
    claude_score: float | None
    local_score: float
    tags: list[str]
    description: str
    reason: str
    flags: list[str]
    auto_rejected: str | None
    available: bool
    choice: Choice
    used_in: dict[str, int]
    moments: list[str]


class MomentOut(BaseModel):
    """A strong move found by joining adjacent segments and a second look; the director can hold it in full."""

    id: str
    clip: str
    flight: int
    kind: str
    hold: str
    worth: float
    start_s: float
    end_s: float
    duration_s: float
    peak_s: float | None
    description: str
    reason: str
    segment_ids: list[str]
    source: str
    proxy_url: str
    thumb_url: str
    available: bool
    blocked_by: list[str]
    choice: Choice
    used_in: dict[str, int]


class AiLogEntry(BaseModel):
    id: int
    step: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cost_usd: float
    created_at: float


class AiLogPage(BaseModel):
    total: int
    total_cost_usd: float
    entries: list[AiLogEntry]


class AiLogDetail(AiLogEntry):
    request: dict[str, Any]
    response: dict[str, Any]


class ApiKeyInfo(BaseModel):
    set: bool
    source: Literal["env", "settings"] | None
    hint: str | None


class SettingsOut(BaseModel):
    models: dict[str, str]
    effort: dict[str, str]
    weights: dict[str, float]
    style_note: str
    pace: str
    max_cost_usd: float
    api_key: ApiKeyInfo
    data_root: str
    config_path: str
    available_models: list[str]


class SettingsUpdate(BaseModel):
    models: dict[str, str] | None = None
    effort: dict[str, str] | None = None
    weights: dict[str, float] | None = None
    style_note: str | None = None
    pace: str | None = None
    max_cost_usd: float | None = Field(default=None, ge=0)
    api_key: str | None = Field(default=None, description="New key; an empty string removes it from the file.")


class FsEntry(BaseModel):
    name: str
    path: str
    kind: Literal["dir", "video", "audio", "other"]


class FsListing(BaseModel):
    path: str
    parent: str | None
    entries: list[FsEntry]


class MusicRequest(BaseModel):
    path: str
    length: float = Field(default=150, ge=4, le=1200)
