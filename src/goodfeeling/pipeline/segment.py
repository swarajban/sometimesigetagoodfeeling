"""Segment: 3-8 s windows aligned to motion-energy changes, idle dropped, takeoff/landing marked."""

from __future__ import annotations

import logging

import numpy as np

from goodfeeling.pipeline.signals import ClipSignals, analyze_clip, crash_flags, idle_spans, smooth
from goodfeeling.schemas.clip import ClipInfo, FlightInfo
from goodfeeling.schemas.segment import Segment, SegmentSignals
from goodfeeling.settings import Weights
from goodfeeling.store.project import Project

log = logging.getLogger(__name__)

MIN_S, MAX_S, TARGET_S = 3.0, 8.0, 5.0
EDGE_S = 5.0
MIN_KEEP_S = 1.5


def _windows(start: int, end: int, energy: np.ndarray, fps_s: float) -> list[tuple[int, int]]:
    """Split sample range [start, end) greedily at the strongest motion-energy change in reach."""
    windows = []
    change = np.abs(np.gradient(smooth(energy, 5))) if len(energy) > 1 else np.zeros_like(energy)
    lo, hi, target = int(MIN_S * fps_s), int(MAX_S * fps_s), int(TARGET_S * fps_s)
    cursor = start
    while end - cursor > hi:
        reach = range(cursor + lo, min(cursor + hi, end - lo) + 1)
        if not reach:
            break
        best = max(reach, key=lambda i: (change[i], -abs(i - cursor - target)))
        windows.append((cursor, best))
        cursor = best
    remaining = end - cursor
    if remaining >= lo or not windows:
        if remaining >= MIN_KEEP_S * fps_s:
            windows.append((cursor, end))
    else:
        prev_start, _ = windows.pop()
        if end - prev_start <= hi + lo:
            windows.append((prev_start, end))
        else:
            mid = (prev_start + end) // 2
            windows += [(prev_start, mid), (mid, end)]
    return windows


def segment_clip(flight: FlightInfo, clip: ClipInfo, sig: ClipSignals, *, last_in_flight: bool) -> list[Segment]:
    n = len(sig.frames)
    if n == 0:
        return []
    fps_s = sig.sample_fps

    def to_frame(sample: int) -> int:
        return clip.frames if sample >= n else min(clip.frames, sig.frames[sample])

    blocked = np.zeros(n, dtype=bool)
    for a, b in idle_spans(sig):
        blocked[a:b] = True
    edge = int(EDGE_S * fps_s)
    takeoff = (0, min(edge, n)) if flight.order_in_flight == 0 and not flight.continues_previous else None
    landing = (max(0, n - edge), n) if last_in_flight else None
    hard = {0, n} | {min(n, int(round(cut / (clip.fps[0] / clip.fps[1]) * fps_s))) for cut in sig.scene_cuts}
    for span in (takeoff, landing):
        if span:
            hard |= set(span)
    hard |= {i for i in range(1, n) if blocked[i] != blocked[i - 1]}
    bounds = sorted(b for b in hard if 0 <= b <= n)

    crashes = crash_flags(sig)
    energy = np.asarray(sig.motion)
    pieces: list[tuple[int, int, str]] = []
    for a, b in zip(bounds, bounds[1:], strict=False):
        if b <= a or blocked[a:b].mean() > 0.5:
            continue
        if takeoff and a >= takeoff[0] and b <= takeoff[1]:
            pieces.append((a, b, "takeoff"))
        elif landing and a >= landing[0] and b <= landing[1]:
            pieces.append((a, b, "landing"))
        else:
            pieces += [(wa, wb, "flight") for wa, wb in _windows(a, b, energy, fps_s)]

    segments = []
    cut_frames = set(sig.scene_cuts)
    for index, (a, b, kind) in enumerate(pieces):
        if (b - a) / fps_s < MIN_KEEP_S:
            continue
        start, end = to_frame(a), to_frame(b)
        window = slice(a, b)
        motion = np.asarray(sig.motion[window])
        sharp = np.asarray(sig.sharpness[window])
        mid = (a + b) // 2
        signals = SegmentSignals(
            motion=float(motion.mean()),
            motion_peak=float(np.percentile(motion, 90)),
            coherence=float(np.mean(sig.coherence[window])),
            rotation=float(np.mean(np.abs(sig.rotation[window]))),
            sharpness=float(np.median(sharp)),
            exposure=float(np.mean(sig.exposure[window])),
            clipped=float(np.mean(sig.clipped[window])),
            crash=bool(crashes[window].any()),
            blur=False,
            scene_cut_inside=any(start < f < end for f in cut_frames),
        )
        segments.append(
            Segment(
                id=f"{flight.clip_id}s{index + 1:02d}",
                clip=flight.clip_id,
                flight=flight.flight,
                start=start,
                end=end,
                kind=kind,
                signals=signals,
                phash=sig.dhash[mid],
            )
        )
    return segments


def local_scores(segments: list[Segment]) -> None:
    """0-10 scores from local signals, relative to this project's footage."""
    if not segments:
        return
    motion = np.array([s.signals.motion for s in segments])
    sharp = np.array([s.signals.sharpness for s in segments])
    sharp_median = float(np.median(sharp)) or 1.0
    m_lo, m_hi = np.percentile(motion, 5), np.percentile(motion, 95)
    for seg in segments:
        sig = seg.signals
        sig.blur = sig.sharpness < 0.35 * sharp_median
        motion_score = 10 * float(np.clip((sig.motion - m_lo) / ((m_hi - m_lo) or 1.0), 0, 1))
        sharp_score = 10 * float(np.clip(sig.sharpness / (sharp_median * 1.5), 0, 1))
        exposure_score = 10 * float(np.clip(1 - abs(sig.exposure - 0.45) * 2.2 - sig.clipped * 3, 0, 1))
        seg.local_score = round((motion_score * 0.5 + sharp_score * 0.3 + exposure_score * 0.2), 2)
        seg.local_parts = {"motion": motion_score, "sharpness": sharp_score, "exposure": exposure_score}


def blend(segments: list[Segment], weights: Weights) -> None:
    """Final score: weighted blend of Claude's score and local signals, minus local crash/blur penalties."""
    for seg in segments:
        parts = seg.local_parts or {
            "motion": seg.local_score,
            "sharpness": seg.local_score,
            "exposure": seg.local_score,
        }
        pairs = [
            (weights.motion, parts["motion"]),
            (weights.sharpness, parts["sharpness"]),
            (weights.exposure, parts["exposure"]),
        ]
        if seg.claude is not None:
            pairs.append((weights.claude, seg.claude.score))
        total = sum(w for w, _ in pairs) or 1.0
        score = sum(w * v for w, v in pairs) / total
        if seg.signals.crash:
            score -= 2.0
        if seg.signals.blur:
            score -= 1.0
        if seg.claude is not None:
            p = seg.claude.penalties
            score -= 0.5 * sum([p.blur, p.crash, p.horizon_chaos, p.props_in_view])
        seg.final_score = round(float(np.clip(score, 0, 10)), 2)


def segment_project(
    project: Project, clips: list[tuple[FlightInfo, ClipInfo, object]], progress=print
) -> list[Segment]:
    segments: list[Segment] = []
    last_by_flight = {}
    for flight, _, _ in clips:
        last_by_flight[flight.flight] = flight.clip_id
    for flight, clip, proxy in clips:
        rel = f"analysis/signals-{clip.fingerprint}.json"
        cached = project.read_json(rel)
        if cached and cached.get("version") == ClipSignals.model_fields["version"].default:
            sig = ClipSignals.model_validate(cached)
        else:
            progress(f"  signals {flight.clip_id}")
            sig = analyze_clip(proxy, clip.fps, clip.fingerprint)
            project.write_json(rel, sig.model_dump())
        segments += segment_clip(flight, clip, sig, last_in_flight=last_by_flight[flight.flight] == flight.clip_id)
    local_scores(segments)
    return segments
