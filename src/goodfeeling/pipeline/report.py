"""Human-readable outputs: the director's plan as Markdown."""

from __future__ import annotations

from goodfeeling.ai.client import Meter
from goodfeeling.pipeline.direct import Context, DirectorResult, fps_float
from goodfeeling.timebase import format_timecode


def plan_markdown(result: DirectorResult, ctx: Context, meter: Meter | None = None) -> str:
    plan, edl = result.plan, result.edl
    timeline = edl.timeline()
    spb = ctx.seconds_per_beat
    total = edl.total_frames()
    lines = [
        f"# {plan.title or 'Edit plan'}",
        "",
        plan.concept,
        "",
        f"- Length: {format_timecode(total, edl.output.fps)} ({total} frames at "
        f"{fps_float(edl.output.fps):.3f} fps), {ctx.window.beats} beats at {ctx.music.tempo:.1f} BPM",
        f"- Style: {ctx.style}; output {edl.output.width}x{edl.output.height}",
        f"- Music window: from {ctx.music.beats[ctx.window.start_beat]:.2f} s of the track "
        f"(beat {ctx.window.start_beat}, an estimated downbeat)",
        f"- Planned by: {plan.source}"
        + (" (repaired by the deterministic fixer)" if result.fixed else "")
        + (" (deterministic selector)" if result.fallback and plan.source == "deterministic" else ""),
        "",
    ]
    if plan.order_note:
        lines += [f"**Order:** {plan.order_note}", ""]
    lines += ["## Music sections", ""]
    for i, s in enumerate(ctx.music.sections):
        a = s.start_beat - ctx.window.start_beat
        b = s.end_beat - ctx.window.start_beat
        if b <= 0 or a >= ctx.window.beats:
            continue
        pace = next((p.beats_per_cut for p in plan.pacing if p.section == i), None)
        lines.append(
            f"- {i}: **{s.label}**, edit beats {max(0, a)}-{min(ctx.window.beats, b)} "
            f"({max(0, a) * spb:.1f}-{min(ctx.window.beats, b) * spb:.1f} s)"
            + (f", {pace} beats per cut" if pace else "")
        )
    lines += [
        "",
        "## Shots",
        "",
        "| # | at | beats | segment | source | speed | join | score | role | reason |",
        "|---|----|------:|---------|--------|------:|------|------:|------|--------|",
    ]
    for entry, planned in zip(timeline, plan.shots, strict=False):
        shot = entry.shot
        src = edl.sources[shot.clip]
        lines.append(
            f"| {entry.index + 1} | {format_timecode(entry.start, edl.output.fps)} | {planned.beats} | "
            f"{planned.segment_id} | {shot.clip} {format_timecode(shot.in_, src.fps)}-"
            f"{format_timecode(shot.out, src.fps)} | {shot.constant_speed:g} | {shot.transition} | "
            f"{(shot.score or 0):.1f} | {planned.role} | {planned.reason.replace('|', '/')} |"
        )
    if plan.closing_note:
        lines += ["", f"**Ending:** {plan.closing_note}"]
    lines += ["", "## Validation", ""]
    if not result.rounds:
        lines.append("- Deterministic plan, validated once with no problems.")
    for i, problems in enumerate(result.rounds):
        label = "first plan" if i == 0 else f"repair {i}" if i <= 3 else "after fixer"
        lines.append(f"- {label}: " + ("valid" if not problems else f"{len(problems)} problem(s)"))
        lines += [f"  - {p}" for p in problems]
    if meter is not None and meter.calls:
        lines += [
            "",
            "## Claude usage",
            "",
            f"- {meter.calls} calls, {meter.input_tokens} input tokens, "
            f"{meter.output_tokens} output tokens, {meter.cache_read} cache-read tokens",
            f"- Cost this run: ${meter.cost:.4f} ("
            + ", ".join(f"{k} ${v:.4f}" for k, v in meter.by_step.items())
            + ")",
        ]
    return "\n".join(lines) + "\n"
