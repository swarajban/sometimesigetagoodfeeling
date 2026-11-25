"""Direct: Claude plans the edit in beats; code builds frames, validates, repairs, or falls back.

The deterministic selector here is also the no-API-key path and what the tests run.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field

from goodfeeling.ai.client import Claude, tool_result
from goodfeeling.ai.prompts import DIRECT_SYSTEM, DIRECT_TOOL
from goodfeeling.pipeline.render import render_problems
from goodfeeling.pipeline.signals import hamming
from goodfeeling.schemas.clip import ClipInfo, FlightInfo
from goodfeeling.schemas.edl import EDL, MusicRef, Output, Shot, Source
from goodfeeling.schemas.music import MusicAnalysis
from goodfeeling.schemas.plan import EditPlan, PlannedShot
from goodfeeling.schemas.segment import Segment
from goodfeeling.timebase import (
    Rate,
    as_fraction,
    format_timecode,
    nearest_source_frames,
    seconds_to_frames,
    source_per_output,
)

log = logging.getLogger(__name__)

MAX_REPAIR_ROUNDS = 3
HIGH_SCORE_EDGE = 7.0
DUPLICATE_HAMMING = 12
DUPLICATE_GAP_S = 2.0
PACING = {
    "fast": {"intro": 4, "build": 2, "drop": 2, "main": 2, "outro": 4},
    "cinematic": {"intro": 8, "build": 4, "drop": 4, "main": 4, "outro": 8},
}


@dataclass
class EditWindow:
    start_beat: int
    beats: int
    start_frame: int
    frames: list[int]

    def frame_of(self, beat: int) -> int:
        return self.frames[beat]


@dataclass
class Context:
    segments: dict[str, Segment]
    clips: dict[str, ClipInfo]
    flights: dict[str, FlightInfo]
    music: MusicAnalysis
    window: EditWindow
    out_fps: Rate
    width: int
    height: int
    style: str
    target_s: float
    style_note: str = ""
    summary: str = ""
    transition_frames: int = 8
    lut: str | None = None

    @property
    def seconds_per_beat(self) -> float:
        return 60.0 / self.music.tempo

    def chrono_key(self, seg: Segment) -> tuple:
        flight = self.flights[seg.clip]
        return (flight.flight, seg.clip, seg.start)

    def section_label(self, edit_beat: int) -> str:
        section = self.music.section_at(self.window.start_beat + edit_beat)
        return section.label if section else "main"


@dataclass
class Violation:
    shot: int | None
    code: str
    message: str

    def __str__(self) -> str:
        where = f"shot {self.shot + 1}" if self.shot is not None else "plan"
        return f"{where}: [{self.code}] {self.message}"


@dataclass
class DirectorResult:
    plan: EditPlan
    edl: EDL
    rounds: list[list[str]] = field(default_factory=list)
    fixed: bool = False
    fallback: bool = False


def choose_window(music: MusicAnalysis, out_fps: Rate, target_s: float) -> EditWindow:
    """Pick the stretch of the song to cut to: start on a downbeat, end on a bar, keep the drop inside."""
    beats = music.beats
    spb = 60.0 / music.tempo
    downbeats = music.downbeats() or [0]
    target_beats = max(4, int(round(target_s / spb / 4)) * 4)
    usable = len(beats) - 1
    start = downbeats[0]
    if music.drop_beat is not None and beats[music.drop_beat] - beats[start] > 0.85 * target_s:
        wanted = beats[music.drop_beat] - 0.6 * target_s
        latest = beats[-1] - target_s
        wanted = max(0.0, min(wanted, latest))
        start = min(downbeats, key=lambda i: abs(beats[i] - wanted))
    count = min(target_beats, usable - start)
    count -= count % 4 if count > 4 else 0
    start_frame = seconds_to_frames(beats[start], out_fps)
    frames = [seconds_to_frames(beats[start + k], out_fps) - start_frame for k in range(count + 1)]
    return EditWindow(start_beat=start, beats=count, start_frame=start_frame, frames=frames)


def eligible(segments: list[Segment]) -> list[Segment]:
    """Takeoff and landing segments survive only if they score highly."""
    return [s for s in segments if s.kind == "flight" or s.final_score >= HIGH_SCORE_EDGE]


def max_slowmo(clip: ClipInfo, out_fps: Rate) -> float:
    ratio = float(as_fraction(clip.fps) / as_fraction(out_fps))
    return 1.0 if ratio < 1.9 else round(1.0 / math.floor(ratio + 0.05), 3)


def fps_float(fps: Rate) -> float:
    return fps[0] / fps[1]


def build_edl(ctx: Context, plan: EditPlan) -> tuple[EDL | None, list[Violation]]:
    violations: list[Violation] = []
    shots: list[Shot] = []
    used: dict[str, int] = {}
    beat_starts: list[int] = []
    beat = 0
    position = 0
    for i, planned in enumerate(plan.shots):
        seg = ctx.segments.get(planned.segment_id)
        if seg is None:
            violations.append(Violation(i, "unknown_segment", f"segment {planned.segment_id!r} does not exist"))
            continue
        if planned.beats < 1:
            violations.append(Violation(i, "beats", "a shot must last at least 1 beat"))
            continue
        if planned.segment_id in used:
            violations.append(
                Violation(i, "reused", f"{planned.segment_id} is already used by shot {used[planned.segment_id] + 1}")
            )
        used.setdefault(planned.segment_id, i)
        clip = ctx.clips[seg.clip]
        speed = planned.speed
        if speed <= 0 or speed > 4:
            violations.append(Violation(i, "speed", f"speed {speed} is outside 0.25-4"))
            speed = 1.0
        if speed < 1.0 and speed + 1e-6 < max_slowmo(clip, ctx.out_fps):
            violations.append(
                Violation(
                    i,
                    "slowmo",
                    f"{seg.id} is {fps_float(clip.fps):.2f} fps; slowest speed is {max_slowmo(clip, ctx.out_fps)}",
                )
            )
            speed = 1.0
        end_beat = beat + planned.beats
        if end_beat > ctx.window.beats:
            violations.append(Violation(i, "too_long", f"plan runs past the edit's {ctx.window.beats} beats"))
            break
        need, length = nearest_source_frames(ctx.window.frame_of(end_beat) - position, clip.fps, ctx.out_fps, speed)
        if need > seg.frames:
            have = seg.frames / fps_float(clip.fps)
            violations.append(
                Violation(
                    i,
                    "too_short",
                    f"{seg.id} has {have:.2f} s but {planned.beats} beats at "
                    f"speed {speed} need {need / fps_float(clip.fps):.2f} s",
                )
            )
        start = seg.start + seconds_to_frames(max(0.0, planned.start_offset_s), clip.fps)
        start = max(seg.start, min(start, seg.end - need))
        if not planned.reason.strip():
            violations.append(Violation(i, "reason", "every shot needs a reason"))
        transition = planned.transition if shots else "cut"
        beat_starts.append(ctx.window.frame_of(beat))
        position += length
        shots.append(
            Shot(
                id=f"s{len(shots) + 1:02d}",
                clip=seg.clip,
                in_=start,
                out=start + need,
                speed=[(0, speed)],
                transition=transition,
                transition_frames=ctx.transition_frames,
                score=seg.final_score,
                tags=seg.claude.tags if seg.claude else [],
                description=seg.claude.description if seg.claude else "",
                reason=planned.reason,
            )
        )
        beat = end_beat
    tolerance = max(2, round(0.05 * ctx.window.beats))
    if abs(beat - ctx.window.beats) > tolerance:
        violations.append(
            Violation(None, "length", f"plan totals {beat} beats; target is {ctx.window.beats} (tolerance {tolerance})")
        )
    if not shots:
        return None, violations or [Violation(None, "empty", "plan has no shots")]
    edl = make_edl(ctx, shots)
    violations += check_edl(ctx, edl, plan, beat_starts)
    return edl, violations


def make_edl(ctx: Context, shots: list[Shot]) -> EDL:
    used = {shot.clip for shot in shots}
    sources = {
        cid: Source(
            path=ctx.clips[cid].path,
            fingerprint=ctx.clips[cid].fingerprint,
            fps=ctx.clips[cid].fps,
            frames=ctx.clips[cid].frames,
            stabilized_path=ctx.flights[cid].stabilized_path,
            lut=ctx.lut,
        )
        for cid in sorted(used)
    }
    fps = fps_float(ctx.out_fps)
    starts_mid_song = ctx.music.beats[ctx.window.start_beat] > 0.5
    end_beat = ctx.window.start_beat + ctx.window.beats
    ends_with_song = end_beat >= len(ctx.music.beats) - 2
    music = MusicRef(
        path=ctx.music.path,
        fingerprint=ctx.music.fingerprint,
        start_frame=ctx.window.start_frame,
        fade_in_frames=round(fps * (0.5 if starts_mid_song else 0.1)),
        fade_out_frames=round(fps * (1.0 if ends_with_song else 2.0)),
    )
    return EDL(
        output=Output(width=ctx.width, height=ctx.height, fps=ctx.out_fps), music=music, sources=sources, shots=shots
    )


def check_edl(ctx: Context, edl: EDL, plan: EditPlan, beat_starts: list[int]) -> list[Violation]:
    """Beat landing plus the render-time rules.

    A cut may sit off its beat only when the shot before it comes from footage slower than the output, whose whole
    source frames cannot produce every length; the next shot absorbs the difference, so it never accumulates.
    """
    violations = []
    timeline = edl.timeline()
    for entry, wanted in zip(timeline, beat_starts, strict=False):
        allowed = 0
        if entry.index > 0:
            prev = timeline[entry.index - 1].shot
            ratio = source_per_output(edl.sources[prev.clip].fps, ctx.out_fps, prev.constant_speed)
            allowed = 0 if ratio >= 1 else math.ceil(1 / (2 * ratio))
        if abs(entry.start - wanted) > allowed:
            violations.append(Violation(entry.index, "off_beat", f"starts at frame {entry.start}, beat is {wanted}"))
    violations += [Violation(index, "crossfade", message) for index, message in render_problems(edl)]
    by_seg = {}
    for i, planned in enumerate(plan.shots):
        if planned.segment_id in ctx.segments:
            by_seg.setdefault(i, ctx.segments[planned.segment_id])
    items = sorted(by_seg.items())
    for a in range(len(items)):
        for b in range(a + 1, len(items)):
            (ia, sa), (ib, sb) = items[a], items[b]
            if sa.id == sb.id or sa.clip != sb.clip:
                continue
            gap = max(sa.start, sb.start) - min(sa.end, sb.end)
            if (
                gap <= DUPLICATE_GAP_S * fps_float(ctx.clips[sa.clip].fps)
                and hamming(sa.phash, sb.phash) <= DUPLICATE_HAMMING
            ):
                violations.append(
                    Violation(ib, "near_duplicate", f"{sb.id} looks like {sa.id} (shot {ia + 1}), adjacent in time")
                )
    keys = [ctx.chrono_key(seg) for _, seg in items]
    backwards = sum(1 for x, y in zip(keys, keys[1:], strict=False) if y < x)
    allowed = max(1, len(keys) // 5)
    if backwards > allowed and not plan.order_note.strip():
        violations.append(
            Violation(None, "order", f"{backwards} backward jumps in time with no order_note (allowed {allowed})")
        )
    return violations


def check_speed_keyframes(edl: EDL) -> list[str]:
    return [
        f"shot {s.id}: speed ramps (more than one keyframe) arrive in phase 3" for s in edl.shots if len(s.speed) != 1
    ]


def pacing_for(style: str, label: str) -> int:
    return PACING.get(style, PACING["fast"]).get(label, 2)


def slots(ctx: Context) -> list[tuple[int, int, str]]:
    """(start_beat, beats, section label) for each cut, following section pacing."""
    result = []
    beat = 0
    while beat < ctx.window.beats:
        label = ctx.section_label(beat)
        length = min(pacing_for(ctx.style, label), ctx.window.beats - beat)
        result.append((beat, length, label))
        beat += length
    if len(result) > 1 and result[-1][1] < pacing_for(ctx.style, result[-1][2]) / 2:
        last = result.pop()
        prev = result.pop()
        result.append((prev[0], prev[1] + last[1], prev[2]))
    return result


def _need_seconds(ctx: Context, beats: int, clip: ClipInfo) -> float:
    return beats * ctx.seconds_per_beat + 2 * ctx.transition_frames / fps_float(ctx.out_fps)


def _pick(ctx: Context, pool: list[Segment], count: int, beats: int) -> list[Segment]:
    chosen: list[Segment] = []
    for seg in pool:
        if len(chosen) == count:
            break
        clip = ctx.clips[seg.clip]
        if seg.frames / fps_float(clip.fps) < _need_seconds(ctx, beats, clip):
            continue
        if any(
            c.clip == seg.clip
            and hamming(c.phash, seg.phash) <= DUPLICATE_HAMMING
            and max(c.start, seg.start) - min(c.end, seg.end) <= DUPLICATE_GAP_S * fps_float(clip.fps)
            for c in chosen
        ):
            continue
        chosen.append(seg)
    return chosen


def _merge_shortest(plan_slots: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Join the shortest neighbouring pair of cuts, leaving the drop's pace alone when possible."""
    best = min(
        range(len(plan_slots) - 1),
        key=lambda i: (plan_slots[i][2] == "drop", plan_slots[i][1] + plan_slots[i + 1][1], i),
    )
    a, b = plan_slots[best], plan_slots[best + 1]
    return [*plan_slots[:best], (a[0], a[1] + b[1], a[2]), *plan_slots[best + 2 :]]


def select(ctx: Context) -> EditPlan:
    """Deterministic plan: best segments, chronological, strongest ones moved onto the drop."""
    plan_slots = slots(ctx)
    pool = sorted(ctx.segments.values(), key=lambda s: (-s.final_score, ctx.chrono_key(s)))
    clean = [s for s in pool if not s.signals.crash and not (s.claude and s.claude.penalties.crash)]
    pool = clean + [s for s in pool if s not in clean]
    while True:
        chosen = _pick(ctx, pool, len(plan_slots), max(b for _, b, _ in plan_slots))
        if len(chosen) >= len(plan_slots) or len(plan_slots) == 1:
            break
        plan_slots = _merge_shortest(plan_slots)
    if len(chosen) < len(plan_slots):
        raise RuntimeError(f"only {len(chosen)} usable segment(s) for {len(plan_slots)} shots; shorten --length")
    chosen = chosen[: len(plan_slots)]
    chosen.sort(key=ctx.chrono_key)
    drop_positions = [i for i, (_, _, label) in enumerate(plan_slots) if label == "drop"]
    order_note = ""
    if drop_positions:
        ranked = sorted(range(len(chosen)), key=lambda i: -chosen[i].final_score)[: len(drop_positions)]
        stars = [chosen[i] for i in sorted(ranked)]
        rest = [s for i, s in enumerate(chosen) if i not in ranked]
        ordered: list[Segment] = []
        for i in range(len(plan_slots)):
            ordered.append(stars.pop(0) if i in drop_positions else rest.pop(0))
        if ordered != chosen:
            order_note = "The highest-scoring shots are moved onto the drop; everything else stays chronological."
        chosen = ordered
    shots = []
    for i, ((_, beats, label), seg) in enumerate(zip(plan_slots, chosen, strict=True)):
        clip = ctx.clips[seg.clip]
        need = beats * ctx.seconds_per_beat
        offset = max(0.0, (seg.frames / fps_float(clip.fps) - need) / 2)
        role = (
            "opening"
            if i == 0
            else "closing"
            if i == len(plan_slots) - 1
            else ("peak" if label == "drop" else "build" if label == "build" else "support")
        )
        boundary = i > 0 and plan_slots[i - 1][2] != label
        transition = "crossfade" if (ctx.style == "cinematic" and boundary) or label == "outro" and boundary else "cut"
        tags = ", ".join(seg.claude.tags[:3]) if seg.claude and seg.claude.tags else seg.kind
        shots.append(
            PlannedShot(
                segment_id=seg.id,
                beats=beats,
                start_offset_s=round(offset, 3),
                speed=1.0,
                transition=transition,
                role=role,
                reason=f"Score {seg.final_score:.1f} ({tags}); {beats} beats in the {label}.",
            )
        )
    pacing = []
    for index, section in enumerate(ctx.music.sections):
        pacing.append({"section": index, "label": section.label, "beats_per_cut": pacing_for(ctx.style, section.label)})
    return EditPlan(
        title=f"{ctx.style.title()} cut",
        concept="Deterministic selection: best-scoring segments in "
        "flight order, cut to the section pacing, with the strongest moments on the drop.",
        pacing=pacing,
        shots=shots,
        order_note=order_note,
        closing_note="Ends on the last chosen segment as the music fades.",
        source="deterministic",
    )


def fix(ctx: Context, plan: EditPlan) -> EditPlan:
    """Deterministic fixer: keep what is valid in Claude's plan, then patch length and order."""
    kept: list[PlannedShot] = []
    seen: set[str] = set()
    for planned in plan.shots:
        seg = ctx.segments.get(planned.segment_id)
        if seg is None or planned.segment_id in seen or planned.beats < 1:
            continue
        clip = ctx.clips[seg.clip]
        if any(
            ctx.segments[k.segment_id].clip == seg.clip
            and hamming(ctx.segments[k.segment_id].phash, seg.phash) <= DUPLICATE_HAMMING
            and max(ctx.segments[k.segment_id].start, seg.start) - min(ctx.segments[k.segment_id].end, seg.end)
            <= DUPLICATE_GAP_S * fps_float(clip.fps)
            for k in kept
        ):
            continue
        speed = planned.speed if 0 < planned.speed <= 4 and planned.speed >= max_slowmo(clip, ctx.out_fps) else 1.0
        beats = planned.beats
        while beats > 1 and seg.frames / fps_float(clip.fps) < beats * ctx.seconds_per_beat * speed:
            beats -= 1
        if seg.frames / fps_float(clip.fps) < beats * ctx.seconds_per_beat * speed:
            continue
        kept.append(
            planned.model_copy(
                update={"beats": beats, "speed": speed, "reason": planned.reason or "Kept from the director's plan."}
            )
        )
        seen.add(planned.segment_id)
    total = sum(p.beats for p in kept)
    while total > ctx.window.beats and kept:
        longest = max(range(len(kept)), key=lambda i: kept[i].beats)
        if kept[longest].beats > 1:
            kept[longest] = kept[longest].model_copy(update={"beats": kept[longest].beats - 1})
        else:
            kept.pop(min(range(len(kept)), key=lambda i: ctx.segments[kept[i].segment_id].final_score))
        total = sum(p.beats for p in kept)
    spares = [s for s in sorted(ctx.segments.values(), key=lambda s: -s.final_score) if s.id not in seen]
    while total < ctx.window.beats and spares:
        label = ctx.section_label(total)
        beats = min(pacing_for(ctx.style, label), ctx.window.beats - total)
        seg = next(
            (
                s
                for s in spares
                if s.frames / fps_float(ctx.clips[s.clip].fps) >= _need_seconds(ctx, beats, ctx.clips[s.clip])
            ),
            None,
        )
        if seg is None:
            break
        spares.remove(seg)
        kept.append(
            PlannedShot(
                segment_id=seg.id,
                beats=beats,
                start_offset_s=0.0,
                speed=1.0,
                transition="cut",
                role="support",
                reason=f"Added by the fixer to reach length (score {seg.final_score:.1f}).",
            )
        )
        total += beats
    if not plan.order_note.strip():
        positions = [p.beats for p in kept]
        kept.sort(key=lambda p: ctx.chrono_key(ctx.segments[p.segment_id]))
        kept = [p.model_copy(update={"beats": b}) for p, b in zip(kept, positions, strict=True)]
        for i, p in enumerate(kept):
            seg = ctx.segments[p.segment_id]
            if seg.frames / fps_float(ctx.clips[seg.clip].fps) < p.beats * ctx.seconds_per_beat * p.speed:
                kept[i] = p.model_copy(update={"speed": 1.0})
    for i, p in enumerate(kept):
        if p.transition == "crossfade" and i > 0:
            continue
        kept[i] = p.model_copy(update={"transition": "cut" if i == 0 else p.transition})
    return plan.model_copy(update={"shots": kept, "source": f"{plan.source}+fixer"})


def director_prompt(ctx: Context, segments_table: str) -> str:
    spb = ctx.seconds_per_beat
    lines = [
        f"Target: {ctx.window.beats} beats = {ctx.window.beats * spb:.1f} s at {ctx.music.tempo:.1f} BPM "
        f"(one beat = {spb:.3f} s). Requested length {ctx.target_s:.0f} s.",
        f"Style: {ctx.style}. Output {ctx.width}x{ctx.height} at {fps_float(ctx.out_fps):.3f} fps.",
        f"Pilot's style note: {ctx.style_note or '(none)'}",
        "",
        "Music sections inside the edit window (beats relative to the edit start):",
    ]
    for index, section in enumerate(ctx.music.sections):
        a = max(0, section.start_beat - ctx.window.start_beat)
        b = min(ctx.window.beats, section.end_beat - ctx.window.start_beat)
        if b <= 0 or a >= ctx.window.beats:
            continue
        lines.append(
            f"  section {index}: {section.label}, beats {a}-{b} ({a * spb:.1f}-{b * spb:.1f} s), "
            f"energy {section.energy:.2f}"
        )
    if ctx.music.drop_beat is not None:
        lines.append(
            f"Estimated drop at edit beat {ctx.music.drop_beat - ctx.window.start_beat} "
            "(section labels are an energy heuristic)."
        )
    lines += [
        "",
        "Session summary:",
        ctx.summary or "(none)",
        "",
        "Segments (chronological). usable_s is the most a shot can use; max_slowmo is the slowest speed allowed.",
        segments_table,
        "",
        "Plan the edit and call submit_edit_plan.",
    ]
    return "\n".join(lines)


def segments_for_director(ctx: Context) -> str:
    lines = ["id | flight | clip@time | usable_s | score | max_slowmo | flags | tags | description"]
    for seg in sorted(ctx.segments.values(), key=ctx.chrono_key):
        clip = ctx.clips[seg.clip]
        flags = [
            name
            for name, on in (
                ("crash", seg.signals.crash or (seg.claude and seg.claude.penalties.crash)),
                ("blur", seg.signals.blur or (seg.claude and seg.claude.penalties.blur)),
                ("props", seg.claude and seg.claude.penalties.props_in_view),
                ("horizon", seg.claude and seg.claude.penalties.horizon_chaos),
                (seg.kind, seg.kind != "flight"),
            )
            if on
        ]
        tags = ",".join(seg.claude.tags) if seg.claude else ""
        desc = seg.claude.description if seg.claude else "(local signals only)"
        lines.append(
            f"{seg.id} | {seg.flight} | {seg.clip}@{format_timecode(seg.start, clip.fps)} | "
            f"{seg.frames / fps_float(clip.fps):.2f} | {seg.final_score:.1f} | {max_slowmo(clip, ctx.out_fps)} | "
            f"{','.join(flags) or '-'} | {tags} | {desc}"
        )
    return "\n".join(lines)


def plan_from_tool(data: dict, source: str = "claude") -> EditPlan:
    return EditPlan.model_validate(
        {
            **data,
            "source": source,
            "pacing": [
                {"section": p.get("section", 0), "beats_per_cut": p.get("beats_per_cut", 1)}
                for p in data.get("pacing", [])
            ],
        }
    )


def direct(ctx: Context, *, claude: Claude | None, model: str, effort: str, progress=print) -> DirectorResult:
    rounds: list[list[str]] = []
    if claude is None:
        plan = select(ctx)
        edl, violations = build_edl(ctx, plan)
        if violations or edl is None:
            raise RuntimeError("deterministic plan failed validation: " + "; ".join(map(str, violations)))
        return DirectorResult(plan=plan, edl=edl, rounds=rounds, fallback=True)
    messages = [{"role": "user", "content": director_prompt(ctx, segments_for_director(ctx))}]
    call, conversation = claude.call_tool(
        step="direct",
        model=model,
        system=DIRECT_SYSTEM,
        messages=messages,
        tool=DIRECT_TOOL,
        effort=effort,
        max_tokens=32000,
    )
    plan = plan_from_tool(call.input)
    for round_index in range(MAX_REPAIR_ROUNDS + 1):
        edl, violations = build_edl(ctx, plan)
        rounds.append([str(v) for v in violations])
        if not violations and edl is not None:
            return DirectorResult(plan=plan, edl=edl, rounds=rounds)
        if round_index == MAX_REPAIR_ROUNDS:
            break
        progress(f"  plan has {len(violations)} problem(s); asking for repair {round_index + 1}/{MAX_REPAIR_ROUNDS}")
        tool_uses = [b for b in conversation[-1]["content"] if b.get("type") == "tool_use"]
        feedback = (
            "The plan failed validation. Fix every problem below and resubmit the complete plan with "
            "submit_edit_plan.\n" + "\n".join(f"- {v}" for v in violations)
        )
        results = [tool_result(t["id"], feedback, is_error=True) for t in tool_uses]
        conversation.append({"role": "user", "content": results})
        call, conversation = claude.call_tool(
            step="repair",
            model=model,
            system=DIRECT_SYSTEM,
            messages=conversation,
            tool=DIRECT_TOOL,
            effort=effort,
            max_tokens=32000,
        )
        plan = plan_from_tool(call.input)
    progress("  repairs exhausted; the deterministic fixer finishes the plan")
    fixed = fix(ctx, plan)
    edl, violations = build_edl(ctx, fixed)
    rounds.append([str(v) for v in violations])
    if not violations and edl is not None:
        return DirectorResult(plan=fixed, edl=edl, rounds=rounds, fixed=True)
    progress("  fixer could not satisfy every rule; using the deterministic selector")
    plan = select(ctx)
    edl, violations = build_edl(ctx, plan)
    if violations or edl is None:
        raise RuntimeError("deterministic plan failed validation: " + "; ".join(map(str, violations)))
    return DirectorResult(plan=plan, edl=edl, rounds=rounds, fixed=True, fallback=True)


def plan_json(result: DirectorResult) -> str:
    return (
        json.dumps(
            {
                "plan": result.plan.model_dump(),
                "validation_rounds": result.rounds,
                "fixed": result.fixed,
                "fallback": result.fallback,
            },
            indent=2,
        )
        + "\n"
    )
