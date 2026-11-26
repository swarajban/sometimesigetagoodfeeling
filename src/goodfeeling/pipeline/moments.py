"""Moments: where a strong move starts and ends.

Adjacent strong segments of one continuous clip are joined into candidates; the best candidates get a second
look (a dense, time-labelled frame grid of the candidate and its neighbours) and Claude returns the move's in and
out points, what it is, and whether it deserves a full hold or only works as a short hit. Results are cached by
content like scores, so nothing is re-scored and a second run makes no calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from goodfeeling.ai.client import Claude, image_block
from goodfeeling.ai.pricing import image_tokens, text_tokens
from goodfeeling.ai.prompts import MOMENT_PROMPT_VERSION, MOMENT_SYSTEM, MOMENT_TOOL
from goodfeeling.media.ffmpeg import run_ffmpeg
from goodfeeling.pipeline.score import Estimate
from goodfeeling.pipeline.strips import font
from goodfeeling.schemas.clip import ClipInfo
from goodfeeling.schemas.moment import Moment
from goodfeeling.schemas.segment import Score, Segment, SegmentSignals
from goodfeeling.store.project import Project
from goodfeeling.timebase import format_timecode, seconds_to_frames, seek_seconds

log = logging.getLogger(__name__)

STRONG = 7.5
LOOK_PAD_S = 3.0
GRID_COLS, GRID_ROWS = 4, 4
CELL_W, CELL_H = 288, 162
LOOK_PARAMS = f"grid{GRID_COLS}x{GRID_ROWS}-{CELL_W}w-v1"


@dataclass
class Candidate:
    clip: str
    flight: int
    start: int
    end: int
    look_start: int
    look_end: int
    segments: list[Segment]

    @property
    def score(self) -> float:
        return max(s.final_score for s in self.segments)


def _crashed(seg: Segment) -> bool:
    return seg.signals.crash or bool(seg.claude and seg.claude.penalties.crash)


def candidates(
    segments: list[Segment], extents: dict[str, tuple[int, int]], clips: dict[str, ClipInfo], limit: int
) -> list[Candidate]:
    """Join adjacent strong, crash-free segments of one continuous stretch; keep the best `limit`."""
    by_clip: dict[str, list[Segment]] = {}
    for seg in segments:
        by_clip.setdefault(seg.clip, []).append(seg)
    found: list[Candidate] = []
    for clip_id, items in by_clip.items():
        items.sort(key=lambda s: s.start)
        fps = clips[clip_id].fps
        group: list[Segment] = []
        for seg in [*items, None]:
            strong = seg is not None and seg.final_score >= STRONG and not _crashed(seg)
            joins = (
                strong and group and extents.get(seg.id) == extents.get(group[-1].id) and seg.start - group[-1].end <= 1
            )
            if joins:
                group.append(seg)
                continue
            if group:
                lo, hi = extents.get(group[0].id, (group[0].start, group[-1].end))
                pad = seconds_to_frames(LOOK_PAD_S, fps)
                found.append(
                    Candidate(
                        clip=clip_id,
                        flight=group[0].flight,
                        start=group[0].start,
                        end=group[-1].end,
                        look_start=max(lo, group[0].start - pad),
                        look_end=min(hi, group[-1].end + pad),
                        segments=list(group),
                    )
                )
            group = [seg] if strong else []
    found.sort(key=lambda c: (-c.score, -sum(s.final_score for s in c.segments)))
    return found[:limit]


def candidate_limit(clips: dict[str, ClipInfo]) -> int:
    footage_s = sum(c.seconds for c in clips.values())
    return int(min(30, max(6, round(footage_s / 20))))


def cache_key(fingerprint: str, cand: Candidate, model: str) -> str:
    raw = (
        f"{fingerprint}|{cand.look_start}|{cand.look_end}|{cand.start}|{cand.end}|{LOOK_PARAMS}|{model}|"
        f"{MOMENT_PROMPT_VERSION}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def estimate_looks(count: int, model: str) -> Estimate:
    width, height = GRID_COLS * CELL_W, GRID_ROWS * (CELL_H + 22)
    per = text_tokens(MOMENT_SYSTEM) + text_tokens(json.dumps(MOMENT_TOOL)) + 400 + image_tokens(width, height)
    return Estimate("moments", model, count, count * per, count * 700)


def look_grid(proxy: Path, clip: ClipInfo, cand: Candidate) -> tuple[Image.Image, list[float]]:
    """A 4x4 grid of frames evenly across the look window, each captioned with its time in the clip."""
    count = GRID_COLS * GRID_ROWS
    frames = np.linspace(cand.look_start, cand.look_end - 1, count).round().astype(int)
    fps = clip.fps[0] / clip.fps[1]
    select = "+".join(f"eq(n\\,{int(f - frames[0])})" for f in frames)
    proc = run_ffmpeg(
        [
            "-ss",
            f"{seek_seconds(int(frames[0]), clip.fps):.6f}",
            "-i",
            str(proxy),
            "-an",
            "-vf",
            f"select='{select}',scale={CELL_W}:{CELL_H}",
            "-fps_mode",
            "passthrough",
            "-frames:v",
            str(count),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        capture=True,
    )
    data = np.frombuffer(proc.stdout, dtype=np.uint8).reshape(-1, CELL_H, CELL_W, 3)
    grid = Image.new("RGB", (GRID_COLS * CELL_W, GRID_ROWS * (CELL_H + 22)), (0, 0, 0))
    draw = ImageDraw.Draw(grid)
    times = [float(f / fps) for f in frames]
    for i, frame in enumerate(data[:count]):
        x, y = (i % GRID_COLS) * CELL_W, (i // GRID_COLS) * (CELL_H + 22)
        grid.paste(Image.fromarray(frame), (x, y + 22))
        draw.text((x + 4, y + 2), f"{times[i]:.1f}s", fill=(255, 220, 0), font=font(17))
    return grid, times


def local_moment(cand: Candidate, index: int) -> Moment:
    top = max(cand.segments, key=lambda s: s.final_score)
    seconds = sum(s.frames for s in cand.segments)
    return Moment(
        id=f"m{index:02d}",
        clip=cand.clip,
        flight=cand.flight,
        start=cand.start,
        end=cand.end,
        peak=(top.start + top.end) // 2,
        kind=(top.claude.tags[0] if top.claude and top.claude.tags else "move"),
        hold="full" if cand.score >= 8.0 or len(cand.segments) > 1 else "short",
        worth=round(cand.score, 2),
        segment_ids=[s.id for s in cand.segments],
        source="local",
        description="; ".join(s.claude.description for s in cand.segments if s.claude)[:300],
        reason=f"joined {len(cand.segments)} strong segment(s), {seconds} frames",
    )


def _from_answer(answer: dict, cand: Candidate, clip: ClipInfo, index: int) -> Moment | None:
    if answer.get("hold") == "skip":
        return None
    fps = clip.fps
    start = max(cand.look_start, seconds_to_frames(float(answer["in_s"]), fps))
    end = min(cand.look_end, seconds_to_frames(float(answer["out_s"]), fps))
    if end - start < seconds_to_frames(0.8, fps):
        return None
    peak = min(end - 1, max(start, seconds_to_frames(float(answer.get("payoff_s", 0) or 0), fps)))
    worth = float(min(10, max(1, answer.get("worth", cand.score))))
    return Moment(
        id=f"m{index:02d}",
        clip=cand.clip,
        flight=cand.flight,
        start=start,
        end=end,
        peak=peak,
        kind=str(answer.get("kind", "move")),
        hold="full" if answer.get("hold") == "full" else "short",
        worth=worth,
        description=str(answer.get("description", "")),
        reason=str(answer.get("reason", "")),
        segment_ids=[s.id for s in cand.segments],
        source="claude",
    )


def find_moments(
    project: Project,
    segments: list[Segment],
    extents: dict[str, tuple[int, int]],
    clips: dict[str, tuple[ClipInfo, Path]],
    *,
    claude: Claude | None,
    model: str,
    effort: str,
    progress=print,
) -> tuple[list[Moment], dict[str, int]]:
    infos = {cid: info for cid, (info, _) in clips.items()}
    cands = candidates(segments, extents, infos, candidate_limit(infos))
    stats = {"candidates": len(cands), "cached": 0, "looked": 0}
    answers: dict[int, dict | None] = {}
    todo = []
    for i, cand in enumerate(cands):
        cached = project.db.cached_moment(cache_key(infos[cand.clip].fingerprint, cand, model))
        if cached is not None:
            answers[i] = cached
            stats["cached"] += 1
        else:
            todo.append(i)
    if claude is not None and todo:
        progress(f"  second look at {len(todo)} candidate moment(s)")

        def look(i: int) -> tuple[int, dict]:
            cand = cands[i]
            info, proxy = clips[cand.clip]
            grid, times = look_grid(proxy, info, cand)
            notes = "\n".join(
                f"- {s.id} ({format_timecode(s.start, info.fps)}-{format_timecode(s.end, info.fps)}, "
                f"score {s.final_score:.1f}): {s.claude.description if s.claude else ''}"
                for s in cand.segments
            )
            content = [
                {
                    "type": "text",
                    "text": f"Clip {cand.clip}. The frames below run from {times[0]:.1f} s to "
                    f"{times[-1]:.1f} s of the clip, left to right, top to bottom; each is "
                    f"labelled with its time in the clip. The scout's strong segments here:\n"
                    f"{notes}",
                },
                image_block(grid),
                {"type": "text", "text": "Mark the move."},
            ]
            call, _ = claude.call_tool(
                step="moments",
                model=model,
                system=MOMENT_SYSTEM,
                messages=[{"role": "user", "content": content}],
                tool=MOMENT_TOOL,
                effort=effort,
                max_tokens=4000,
                cache_system=True,
            )
            return i, call.input

        with ThreadPoolExecutor(max_workers=4) as pool:
            for i, answer in pool.map(look, todo):
                cand = cands[i]
                info = infos[cand.clip]
                project.db.store_moment(
                    cache_key(info.fingerprint, cand, model),
                    fingerprint=info.fingerprint,
                    start=cand.look_start,
                    end=cand.look_end,
                    params=LOOK_PARAMS,
                    model=model,
                    prompt_version=MOMENT_PROMPT_VERSION,
                    result=answer,
                )
                answers[i] = answer
                stats["looked"] += 1
    moments: list[Moment] = []
    for i, cand in enumerate(cands):
        answer = answers.get(i)
        moment = (
            _from_answer(answer, cand, infos[cand.clip], len(moments) + 1)
            if answer
            else (local_moment(cand, len(moments) + 1) if claude is None or i not in answers else None)
        )
        if moment is not None:
            moments.append(moment)
    moments.sort(key=lambda m: (m.flight, m.clip, m.start))
    for n, moment in enumerate(moments, start=1):
        moment.id = f"m{n:02d}"
    project.db.execute("DELETE FROM moments WHERE project_id=?", (project.id,))
    for moment in moments:
        project.db.execute(
            "INSERT INTO moments (project_id, moment_id, clip_id, start_frame, end_frame, moment_json) "
            "VALUES (?,?,?,?,?,?)",
            (project.id, moment.id, moment.clip, moment.start, moment.end, moment.model_dump_json()),
        )
    project.write_json("analysis/moments.json", [m.model_dump() for m in moments])
    return moments, stats


def moment_segment(moment: Moment, segments: dict[str, Segment]) -> Segment:
    """A moment presented to the director and the EDL builder as a pick-able span."""
    parts = [segments[s] for s in moment.segment_ids if s in segments]
    middle = parts[len(parts) // 2] if parts else None
    return Segment(
        id=moment.id,
        clip=moment.clip,
        flight=moment.flight,
        start=moment.start,
        end=moment.end,
        kind="moment",
        signals=middle.signals if middle else SegmentSignals(),
        phash=middle.phash if middle else "0",
        final_score=moment.worth,
        local_score=moment.worth,
        claude=Score(score=moment.worth, tags=[moment.kind], description=moment.description, reason=moment.reason),
    )
