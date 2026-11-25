"""The engine's steps over a project store. The CLI (and later the server) call these."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from goodfeeling.ai.client import Claude, JsonlLogger, Transport, anthropic_transport
from goodfeeling.ai.pricing import cost_usd, image_tokens, text_tokens
from goodfeeling.ai.prompts import DIRECT_SYSTEM, DIRECT_TOOL, UNDERSTAND_PROMPT_VERSION, UNDERSTAND_SYSTEM
from goodfeeling.media.encoders import choose_encoder
from goodfeeling.media.frames import extract_frame
from goodfeeling.pipeline.direct import Context, choose_window, direct, eligible, plan_json
from goodfeeling.pipeline.ingest import ingest, load_clips
from goodfeeling.pipeline.music import analyze_music, apply_drop_override
from goodfeeling.pipeline.render import render
from goodfeeling.pipeline.report import plan_markdown
from goodfeeling.pipeline.score import Estimate, build_strip, cache_key, estimate_scoring, score_segments
from goodfeeling.pipeline.segment import blend, segment_project
from goodfeeling.pipeline.strips import contact_sheet
from goodfeeling.pipeline.understand import summarize, summary_markdown
from goodfeeling.schemas.clip import ClipInfo, FlightInfo
from goodfeeling.schemas.music import MusicAnalysis
from goodfeeling.schemas.segment import Segment
from goodfeeling.settings import Settings
from goodfeeling.store.project import Project
from goodfeeling.timebase import Rate, normalize_rate

log = logging.getLogger("goodfeeling")

RESOLUTIONS = {"1080": (1920, 1080), "4k": (3840, 2160)}


class CostRefused(RuntimeError):
    pass


@dataclass
class RunOptions:
    footage: Path
    music: Path | None = None
    length: float = 150.0
    style: str = "fast"
    res: str = "1080"
    project: str | None = None
    data_root: Path | None = None
    drop_at: float | None = None
    no_ai: bool = False
    fps: str | None = None
    render: bool = True


@dataclass
class RunResult:
    project: Project
    segments: list[Segment]
    outputs: dict[str, Path] = field(default_factory=dict)
    score_stats: dict[str, int] = field(default_factory=dict)
    estimate: list[Estimate] = field(default_factory=list)
    cost: float = 0.0


def setup_logging(project: Project) -> None:
    handler = logging.FileHandler(project.path("logs", "run.log"))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("goodfeeling")
    root.handlers = [h for h in root.handlers if not isinstance(h, logging.FileHandler)]
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def make_claude(settings: Settings, project: Project, transport: Transport | None, no_ai: bool) -> Claude | None:
    if no_ai:
        return None
    if transport is None:
        key = settings.api_key()
        if not key:
            return None
        transport = anthropic_transport(key)
    return Claude(transport, JsonlLogger(project.path("logs", "ai.jsonl"), project.db, project.id))


def analyze(project: Project, settings: Settings, progress: Callable[[str], None]):
    progress("Ingest")
    ingest(
        project,
        flight_gap_s=settings.flight_gap_s,
        stabilized_dir=settings.stabilized_dir,
        lut=settings.lut,
        progress=progress,
    )
    clips = load_clips(project)
    progress("Segment")
    segments = segment_project(project, clips, progress=progress)
    progress(f"  {len(segments)} segments from {len(clips)} clips")
    return clips, segments


def estimate_run(
    project: Project, settings: Settings, segments: list[Segment], clip_map: dict, *, directing: bool
) -> list[Estimate]:
    uncached = sum(
        1
        for s in segments
        if project.db.cached_score(cache_key(clip_map[s.clip][0].fingerprint, s, settings.models.score)) is None
    )
    estimates = [estimate_scoring(uncached, settings.models.score, settings.score_batch_size)]
    if directing:
        table = len(segments) * 45
        estimates.append(
            Estimate(
                "understand",
                settings.models.understand,
                1,
                text_tokens(UNDERSTAND_SYSTEM) + table + image_tokens(1300, 900) + 400,
                2500,
            )
        )
        direct_in = text_tokens(DIRECT_SYSTEM) + text_tokens(json.dumps(DIRECT_TOOL)) + table + 1200
        estimates.append(Estimate("direct", settings.models.direct, 1, direct_in, 9000))
    return estimates


def print_estimate(estimates: list[Estimate], progress, settings: Settings) -> tuple[float, float]:
    expected = sum(e.cost for e in estimates)
    direct = next((e for e in estimates if e.step == "direct"), None)
    worst = expected + (
        3 * cost_usd(direct.model, direct.input_tokens + direct.output_tokens, direct.output_tokens) if direct else 0.0
    )
    progress("Cost estimate (before any Claude call; prompt caching not assumed):")
    for e in estimates:
        progress(
            f"  {e.step:<10} {e.model:<20} {e.requests:>3} request(s)  ~{e.input_tokens:>7} in  "
            f"~{e.output_tokens:>6} out  ${e.cost:.3f}"
        )
    progress(
        f"  expected ${expected:.2f}; up to ${worst:.2f} if the director needs all 3 repair rounds "
        f"(limit ${settings.max_cost_usd:.2f})"
    )
    return expected, worst


def score_step(
    project: Project, settings: Settings, segments: list[Segment], clip_map: dict, claude: Claude | None, progress
) -> dict[str, int]:
    stats = score_segments(
        project,
        segments,
        clip_map,
        claude=claude,
        model=settings.models.score,
        effort=settings.effort.score,
        batch_size=settings.score_batch_size,
        progress=progress,
    )
    blend(segments, settings.weights)
    project.write_json("analysis/segments.json", [s.model_dump() for s in segments])
    return stats


def music_step(project: Project, music_path: Path, drop_at: float | None, progress) -> MusicAnalysis:
    from goodfeeling.media.fingerprint import fingerprint

    fp = fingerprint(music_path)
    existing = project.read_json("analysis/music.json")
    if existing and existing.get("fingerprint") == fp:
        music = MusicAnalysis.model_validate(existing)
        progress("Music (from analysis/music.json)")
    else:
        progress("Music")
        music = analyze_music(music_path)
        project.write_json("analysis/music.json", music.model_dump())
    if drop_at is not None:
        music = apply_drop_override(music, drop_at)
    progress(
        f"  {music.tempo:.1f} BPM, {len(music.beats)} beats, grid {music.grid}, "
        f"sections {', '.join(s.label for s in music.sections)}"
    )
    return music


def dominant_fps(clips: list[tuple[FlightInfo, ClipInfo, Path]]) -> Rate:
    weight: Counter = Counter()
    for _, clip, _ in clips:
        weight[clip.fps] += clip.duration_s
    return weight.most_common(1)[0][0]


def understand_step(
    project: Project, settings: Settings, segments: list[Segment], clip_map: dict, claude: Claude | None, progress
) -> dict:
    strips = {s.id: build_strip(project, s, clip_map[s.clip][0], clip_map[s.clip][1]) for s in segments}
    key_src = json.dumps([[s.id, s.start, s.end, s.claude.model_dump() if s.claude else None] for s in segments])
    key = hashlib.sha256(
        f"{key_src}|{settings.models.understand}|{UNDERSTAND_PROMPT_VERSION}|{claude is None}".encode()
    ).hexdigest()[:16]
    cached = project.read_json(f"analysis/summary-{key}.json")
    clips = {cid: info for cid, (info, _) in clip_map.items()}
    if cached:
        progress("Understand (cached)")
        return cached
    progress("Understand")
    summary, sheet = summarize(
        segments, clips, strips, claude=claude, model=settings.models.understand, effort=settings.effort.understand
    )
    if sheet is not None:
        sheet.save(project.path("frames", "session_sheet.jpg"), quality=85)
    project.write_json(f"analysis/summary-{key}.json", summary)
    return summary


def run(
    options: RunOptions,
    settings: Settings,
    *,
    transport: Transport | None = None,
    progress: Callable[[str], None] = print,
    confirm: Callable[[str], bool] | None = None,
) -> RunResult:
    data_root = settings.resolved_data_root(options.data_root)
    project = Project.open(data_root, options.footage, options.project)
    setup_logging(project)
    progress(f"Project '{project.slug}' in {project.root}")
    clips, segments = analyze(project, settings, progress)
    clip_map = {flight.clip_id: (info, proxy) for flight, info, proxy in clips}
    claude = make_claude(settings, project, transport, options.no_ai)
    result = RunResult(project=project, segments=segments)
    directing = options.music is not None
    if claude is None:
        progress("Claude: off (no ANTHROPIC_API_KEY or --no-ai); scores come from local signals only")
    else:
        result.estimate = estimate_run(project, settings, segments, clip_map, directing=directing)
        expected, worst = print_estimate(result.estimate, progress, settings)
        if worst > settings.max_cost_usd:
            raise CostRefused(
                f"estimate ${worst:.2f} exceeds the ${settings.max_cost_usd:.2f} limit "
                "(raise max_cost_usd in settings or pass --max-cost)"
            )
        if expected > 0 and confirm is not None and not confirm(f"Spend about ${expected:.2f} on Claude?"):
            raise CostRefused("cancelled before any Claude call")
    progress("Score")
    result.score_stats = score_step(project, settings, segments, clip_map, claude, progress)
    progress(f"  {result.score_stats}")
    if not directing:
        result.cost = claude.meter.cost if claude else 0.0
        return result

    music = music_step(project, options.music, options.drop_at, progress)
    usable = eligible(segments)
    summary = understand_step(project, settings, usable, clip_map, claude, progress)
    result.outputs["summary"] = project.write_json("analysis/summary.json", summary)
    summary_md = project.path("edits", "summary.md")
    summary_md.write_text(summary_markdown(summary))
    result.outputs["summary_md"] = summary_md

    out_fps = normalize_rate(options.fps) if options.fps else dominant_fps(clips)
    width, height = RESOLUTIONS[options.res]
    window = choose_window(music, out_fps, options.length)
    ctx = Context(
        segments={s.id: s for s in usable},
        clips={k: v[0] for k, v in clip_map.items()},
        flights={f.clip_id: f for f, _, _ in clips},
        music=music,
        window=window,
        out_fps=out_fps,
        width=width,
        height=height,
        style=options.style,
        target_s=options.length,
        style_note=settings.style_note,
        summary=summary_markdown(summary),
        lut=settings.lut,
    )
    progress(f"Direct ({window.beats} beats from track beat {window.start_beat}, {out_fps[0]}/{out_fps[1]} fps)")
    directed = direct(
        ctx, claude=claude, model=settings.models.direct, effort=settings.effort.direct, progress=progress
    )
    result.outputs["edl"] = project.save_edl(directed.edl, origin=directed.plan.source)
    result.outputs["plan_json"] = project.path("edits", "plan.json")
    result.outputs["plan_json"].write_text(plan_json(directed))
    plan_md = project.path("edits", "plan.md")
    plan_md.write_text(plan_markdown(directed, ctx, claude.meter if claude else None))
    result.outputs["plan_md"] = plan_md
    result.outputs["contact_sheet"] = write_shot_sheet(project, directed.edl, clip_map)

    if options.render:
        progress("Render")
        encoder = choose_encoder(settings.render, height)
        final = render(
            directed.edl,
            project.path("renders", "final.mp4"),
            encoder,
            project.shot_cache,
            audio_bitrate=settings.render.audio_bitrate,
            progress=progress,
        )
        result.outputs["final"] = final
    if claude is not None:
        plan_md.write_text(plan_markdown(directed, ctx, claude.meter))
        result.cost = claude.meter.cost
    return result


def write_shot_sheet(project: Project, edl, clip_map: dict) -> Path:
    by_fp = {info.fingerprint: (info, proxy) for info, proxy in clip_map.values()}
    items = []
    for entry in edl.timeline():
        shot = entry.shot
        info, proxy = by_fp[edl.sources[shot.clip].fingerprint]
        mid = (shot.in_ + shot.out) // 2
        items.append(
            (
                f"{entry.index + 1}. {shot.id} {shot.clip} {shot.score or 0:.1f}",
                extract_frame(proxy, mid, info.fps, 320),
            )
        )
    columns = min(6, max(1, math.ceil(math.sqrt(len(items)))))
    path = project.path("renders", "contact_sheet.jpg")
    contact_sheet(items, columns=columns, thumb_w=240).save(path, quality=85)
    return path
