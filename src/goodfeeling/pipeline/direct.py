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
from goodfeeling.pipeline.pacing import Pacing, beats_for, floor_beats, pacing_for, pacing_problems
from goodfeeling.pipeline.render import render_problems
from goodfeeling.pipeline.signals import hamming
from goodfeeling.pipeline.stretch import EditWindow
from goodfeeling.schemas.clip import ClipInfo, FlightInfo
from goodfeeling.schemas.edl import EDL, MusicRef, Output, Shot, Source
from goodfeeling.schemas.music import MusicAnalysis, MusicSection
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
    format: str = "landscape"
    centre: dict[str, float] = field(default_factory=dict)
    pace: float = 1.0
    extent: dict[str, tuple[int, int]] = field(default_factory=dict)
    must_use: set[str] = field(default_factory=set)
    disliked: set[str] = field(default_factory=set)

    @property
    def seconds_per_beat(self) -> float:
        return 60.0 / self.music.tempo

    def chrono_key(self, seg: Segment) -> tuple:
        flight = self.flights[seg.clip]
        return (flight.flight, seg.clip, seg.start)

    @property
    def reel(self) -> bool:
        return self.format == "reel"

    def in_window(self) -> list[tuple[int, int, MusicSection]]:
        """Sections overlapping the edit, as (start, end) edit beats."""
        result = []
        for section in self.music.sections:
            a = max(0, section.start_beat - self.window.start_beat)
            b = min(self.window.beats, section.end_beat - self.window.start_beat)
            if b > a:
                result.append((a, b, section))
        return result

    def peak(self) -> tuple[int, int] | None:
        """Where the strongest shots go: the drop if the stretch has one, else its highest-energy section."""
        inside = self.in_window()
        drops = [(a, b) for a, b, s in inside if s.label == "drop"]
        if drops:
            return drops[0]
        if not inside:
            return None
        a, b, _ = max(inside, key=lambda item: item[2].energy)
        return (a, b)

    def section_label(self, edit_beat: int) -> str:
        section = self.music.section_at(self.window.start_beat + edit_beat)
        label = section.label if section else "main"
        peak = self.peak()
        if peak and peak[0] <= edit_beat < peak[1]:
            return "drop"
        return "main" if label == "drop" else label

    @property
    def pacing(self) -> Pacing:
        return pacing_for(self.format, self.style, self.pace)

    @property
    def floor_beats(self) -> int:
        return floor_beats(self.pacing, self.seconds_per_beat)

    def section_seconds(self, section: MusicSection | None) -> float:
        """Target shot length for a section: shaped by its energy relative to the rest of the stretch, then scaled
        so the whole edit averages the pacing target."""
        inside = self.in_window()
        energies = [s.energy for _, _, s in inside] or [0.5]
        low, high = min(energies), max(energies)

        def relative(s: MusicSection | None) -> float:
            return 0.5 if s is None or high - low < 1e-6 else (s.energy - low) / (high - low)

        pacing = self.pacing
        weights = [(b - a, pacing.for_energy(relative(s))) for a, b, s in inside]
        mean = sum(w * v for w, v in weights) / (sum(w for w, _ in weights) or 1) if weights else pacing.target_s
        return min(pacing.max_s, max(pacing.min_s, pacing.for_energy(relative(section)) * pacing.target_s / mean))

    def beats_per_cut(self, edit_beat: int) -> int:
        section = self.music.section_at(self.window.start_beat + edit_beat)
        return max(self.floor_beats, beats_for(self.section_seconds(section), self.seconds_per_beat))

    def bounds(self, seg: Segment) -> tuple[int, int]:
        """Frames a shot built on this segment may use: the segment, or the clean continuous footage around it."""
        return self.extent.get(seg.id, (seg.start, seg.end))

    def rank(self, seg: Segment) -> float:
        """Selection score; reels favour segments whose detail sits in the centre, since the sides are cropped."""
        if not self.reel:
            return seg.final_score
        centre = min(1.5, max(0.5, self.centre.get(seg.id, 1.0)))
        return seg.final_score + 1.5 * (centre - 1.0)


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


def extents(segments: list[Segment]) -> dict[str, tuple[int, int]]:
    """For each segment, the continuous clean footage it sits in: neighbouring segments of the same clip with no
    gap (a dropped span) and no crash flag between them."""

    def crashed(seg: Segment) -> bool:
        return seg.signals.crash or bool(seg.claude and seg.claude.penalties.crash)

    result: dict[str, tuple[int, int]] = {}
    by_clip: dict[str, list[Segment]] = {}
    for seg in segments:
        by_clip.setdefault(seg.clip, []).append(seg)
    for clip_segments in by_clip.values():
        clip_segments.sort(key=lambda s: s.start)
        run: list[Segment] = []
        for seg in [*clip_segments, None]:
            joins = seg is not None and run and not crashed(seg) and seg.start - run[-1].end <= 1
            if joins:
                run.append(seg)
                continue
            for member in run:
                result[member.id] = (run[0].start, run[-1].end)
            run = [] if seg is None else [seg]
            if seg is not None and crashed(seg):
                result[seg.id] = (seg.start, seg.end)
                run = []
    return result


def eligible(segments: list[Segment]) -> list[Segment]:
    """Takeoff and landing segments survive only if they score highly."""
    return [s for s in segments if s.kind == "flight" or s.final_score >= HIGH_SCORE_EDGE]


def max_slowmo(clip: ClipInfo, out_fps: Rate) -> float:
    ratio = float(as_fraction(clip.fps) / as_fraction(out_fps))
    return 1.0 if ratio < 1.9 else round(1.0 / math.floor(ratio + 0.05), 3)


def fps_float(fps: Rate) -> float:
    return fps[0] / fps[1]


def usable_stars(ctx: Context) -> set[str]:
    """Starred segments that can hold a shot at the pacing floor, so a plan can be required to use them."""
    result = set()
    for sid in ctx.must_use:
        seg = ctx.segments.get(sid)
        if seg is None:
            continue
        lo, hi = ctx.bounds(seg)
        if _seconds(ctx, seg, hi - lo) >= _need_seconds(ctx, ctx.floor_beats):
            result.add(sid)
    return result


def must_use_problems(ctx: Context, plan: EditPlan) -> list[Violation]:
    used = {p.segment_id for p in plan.shots}
    return [
        Violation(None, "must_use", f"{sid} is starred by the pilot and must be in the edit")
        for sid in sorted(usable_stars(ctx) - used)
    ]


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
        lo, hi = ctx.bounds(seg)
        if need > hi - lo:
            have = (hi - lo) / fps_float(clip.fps)
            violations.append(
                Violation(
                    i,
                    "too_short",
                    f"{seg.id} and the clean footage around it give {have:.2f} s, but {planned.beats} beats at "
                    f"speed {speed} need {need / fps_float(clip.fps):.2f} s",
                )
            )
        start = seg.start + seconds_to_frames(max(0.0, planned.start_offset_s), clip.fps)
        start = max(lo, min(start, hi - need))
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
                starred=seg.id in ctx.must_use,
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
    fps = fps_float(ctx.out_fps)
    lengths = [entry.length / fps for entry in timeline]
    violations += [Violation(index, "pace", message) for index, message in pacing_problems(lengths, ctx.pacing.floor_s)]
    for a in range(len(timeline)):
        for b in range(a + 1, len(timeline)):
            sa, sb = timeline[a].shot, timeline[b].shot
            if sa.clip == sb.clip and sa.in_ < sb.out and sb.in_ < sa.out:
                violations.append(Violation(b, "overlap", f"uses the same footage as shot {a + 1}"))
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


def slots(ctx: Context) -> list[tuple[int, int, str]]:
    """(start beat, beats, section label) for each cut, from the target shot length in each section."""
    result = []
    beat = 0
    while beat < ctx.window.beats:
        length = min(ctx.beats_per_cut(beat), ctx.window.beats - beat)
        result.append((beat, length, ctx.section_label(beat)))
        beat += length
    if len(result) > 1 and result[-1][1] < ctx.floor_beats:
        last = result.pop()
        prev = result.pop()
        result.append((prev[0], prev[1] + last[1], prev[2]))
    return result


def _need_seconds(ctx: Context, beats: int) -> float:
    return beats * ctx.seconds_per_beat + 2 * ctx.transition_frames / fps_float(ctx.out_fps)


def _seconds(ctx: Context, seg: Segment, frames: int) -> float:
    return frames / fps_float(ctx.clips[seg.clip].fps)


def _same_run(ctx: Context, a: Segment, b: Segment) -> bool:
    return a.clip == b.clip and ctx.bounds(a) == ctx.bounds(b)


def _pick(ctx: Context, pool: list[Segment], count: int, beats: int) -> list[Segment]:
    """Best segments that can hold `beats`. A segment too short on its own may borrow the clean footage around
    it, but then nothing else from that stretch of footage is picked, so shots never overlap."""
    chosen: list[Segment] = []
    extended: list[Segment] = []
    need = _need_seconds(ctx, beats)
    for seg in pool:
        if len(chosen) == count:
            break
        clip = ctx.clips[seg.clip]
        if any(_same_run(ctx, seg, other) for other in extended):
            continue
        if _seconds(ctx, seg, seg.frames) < need:
            lo, hi = ctx.bounds(seg)
            if _seconds(ctx, seg, hi - lo) < need or any(_same_run(ctx, seg, other) for other in chosen):
                continue
            extended.append(seg)
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


def _pacing_list(ctx: Context) -> list[dict]:
    result = []
    for a, _, section in ctx.in_window():
        result.append({"section": ctx.music.sections.index(section), "beats_per_cut": ctx.beats_per_cut(a)})
    return result


def select(ctx: Context) -> EditPlan:
    """Deterministic plan: best segments, chronological, strongest ones moved onto the peak."""
    plan_slots = slots(ctx)
    pool = sorted(ctx.segments.values(), key=lambda s: (-ctx.rank(s), ctx.chrono_key(s)))
    clean = [s for s in pool if not s.signals.crash and not (s.claude and s.claude.penalties.crash)]
    pool = clean + [s for s in pool if s not in clean]
    stars = usable_stars(ctx)
    pool = (
        [s for s in pool if s.id in stars]
        + [s for s in pool if s.id not in stars and s.id not in ctx.disliked]
        + [s for s in pool if s.id not in stars and s.id in ctx.disliked]
    )
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
        ranked = sorted(range(len(chosen)), key=lambda i: -ctx.rank(chosen[i]))[: len(drop_positions)]
        stars = [chosen[i] for i in sorted(ranked)]
        rest = [s for i, s in enumerate(chosen) if i not in ranked]
        ordered: list[Segment] = []
        for i in range(len(plan_slots)):
            ordered.append(stars.pop(0) if i in drop_positions else rest.pop(0))
        if ordered != chosen:
            order_note = "The highest-scoring shots are moved onto the peak; everything else stays chronological."
        chosen = ordered
    if ctx.reel and chosen:
        best = max(range(len(chosen)), key=lambda i: ctx.rank(chosen[i]))
        if best:
            chosen[0], chosen[best] = chosen[best], chosen[0]
            order_note = "A reel opens on its strongest shot; " + (order_note or "the rest stays chronological.")
    shots = []
    for i, ((_, beats, label), seg) in enumerate(zip(plan_slots, chosen, strict=True)):
        need = beats * ctx.seconds_per_beat
        offset = max(0.0, (_seconds(ctx, seg, seg.frames) - need) / 2)
        role = (
            "opening"
            if i == 0
            else "closing"
            if i == len(plan_slots) - 1
            else ("peak" if label == "drop" else "build" if label == "build" else "support")
        )
        boundary = i > 0 and plan_slots[i - 1][2] != label
        crossfade = (ctx.style == "cinematic" and boundary) or (label == "outro" and boundary)
        transition = "crossfade" if crossfade and not ctx.reel else "cut"
        tags = ", ".join(seg.claude.tags[:3]) if seg.claude and seg.claude.tags else seg.kind
        shots.append(
            PlannedShot(
                segment_id=seg.id,
                beats=beats,
                start_offset_s=round(offset, 3),
                speed=1.0,
                transition=transition,
                role=role,
                reason=f"Score {seg.final_score:.1f} ({tags}); {beats} beats ({need:.1f} s) in the {label}.",
            )
        )
    return EditPlan(
        title=f"{ctx.style.title()} {'reel' if ctx.reel else 'cut'}",
        concept="Deterministic selection: best-scoring segments in flight order, held for the target shot length "
        "of each section, with the strongest moments on the peak.",
        pacing=_pacing_list(ctx),
        shots=shots,
        order_note=order_note,
        closing_note="Ends on the last chosen segment as the music fades.",
        source="deterministic",
    )


def fix(ctx: Context, plan: EditPlan) -> EditPlan:
    """Deterministic fixer: keep what is valid in Claude's plan, lift short shots to the floor, then patch length
    and order."""
    floor = ctx.floor_beats
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
        if any(_same_run(ctx, seg, ctx.segments[k.segment_id]) for k in kept):
            continue
        speed = planned.speed if 0 < planned.speed <= 4 and planned.speed >= max_slowmo(clip, ctx.out_fps) else 1.0
        lo, hi = ctx.bounds(seg)
        beats = max(planned.beats, floor)
        while beats > floor and _seconds(ctx, seg, hi - lo) < _need_seconds(ctx, beats) * speed:
            beats -= 1
        if _seconds(ctx, seg, hi - lo) < _need_seconds(ctx, beats) * speed:
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
        if kept[longest].beats > floor:
            kept[longest] = kept[longest].model_copy(update={"beats": kept[longest].beats - 1})
        else:
            kept.pop(
                min(
                    range(len(kept)),
                    key=lambda i: (
                        kept[i].segment_id in ctx.must_use,
                        ctx.segments[kept[i].segment_id].final_score,
                    ),
                )
            )
        total = sum(p.beats for p in kept)
    spares = [s for s in sorted(ctx.segments.values(), key=lambda s: -ctx.rank(s)) if s.id not in seen]
    spares.sort(key=lambda s: (s.id not in ctx.must_use, s.id in ctx.disliked))
    while total < ctx.window.beats and spares:
        beats = min(ctx.beats_per_cut(total), ctx.window.beats - total)
        if ctx.window.beats - total - beats < floor:
            beats = ctx.window.beats - total
        seg = next(
            (
                s
                for s in spares
                if _seconds(ctx, s, s.frames) >= _need_seconds(ctx, beats)
                and not any(_same_run(ctx, s, ctx.segments[k.segment_id]) for k in kept)
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
            lo, hi = ctx.bounds(seg)
            if _seconds(ctx, seg, hi - lo) < p.beats * ctx.seconds_per_beat * p.speed:
                kept[i] = p.model_copy(update={"speed": 1.0})
    for i, p in enumerate(kept):
        if p.transition == "crossfade" and i > 0:
            continue
        kept[i] = p.model_copy(update={"transition": "cut" if i == 0 else p.transition})
    return plan.model_copy(update={"shots": kept, "source": f"{plan.source}+fixer"})


REEL_BRIEF = (
    "Format: a SHORT VERTICAL edit for TikTok and Instagram Reels. Every shot is centre-cropped from the "
    "landscape source to 9:16, so the left and right thirds of each frame are cut away. Open on one of the "
    "strongest shots in the very first second: no slow build, no establishing shot. Keep the pacing brisk "
    "throughout (see the pacing targets below), favour shots whose subject or "
    "action sits in the centre of the frame (the centre column scores this: above 1.0 means the detail is "
    "concentrated in the middle), and avoid shots whose interest is at the sides. Crossfades are rarely right here."
)


def director_prompt(ctx: Context, segments_table: str) -> str:
    spb = ctx.seconds_per_beat
    lines = [
        f"Target: {ctx.window.beats} beats = {ctx.window.beats * spb:.1f} s at {ctx.music.tempo:.1f} BPM "
        f"(one beat = {spb:.3f} s). Requested length {ctx.target_s:.0f} s.",
        f"Style: {ctx.style}. Output {ctx.width}x{ctx.height} at {fps_float(ctx.out_fps):.3f} fps.",
        f"Pilot's style note: {ctx.style_note or '(none)'}",
    ]
    stars = sorted(usable_stars(ctx))
    if stars:
        lines.append(
            f"Pilot's must-use segments (starred in review; every one must be in the plan): {', '.join(stars)}"
        )
    if ctx.disliked:
        lines.append(
            "Pilot disliked these segments; use one only if nothing else fits: " + ", ".join(sorted(ctx.disliked))
        )
    if ctx.reel:
        lines += ["", REEL_BRIEF]
    lines += [
        "",
        f"The edit uses the track from {ctx.music.beats[ctx.window.start_beat]:.2f} s. Music sections inside the "
        "edit (beats relative to the edit start):",
    ]
    for a, b, section in ctx.in_window():
        index = ctx.music.sections.index(section)
        per_cut = ctx.beats_per_cut(a)
        lines.append(
            f"  section {index}: {section.label}, beats {a}-{b} ({a * spb:.1f}-{b * spb:.1f} s), "
            f"energy {section.energy:.2f}, aim for about {per_cut} beats ({per_cut * spb:.1f} s) per shot"
        )
    peak = ctx.peak()
    if peak:
        has_drop = any(s.label == "drop" for _, _, s in ctx.in_window())
        where = "the drop" if has_drop else "the highest-energy part of this stretch (it has no drop)"
        lines.append(
            f"Peak: edit beats {peak[0]}-{peak[1]}, {where}. Put the strongest shots there "
            "(section labels are an energy heuristic)."
        )
    pace = ctx.pacing
    lines += [
        "",
        f"Pacing: shots average about {pace.target_s:.1f} s ({beats_for(pace.target_s, spb)} beats); most between "
        f"{pace.min_s:.1f} and {pace.max_s:.1f} s. Hold longer in quiet sections and a little shorter in loud ones. "
        "FPV moves need time to read: give the best moments (a gap thread, a reveal, a close pass) a hold long "
        "enough to show the approach, the pass and the exit. No shot may be shorter than "
        f"{pace.floor_s:.1f} s ({ctx.floor_beats} beats), except a burst of 2-4 quick cuts used as an accent "
        f"(for example into or on the drop), at most {max(1, int(ctx.window.beats * spb // 30))} such burst(s). "
        "A hold may run past its segment's usable_s into the clean footage around it (extend_s is the most "
        "available), as long as two shots never use the same footage.",
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
    header = "id | flight | clip@time | usable_s | extend_s | score | max_slowmo | flags | tags | description"
    lines = [header.replace("| flags", "| centre | flags") if ctx.reel else header]
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
                ("STARRED", seg.id in ctx.must_use),
                ("disliked", seg.id in ctx.disliked),
            )
            if on
        ]
        tags = ",".join(seg.claude.tags) if seg.claude else ""
        desc = seg.claude.description if seg.claude else "(local signals only)"
        centre = f"{ctx.centre.get(seg.id, 1.0):.2f} | " if ctx.reel else ""
        lo, hi = ctx.bounds(seg)
        extend = (hi - lo) / fps_float(clip.fps)
        lines.append(
            f"{seg.id} | {seg.flight} | {seg.clip}@{format_timecode(seg.start, clip.fps)} | "
            f"{seg.frames / fps_float(clip.fps):.2f} | {extend:.2f} | "
            f"{seg.final_score:.1f} | {max_slowmo(clip, ctx.out_fps)} | "
            f"{centre}{','.join(flags) or '-'} | {tags} | {desc}"
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
        violations += must_use_problems(ctx, plan)
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
    violations += must_use_problems(ctx, fixed)
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
