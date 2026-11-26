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
from goodfeeling.pipeline.pacing import LONG_SCORE, Rules, beats_at_most, floor_beats, pacing_problems, rules_for
from goodfeeling.pipeline.render import render_problems
from goodfeeling.pipeline.signals import hamming
from goodfeeling.pipeline.stretch import EditWindow
from goodfeeling.schemas.clip import ClipInfo, FlightInfo
from goodfeeling.schemas.edl import EDL, MusicRef, Output, Shot, Source
from goodfeeling.schemas.moment import Moment
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
    moments: dict[str, Moment] = field(default_factory=dict)
    must_use: set[str] = field(default_factory=set)
    disliked: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    limited: bool = False

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
    def rules(self) -> Rules:
        return rules_for(self.format, self.style, self.pace)

    @property
    def floor_beats(self) -> int:
        return floor_beats(self.rules, self.seconds_per_beat)

    @property
    def filler_beats(self) -> int:
        return max(
            self.floor_beats, beats_at_most(self.rules.filler_s + 0.25 * self.seconds_per_beat, self.seconds_per_beat)
        )

    def span_s(self, seg: Segment) -> float:
        return seg.frames / fps_float(self.clips[seg.clip].fps)

    def full_beats(self, seg: Segment) -> int:
        """Beats that show a moment from approach to exit without running past it."""
        return max(self.floor_beats, beats_at_most(self.span_s(seg) + 0.3, self.seconds_per_beat))

    def max_hold_s(self, seg: Segment) -> tuple[float, str]:
        """How long a shot on this pick may run, and why: content decides, filler stays short."""
        filler = self.rules.filler_s + 0.5 * self.seconds_per_beat
        moment = self.moments.get(seg.id)
        if moment is not None:
            if moment.hold == "short":
                return filler, f"{seg.id} is marked as a short hit, so at most {filler:.1f} s"
            return self.span_s(seg) + 0.6, f"{seg.id}'s move lasts {self.span_s(seg):.1f} s; don't pad it with dead air"
        if seg.final_score >= LONG_SCORE or self.limited:
            return self.span_s(seg) + 1.0, f"{seg.id} is {self.span_s(seg):.1f} s of footage"
        return filler, (
            f"{seg.id} scores {seg.final_score:.1f}; only marked moments or segments scoring "
            f"{LONG_SCORE:g}+ may hold longer than {self.rules.filler_s:.1f} s"
        )

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
    notes: list[str] = field(default_factory=list)


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
    """Starred picks a plan can be required to use: each can hold a shot at the pacing floor, no two share footage
    or show the same moment, and together they fit in the edit. Strongest first when they don't all fit."""
    floor = ctx.floor_beats
    kept: list[Segment] = []
    starred = [ctx.segments[sid] for sid in ctx.must_use if sid in ctx.segments]
    for seg in sorted(starred, key=lambda s: (-ctx.rank(s), ctx.chrono_key(s))):
        lo, hi = ctx.bounds(seg)
        if _seconds(ctx, seg, hi - lo) < floor * ctx.seconds_per_beat or (len(kept) + 1) * floor > ctx.window.beats:
            continue
        if _overlaps(ctx, seg, kept):
            continue
        kept.append(seg)
    return {seg.id for seg in kept}


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
        held = length / fps_float(ctx.out_fps)
        limit, why = ctx.max_hold_s(seg)
        if held > limit + 1e-6:
            violations.append(Violation(i, "hold", f"held {held:.1f} s; {why}"))
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
    violations += [Violation(index, "pace", message) for index, message in pacing_problems(lengths, ctx.rules.floor_s)]
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


def _seconds(ctx: Context, seg: Segment, frames: int) -> float:
    return frames / fps_float(ctx.clips[seg.clip].fps)


def _overlaps(ctx: Context, seg: Segment, others: list[Segment]) -> bool:
    lo, hi = ctx.bounds(seg)
    for other in others:
        if other.clip != seg.clip:
            continue
        olo, ohi = ctx.bounds(other)
        if lo < ohi and olo < hi:
            return True
    return _near_duplicate(ctx, seg, others)


def _near_duplicate(ctx: Context, seg: Segment, others: list[Segment]) -> bool:
    """The validator's near_duplicate rule: same clip, close in time, and alike to look at."""
    return any(
        other.clip == seg.clip
        and other.id != seg.id
        and hamming(other.phash, seg.phash) <= DUPLICATE_HAMMING
        and max(other.start, seg.start) - min(other.end, seg.end)
        <= DUPLICATE_GAP_S * fps_float(ctx.clips[seg.clip].fps)
        for other in others
    )


def _split(total: int, low: int, high: int) -> list[int] | None:
    """Split `total` beats into as few parts as possible, each between low and high."""
    if total <= 0:
        return []
    count = math.ceil(total / high)
    if count * low > total:
        return None
    base, extra = divmod(total, count)
    return [base + (1 if i < extra else 0) for i in range(count)]


def _place_star(
    ctx: Context, order: list[tuple[Segment, int]], star: Segment, at_beat: int
) -> list[tuple[Segment, int]]:
    rest = [item for item in order if item[0] is not star]
    item = next(item for item in order if item[0] is star)
    position, beat = 0, 0
    for i, (_, beats) in enumerate(rest):
        if beat >= at_beat:
            break
        position, beat = i + 1, beat + beats
    return [*rest[:position], item, *rest[position:]]


def _longest_beat_s(ctx: Context) -> float:
    frames = ctx.window.frames
    return max(b - a for a, b in zip(frames, frames[1:], strict=False)) / fps_float(ctx.out_fps)


def select(ctx: Context) -> EditPlan:
    """Deterministic plan: the best moments played in full, short connecting shots between them, the biggest
    moment on the peak (a reel opens on it).

    It always produces a plan from whatever footage there is: shots are placed on the frames they will actually use,
    so one clip can give several shots; when connecting shots run out, the shots already chosen hold as long as
    their content allows; and when that still doesn't fill the stretch, the edit gets shorter (ctx.window shrinks
    and ctx.notes says so). It fails only when no clean stretch of footage can hold a single shot."""
    total, floor, filler = ctx.window.beats, ctx.floor_beats, ctx.filler_beats
    beat_s = _longest_beat_s(ctx)
    required = usable_stars(ctx)
    clean = [
        s
        for s in ctx.segments.values()
        if s.id in required or not (s.signals.crash or (s.claude and s.claude.penalties.crash))
    ]

    def need(seg: Segment, beats: int) -> int:
        return math.ceil(beats * beat_s * fps_float(ctx.clips[seg.clip].fps)) + 1

    def cap(seg: Segment, beats: int) -> int:
        """The most beats, up to `beats`, that the footage around `seg` and its hold limit allow."""
        lo, hi = ctx.bounds(seg)
        limit = ctx.max_hold_s(seg)[0]
        while beats > 0 and (beats * beat_s > limit + 1e-6 or need(seg, beats) > hi - lo):
            beats -= 1
        return beats

    def place(seg: Segment, beats: int, others: list[list]) -> int | None:
        """A start frame for `beats` of `seg` that uses no frame another chosen shot uses, near its centre."""
        lo, hi = ctx.bounds(seg)
        n = need(seg, beats)
        first, last = max(lo, min(seg.start, hi - n)), hi - n
        if last < first or _near_duplicate(ctx, seg, [o[0] for o in others]):
            return None
        taken = [(o[2], o[2] + need(o[0], o[1])) for o in others if o[0].clip == seg.clip]
        centre = min(last, max(first, seg.start + (seg.frames - n) // 2))
        for start in sorted(
            {centre, first, last, *(b for _, b in taken), *(a - n for a, _ in taken)}, key=lambda s: abs(s - centre)
        ):
            if first <= start <= last and all(start + n <= a or start >= b for a, b in taken):
                return start
        return None

    def add(seg: Segment, beats: int) -> bool:
        start = place(seg, beats, chosen)
        if start is not None:
            chosen.append([seg, beats, start])
        return start is not None

    moments = sorted(
        [s for s in clean if s.id in ctx.moments and ctx.moments[s.id].hold == "full"],
        key=lambda s: (s.id not in required, s.id in ctx.disliked, -ctx.rank(s)),
    )
    budget = round(total * (1 - ctx.rules.filler_share))
    chosen: list[list] = []
    used = 0
    for seg in moments:
        beats = cap(seg, min(ctx.full_beats(seg), total))
        over = used + beats > budget and chosen and seg.id not in required
        if beats < floor or over or used + beats > total:
            continue
        if add(seg, beats):
            used += beats
    fillers = sorted(
        [s for s in clean if s.id not in ctx.moments or (s.id in required and ctx.moments[s.id].hold != "full")],
        key=lambda s: (s.id not in required, s.id in ctx.disliked, -ctx.rank(s)),
    )
    if 0 < total - used < floor and chosen:
        item = max(chosen, key=lambda c: c[1])
        cut = floor - (total - used)
        if item[1] - cut >= floor:
            item[1] -= cut
            used -= cut
    while total - used >= floor:
        left = total - used
        for seg in fillers:
            beats = min(cap(seg, filler), left)
            short = floor - (left - beats)
            if 0 < left - beats < floor and beats - short >= floor:
                beats -= short
            if beats >= floor and add(seg, beats):
                fillers.remove(seg)
                used += beats
                break
        else:
            break

    def grow() -> int:
        """Let the chosen shots, the best first, hold longer to fill what connecting shots couldn't."""
        grown = 0
        for item in sorted(chosen, key=lambda c: (c[0].id not in ctx.moments, -ctx.rank(c[0]))):
            while used + grown < total and cap(item[0], item[1] + 1) > item[1]:
                start = place(item[0], item[1] + 1, [c for c in chosen if c is not item])
                if start is None:
                    break
                item[1], item[2] = item[1] + 1, start
                grown += 1
        return grown

    used += grow()
    if total - used >= floor and chosen:
        ctx.limited = True
        grown = grow()
        used += grown
        if grown:
            ctx.notes.append("The footage was limited, so some shots hold longer than they normally would.")
    if not chosen:
        raise RuntimeError(
            f"no clean stretch of footage is long enough for one {floor * ctx.seconds_per_beat:.1f} s shot"
        )
    if not ctx.moments:
        ctx.notes.append(
            "No standout moves were found in this footage, so the edit is simpler: short shots of the best of it."
        )
    if used < total:
        ctx.notes.append(
            f"The footage was limited, so the edit runs {used * ctx.seconds_per_beat:.1f} s instead of "
            f"{total * ctx.seconds_per_beat:.1f} s."
        )
        window = ctx.window
        ctx.window = EditWindow(window.start_beat, used, window.start_frame, window.frames[: used + 1])
    starts = {c[0].id: c[2] for c in chosen}
    chosen = [(c[0], c[1]) for c in chosen]
    order = sorted(chosen, key=lambda item: ctx.chrono_key(item[0]))
    order_note = ""
    stars = sorted([c for c, _ in chosen if c.id in ctx.moments], key=lambda s: -ctx.rank(s))
    peak = ctx.peak()
    if stars and ctx.reel:
        order = _place_star(ctx, order, stars[0], 0)
        order_note = "A reel opens on its biggest moment; the rest stays chronological."
    elif stars and peak:
        order = _place_star(ctx, order, stars[0], peak[0])
        order_note = "The biggest moment is moved onto the peak; the rest stays chronological."
    shots = []
    beat = 0
    for i, (seg, beats) in enumerate(order):
        held = beats * ctx.seconds_per_beat
        offset = max(0.0, _seconds(ctx, seg, starts[seg.id] - seg.start))
        moment = ctx.moments.get(seg.id)
        label = ctx.section_label(beat)
        role = "opening" if i == 0 else "closing" if i == len(order) - 1 else ("peak" if moment else "support")
        why = (
            f"a {moment.kind} worth {moment.worth:.1f}, played in full ({held:.1f} s of its "
            f"{_seconds(ctx, seg, seg.frames):.1f} s)"
            if moment
            else f"score {seg.final_score:.1f}, a short connecting shot ({held:.1f} s)"
        )
        shots.append(
            PlannedShot(
                segment_id=seg.id,
                beats=beats,
                start_offset_s=offset,
                speed=1.0,
                transition="cut",
                role=role,
                reason=f"{why}; starts in the {label}.",
            )
        )
        beat += beats
    return EditPlan(
        title=f"{ctx.style.title()} {'reel' if ctx.reel else 'cut'}",
        concept="Deterministic selection: the strongest moments play from approach to exit, joined by short "
        "connecting shots, with the biggest moment on the peak.",
        pacing=[],
        shots=shots,
        order_note=order_note,
        closing_note="Ends on the last chosen shot as the music fades.",
        source="deterministic",
    )


def fix(ctx: Context, plan: EditPlan) -> EditPlan:
    """Deterministic fixer: keep Claude's valid picks with lengths clamped to what their content allows, then fill
    or trim to the edit's length with short connecting shots."""
    floor, total = ctx.floor_beats, ctx.window.beats
    kept: list[PlannedShot] = []
    picked: list[Segment] = []
    for planned in plan.shots:
        seg = ctx.segments.get(planned.segment_id)
        if seg is None or seg in picked or planned.beats < 1 or _overlaps(ctx, seg, picked):
            continue
        limit_s, _ = ctx.max_hold_s(seg)
        lo, hi = ctx.bounds(seg)
        most = beats_at_most(min(limit_s, _seconds(ctx, seg, hi - lo)), ctx.seconds_per_beat)
        beats = min(max(planned.beats, floor), most)
        if beats < floor:
            continue
        kept.append(
            planned.model_copy(
                update={"beats": beats, "speed": 1.0, "reason": planned.reason or "Kept from the director's plan."}
            )
        )
        picked.append(seg)
    while sum(p.beats for p in kept) > total and kept:
        weakest = min(
            range(len(kept)),
            key=lambda i: (kept[i].segment_id in ctx.must_use, ctx.segments[kept[i].segment_id].final_score),
        )
        kept.pop(weakest)
        picked.pop(weakest)
    spares = sorted(
        [s for s in ctx.segments.values() if s not in picked and (s.id not in ctx.moments or s.id in ctx.must_use)],
        key=lambda s: (s.id not in ctx.must_use, s.id in ctx.disliked, -ctx.rank(s)),
    )
    for beats in _split(total - sum(p.beats for p in kept), floor, ctx.filler_beats) or []:
        seg = next(
            (
                s
                for s in spares
                if _seconds(ctx, s, s.frames) >= beats * ctx.seconds_per_beat + 0.3 and not _overlaps(ctx, s, picked)
            ),
            None,
        )
        if seg is None:
            break
        spares.remove(seg)
        picked.append(seg)
        kept.append(
            PlannedShot(
                segment_id=seg.id,
                beats=beats,
                start_offset_s=0.0,
                speed=1.0,
                transition="cut",
                role="support",
                reason=f"Added by the fixer as a short connecting shot (score {seg.final_score:.1f}).",
            )
        )
    if not plan.order_note.strip():
        kept.sort(key=lambda p: ctx.chrono_key(ctx.segments[p.segment_id]))
    for i, p in enumerate(kept):
        if i == 0 and p.transition != "cut":
            kept[i] = p.model_copy(update={"transition": "cut"})
    return plan.model_copy(update={"shots": kept, "source": f"{plan.source}+fixer"})


REEL_BRIEF = (
    "Format: a SHORT VERTICAL edit for TikTok and Instagram Reels. Every shot is centre-cropped from the "
    "landscape source to 9:16, so the left and right thirds of each frame are cut away. Open on one of the "
    "strongest moments in the very first second: no slow build, no establishing shot. Three to six shots is "
    "typical: the hook, a few of the best moves, and short hits between them. Favour shots whose subject or "
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
    if ctx.notes:
        lines.append("Footage: " + " ".join(ctx.notes))
    stars = sorted(usable_stars(ctx))
    if stars:
        lines.append(f"Pilot's must-use picks (starred in review; every one must be in the plan): {', '.join(stars)}")
    optional = sorted(sid for sid in ctx.must_use - set(stars) if sid in ctx.segments)
    if optional:
        lines.append(
            "Also starred, but not required because they are too short, share footage with another star, or don't "
            f"all fit: {', '.join(optional)}. Use them if they fit."
        )
    if ctx.disliked:
        lines.append("The pilot disliked these; use one only if nothing else fits: " + ", ".join(sorted(ctx.disliked)))
    if ctx.reel:
        lines += ["", REEL_BRIEF]
    lines += [
        "",
        f"The edit uses the track from {ctx.music.beats[ctx.window.start_beat]:.2f} s. Music sections inside the "
        "edit (beats relative to the edit start):",
    ]
    for a, b, section in ctx.in_window():
        index = ctx.music.sections.index(section)
        lines.append(
            f"  section {index}: {section.label}, beats {a}-{b} ({a * spb:.1f}-{b * spb:.1f} s), "
            f"energy {section.energy:.2f}"
        )
    peak = ctx.peak()
    if peak:
        has_drop = any(s.label == "drop" for _, _, s in ctx.in_window())
        where = "the drop" if has_drop else "the highest-energy part of this stretch (it has no drop)"
        lines.append(
            f"Peak: edit beats {peak[0]}-{peak[1]}, {where}. Put the strongest shots there "
            "(section labels are an energy heuristic)."
        )
    rules = ctx.rules
    lines += [
        "",
        "Shot lengths come from the content. Play a moment marked hold=full from approach to exit (its full_beats; "
        "trim a little if you must, never pad it). Moments marked short and ordinary segments are connecting shots "
        f"of at most {rules.filler_s:.1f} s ({ctx.filler_beats} beats), or are left out. Only moments or segments "
        f"scoring {LONG_SCORE:g}+ may hold longer than that. No shot may be shorter than {rules.floor_s:.1f} s "
        f"({ctx.floor_beats} beats), except one burst of 2-4 quick cuts as an accent (at most "
        f"{max(1, int(ctx.window.beats * spb // 30))}). The biggest moment goes on the peak; a long hold may run "
        "across a section change if the move does. A 60 s edit of seven great moves is fine; so is one long hold "
        f"followed by quick hits. {rules.describe_pace()} Every reason says why the shot gets its length.",
        "",
        "Session summary:",
        ctx.summary or "(none)",
        "",
        segments_table,
        "",
        "Plan the edit and call submit_edit_plan.",
    ]
    return "\n".join(lines)


def segments_for_director(ctx: Context) -> str:
    spb = ctx.seconds_per_beat
    lines = [
        "Moments (moves found by a second look; ids m01...). span_s runs from approach to exit; full_beats plays "
        "it in full. A moment overlaps the segments it was built from: use one or the other.",
        "id | flight | clip@time | span_s | full_beats | hold | worth | kind | segments | description",
    ]
    for seg in sorted((s for s in ctx.segments.values() if s.id in ctx.moments), key=ctx.chrono_key):
        moment, clip = ctx.moments[seg.id], ctx.clips[seg.clip]
        lines.append(
            f"{seg.id} | {seg.flight} | {seg.clip}@{format_timecode(seg.start, clip.fps)} | "
            f"{ctx.span_s(seg):.2f} | {ctx.full_beats(seg)} ({ctx.full_beats(seg) * spb:.1f} s) | "
            f"{moment.hold} | {moment.worth:.1f} | {moment.kind} | {','.join(moment.segment_ids)} | "
            f"{'[STARRED] ' if seg.id in ctx.must_use else ''}{'[disliked] ' if seg.id in ctx.disliked else ''}"
            f"{moment.description}"
        )
    header = "id | flight | clip@time | usable_s | score | max_slowmo | flags | tags | description"
    lines += [
        "",
        "Segments (chronological; ordinary footage for short connecting shots).",
        header.replace("| flags", "| centre | flags") if ctx.reel else header,
    ]
    for seg in sorted((s for s in ctx.segments.values() if s.id not in ctx.moments), key=ctx.chrono_key):
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
        lines.append(
            f"{seg.id} | {seg.flight} | {seg.clip}@{format_timecode(seg.start, clip.fps)} | "
            f"{seg.frames / fps_float(clip.fps):.2f} | {seg.final_score:.1f} | {max_slowmo(clip, ctx.out_fps)} | "
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
    deterministic = select(ctx)
    notes = list(ctx.notes)
    for note in notes:
        progress(f"  note: {note}")
    if claude is None:
        edl, violations = build_edl(ctx, deterministic)
        if violations or edl is None:
            raise RuntimeError("deterministic plan failed validation: " + "; ".join(map(str, violations)))
        return DirectorResult(plan=deterministic, edl=edl, rounds=rounds, fallback=True, notes=notes)
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
            return DirectorResult(plan=plan, edl=edl, rounds=rounds, notes=notes)
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
        return DirectorResult(plan=fixed, edl=edl, rounds=rounds, fixed=True, notes=notes)
    progress("  fixer could not satisfy every rule; using the deterministic selector")
    edl, violations = build_edl(ctx, deterministic)
    if violations or edl is None:
        raise RuntimeError("deterministic plan failed validation: " + "; ".join(map(str, violations)))
    return DirectorResult(plan=deterministic, edl=edl, rounds=rounds, fixed=True, fallback=True, notes=notes)


def plan_json(result: DirectorResult) -> str:
    return (
        json.dumps(
            {
                "plan": result.plan.model_dump(),
                "validation_rounds": result.rounds,
                "fixed": result.fixed,
                "fallback": result.fallback,
                "notes": result.notes,
            },
            indent=2,
        )
        + "\n"
    )
