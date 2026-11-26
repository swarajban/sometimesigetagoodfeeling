"""The engine's steps over a project store. The CLI (and later the server) call these."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from goodfeeling.ai.client import Claude, JsonlLogger, Transport, anthropic_transport
from goodfeeling.ai.pricing import cost_usd, image_tokens, text_tokens
from goodfeeling.ai.prompts import DIRECT_SYSTEM, DIRECT_TOOL, UNDERSTAND_PROMPT_VERSION, UNDERSTAND_SYSTEM
from goodfeeling.media.encoders import choose_encoder
from goodfeeling.media.frames import extract_frame
from goodfeeling.pipeline.direct import Context, direct, eligible, extents, plan_json
from goodfeeling.pipeline.ingest import ingest, load_clips
from goodfeeling.pipeline.moments import cache_key as moment_key
from goodfeeling.pipeline.moments import candidate_limit, candidates, estimate_looks, find_moments, moment_segment
from goodfeeling.pipeline.music import analyze_music, apply_drop_override
from goodfeeling.pipeline.pacing import parse_pace
from goodfeeling.pipeline.render import render
from goodfeeling.pipeline.report import plan_markdown
from goodfeeling.pipeline.score import Estimate, build_strip, cache_key, estimate_scoring, score_segments
from goodfeeling.pipeline.segment import blend, segment_project
from goodfeeling.pipeline.stretch import choose_window
from goodfeeling.pipeline.strips import centre_weight, contact_sheet, crop_to
from goodfeeling.pipeline.understand import summarize, summary_markdown
from goodfeeling.schemas.clip import ClipInfo, FlightInfo
from goodfeeling.schemas.music import MusicAnalysis
from goodfeeling.schemas.segment import Segment
from goodfeeling.settings import Settings
from goodfeeling.store.project import DEFAULT_EDIT, Project, check_edit_name
from goodfeeling.timebase import Rate, normalize_rate, seconds_to_frames

log = logging.getLogger("goodfeeling")

RESOLUTIONS = {"1080": (1920, 1080), "4k": (3840, 2160)}
FORMATS = ("landscape", "reel")
DEFAULT_LENGTH = {"landscape": 150.0, "reel": 30.0}


def output_size(res: str, edit_format: str) -> tuple[int, int]:
    width, height = RESOLUTIONS[res]
    return (height, width) if edit_format == "reel" else (width, height)


class CostRefused(RuntimeError):
    pass


@dataclass
class RunOptions:
    footage: Path
    music: Path | None = None
    length: float | None = None
    style: str = "fast"
    format: str = "landscape"
    edit: str | None = None
    music_start: float | None = None
    music_end: float | None = None
    pace: float | None = None
    res: str = "1080"
    project: str | None = None
    data_root: Path | None = None
    drop_at: float | None = None
    no_ai: bool = False
    fps: str | None = None
    render: bool = True

    def to_json(self) -> dict:
        return {k: str(v) if isinstance(v, Path) else v for k, v in asdict(self).items()}

    @classmethod
    def from_json(cls, data: dict) -> RunOptions:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for key in ("footage", "music", "data_root"):
            if known.get(key) is not None:
                known[key] = Path(known[key])
        return cls(**known)


EstimateHook = Callable[[list[Estimate], float, float], None]


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
    return Claude(
        transport,
        JsonlLogger(project.path("logs", "ai.jsonl"), project.db, project.id),
        budget_usd=settings.max_cost_usd,
    )


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
        summary_cached = False
        infos = {cid: info for cid, (info, _) in clip_map.items()}
        looks = candidate_limit(infos)
        if not uncached:
            score_step(project, settings, segments, clip_map, None, lambda _: None)
            usable = eligible(segments)
            summary_cached = (
                project.read_json(f"analysis/summary-{summary_key(settings, usable, False)}.json") is not None
            )
            cands = candidates(usable, extents(segments), infos, looks)
            looks = sum(
                1
                for cand in cands
                if project.db.cached_moment(moment_key(infos[cand.clip].fingerprint, cand, settings.models.moments))
                is None
            )
        estimates.append(estimate_looks(looks, settings.models.moments))
        if not summary_cached:
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
    worst = expected
    if direct:
        for k in range(1, 4):
            grown = direct.input_tokens + k * (direct.output_tokens + 800)
            worst += cost_usd(direct.model, grown, direct.output_tokens)
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


def review_pool(
    segments: list[Segment], choices: dict[str, dict]
) -> tuple[list[Segment], set[str], set[str], set[str]]:
    """The pilot's review applied to segments: the automatic pick minus excluded segments plus included ones (even
    those the automatic filter rejected). Starred segments are always in. Returns the pool and the starred,
    disliked, and excluded ids; choices can name moments (m01...) as well as segments."""
    auto = {s.id for s in eligible(segments)}
    starred = {sid for sid, c in choices.items() if c.get("star")}
    disliked = {sid for sid, c in choices.items() if c.get("taste", 0) < 0}
    excluded = {sid for sid, c in choices.items() if c.get("include") is False and sid not in starred}
    pool = [
        s
        for s in segments
        if s.id in starred or (s.id not in excluded and (s.id in auto or choices.get(s.id, {}).get("include")))
    ]
    return pool, starred, disliked, excluded


def dominant_fps(clips: list[tuple[FlightInfo, ClipInfo, Path]]) -> Rate:
    weight: Counter = Counter()
    for _, clip, _ in clips:
        weight[clip.fps] += clip.duration_s
    return weight.most_common(1)[0][0]


def summary_key(settings: Settings, segments: list[Segment], offline: bool) -> str:
    key_src = json.dumps([[s.id, s.start, s.end, s.claude.model_dump() if s.claude else None] for s in segments])
    raw = f"{key_src}|{settings.models.understand}|{UNDERSTAND_PROMPT_VERSION}|{offline}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def understand_step(
    project: Project, settings: Settings, segments: list[Segment], clip_map: dict, claude: Claude | None, progress
) -> tuple[dict, dict[str, Path]]:
    strips = {s.id: build_strip(project, s, clip_map[s.clip][0], clip_map[s.clip][1]) for s in segments}
    key = summary_key(settings, segments, claude is None)
    cached = project.read_json(f"analysis/summary-{key}.json")
    if cached:
        progress("Understand (cached)")
        return cached, strips
    progress("Understand")
    clips = {cid: info for cid, (info, _) in clip_map.items()}
    summary, sheet = summarize(
        segments, clips, strips, claude=claude, model=settings.models.understand, effort=settings.effort.understand
    )
    if sheet is not None:
        sheet.save(project.path("frames", "session_sheet.jpg"), quality=85)
    project.write_json(f"analysis/summary-{key}.json", summary)
    return summary, strips


def run(
    options: RunOptions,
    settings: Settings,
    *,
    transport: Transport | None = None,
    progress: Callable[[str], None] = print,
    confirm: Callable[[str], bool] | None = None,
    estimate_hook: EstimateHook | None = None,
) -> RunResult:
    """`estimate_hook(estimates, expected, worst)` runs after the estimate and before any Claude call; it may raise to
    stop the run (the server's jobs use it to wait for the pilot's approval)."""
    if options.format not in FORMATS:
        raise ValueError(f"format must be one of {', '.join(FORMATS)}")
    edit = options.edit or ("reel" if options.format == "reel" else DEFAULT_EDIT)
    check_edit_name(edit)
    length = options.length if options.length is not None else DEFAULT_LENGTH[options.format]
    data_root = settings.resolved_data_root(options.data_root)
    project = Project.open(data_root, options.footage, options.project)
    setup_logging(project)
    progress(f"Project '{project.slug}' in {project.root}" + (f", edit '{edit}'" if edit != DEFAULT_EDIT else ""))
    clips, segments = analyze(project, settings, progress)
    clip_map = {flight.clip_id: (info, proxy) for flight, info, proxy in clips}
    claude = make_claude(settings, project, transport, options.no_ai)
    result = RunResult(project=project, segments=segments)
    directing = options.music is not None
    music = None
    if directing:
        music = music_step(project, options.music, options.drop_at, progress)
    out_fps = normalize_rate(options.fps) if options.fps else dominant_fps(clips)
    window = None
    if music is not None:
        if options.music_start is not None and options.music_end is not None and options.length is not None:
            progress("  note: --music-start and --music-end set the length, so --length is ignored")
        window, notes = choose_window(
            music,
            out_fps,
            length,
            start_s=options.music_start,
            end_s=options.music_end,
            prefer="peak" if options.format == "reel" else "drop",
        )
        for note in notes:
            progress(f"  note: {note}")
        length = window.beats * 60 / music.tempo
    if claude is None:
        progress("Claude: off (no ANTHROPIC_API_KEY or --no-ai); scores come from local signals only")
    else:
        result.estimate = estimate_run(project, settings, segments, clip_map, directing=directing)
        expected, worst = print_estimate(result.estimate, progress, settings)
        if estimate_hook is not None:
            estimate_hook(result.estimate, expected, worst)
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

    usable = eligible(segments)
    summary, strips = understand_step(project, settings, usable, clip_map, claude, progress)
    result.outputs["summary"] = project.write_json("analysis/summary.json", summary)
    summary_md = project.path("edits", "summary.md")
    summary_md.write_text(summary_markdown(summary))
    result.outputs["summary_md"] = summary_md

    progress("Moments")
    moments, moment_stats = find_moments(
        project,
        usable,
        extents(segments),
        clip_map,
        claude=claude,
        model=settings.models.moments,
        effort=settings.effort.moments,
        progress=progress,
    )
    progress(f"  {len(moments)} moments ({moment_stats})")
    choices = project.db.choices(project.id)
    pool, starred, disliked, excluded = review_pool(segments, choices)
    if choices:
        progress(
            f"  review: {len(pool)} segments available, {len(starred)} starred, {len(excluded)} excluded, "
            f"{len(disliked)} disliked"
        )
    extent = extents([s for s in segments if s.id not in excluded])
    by_id = {s.id: s for s in segments}
    pickable = {s.id: s for s in pool}
    for moment in moments:
        if moment.id in excluded or excluded.intersection(moment.segment_ids):
            continue
        seg = moment_segment(moment, by_id)
        pickable[seg.id] = seg
        lo, hi = extent.get(moment.segment_ids[0], (moment.start, moment.end))
        pad = seconds_to_frames(0.5, clip_map[moment.clip][0].fps)
        extent[seg.id] = (max(lo, moment.start - pad), min(hi, moment.end + pad))

    width, height = output_size(options.res, options.format)
    centre = {sid: centre_weight(path) for sid, path in strips.items()} if options.format == "reel" else {}
    for moment in moments:
        parts = [centre[s] for s in moment.segment_ids if s in centre]
        if parts:
            centre[moment.id] = sum(parts) / len(parts)
    ctx = Context(
        segments=pickable,
        clips={k: v[0] for k, v in clip_map.items()},
        flights={f.clip_id: f for f, _, _ in clips},
        music=music,
        window=window,
        out_fps=out_fps,
        width=width,
        height=height,
        style=options.style,
        target_s=length,
        style_note=settings.style_note,
        summary=summary_markdown(summary),
        lut=settings.lut,
        format=options.format,
        centre=centre,
        pace=options.pace if options.pace is not None else parse_pace(settings.pace),
        extent=extent,
        moments={m.id: m for m in moments if m.id in pickable},
        must_use=starred & set(pickable),
        disliked=disliked,
    )
    progress(
        f"Direct ({options.format}, pace {ctx.pace:g}, floor {ctx.rules.floor_s:.1f} s, "
        f"filler up to {ctx.rules.filler_s:.1f} s, "
        f"{window.beats} beats from {music.beats[window.start_beat]:.2f} s of the track, "
        f"{out_fps[0]}/{out_fps[1]} fps)"
    )
    directed = direct(
        ctx, claude=claude, model=settings.models.direct, effort=settings.effort.direct, progress=progress
    )
    result.outputs["edl"] = project.save_edl(
        directed.edl, origin=directed.plan.source, edit=edit, edit_format=options.format
    )
    project.db.execute(
        "UPDATE edits SET options_json=? WHERE project_id=? AND name=?",
        (json.dumps(options.to_json()), project.id, edit),
    )
    result.outputs["plan_json"] = project.edit_path(edit, "edits", "plan.json")
    result.outputs["plan_json"].write_text(plan_json(directed))
    plan_md = project.edit_path(edit, "edits", "plan.md")
    plan_md.write_text(plan_markdown(directed, ctx, claude.meter if claude else None))
    result.outputs["plan_md"] = plan_md
    result.outputs["contact_sheet"] = write_shot_sheet(
        project, directed.edl, clip_map, project.edit_path(edit, "renders", "contact_sheet.jpg")
    )

    if options.render:
        progress("Render")
        encoder = choose_encoder(settings.render, min(width, height))
        final = render(
            directed.edl,
            project.edit_path(edit, "renders", "final.mp4"),
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


def write_shot_sheet(project: Project, edl, clip_map: dict, path: Path) -> Path:
    """Middle frame of every shot, framed the way the output crops it."""
    by_fp = {info.fingerprint: (info, proxy) for info, proxy in clip_map.values()}
    portrait = edl.output.height > edl.output.width
    items = []
    for entry in edl.timeline():
        shot = entry.shot
        info, proxy = by_fp[edl.sources[shot.clip].fingerprint]
        frame = extract_frame(proxy, (shot.in_ + shot.out) // 2, info.fps, 640 if portrait else 320)
        caption = (
            f"{entry.index + 1}. {shot.id} {shot.score or 0:.1f}"
            if portrait
            else (f"{entry.index + 1}. {shot.id} {shot.clip} {shot.score or 0:.1f}")
        )
        items.append((caption, crop_to(frame, edl.output.width, edl.output.height, shot.crop_x)))
    columns = min(8 if portrait else 6, max(1, math.ceil(math.sqrt(len(items) * (2 if portrait else 1)))))
    thumb = 150 if portrait else 240
    path.parent.mkdir(parents=True, exist_ok=True)
    contact_sheet(items, columns=columns, thumb_w=thumb).save(path, quality=85)
    return path
