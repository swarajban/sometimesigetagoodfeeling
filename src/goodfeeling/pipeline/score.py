"""Score: one labelled strip per segment, a few segments per request, cached by content."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from goodfeeling.ai.client import Claude, image_block
from goodfeeling.ai.pricing import cost_usd, image_tokens, text_tokens
from goodfeeling.ai.prompts import SCORE_PROMPT_VERSION, SCORE_SYSTEM, SCORE_TOOL
from goodfeeling.pipeline.strips import (
    FRAME_PARAMS,
    GAP,
    LABEL_H,
    STRIP_FRAMES,
    THUMB_W,
    frames_at,
    make_strip,
    strip_offsets,
)
from goodfeeling.schemas.clip import ClipInfo
from goodfeeling.schemas.segment import Penalties, Score, Segment
from goodfeeling.store.project import Project
from goodfeeling.timebase import format_timecode

log = logging.getLogger(__name__)

OUTPUT_TOKENS_PER_SEGMENT = 160
THINKING_TOKENS_PER_REQUEST = 400


def cache_key(fingerprint: str, seg: Segment, model: str) -> str:
    raw = f"{fingerprint}|{seg.start}|{seg.end}|{FRAME_PARAMS}|{model}|{SCORE_PROMPT_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class Estimate:
    step: str
    model: str
    requests: int
    input_tokens: int
    output_tokens: int

    @property
    def cost(self) -> float:
        return cost_usd(self.model, self.input_tokens, self.output_tokens)


def strip_tokens() -> int:
    width = STRIP_FRAMES * THUMB_W + (STRIP_FRAMES - 1) * GAP
    return image_tokens(width, round(THUMB_W * 9 / 16) + LABEL_H)


def estimate_scoring(uncached: int, model: str, batch: int) -> Estimate:
    requests = math.ceil(uncached / batch) if uncached else 0
    prefix = text_tokens(SCORE_SYSTEM) + text_tokens(json.dumps(SCORE_TOOL)) + 300
    per_segment = strip_tokens() + 90
    input_tokens = requests * prefix + uncached * per_segment
    output_tokens = uncached * OUTPUT_TOKENS_PER_SEGMENT + requests * THINKING_TOKENS_PER_REQUEST
    return Estimate("score", model, requests, input_tokens, output_tokens)


def segment_note(seg: Segment, clip: ClipInfo) -> str:
    s = seg.signals
    level = "high" if s.motion > 0.35 else "moderate" if s.motion > 0.12 else "low"
    return (
        f"Segment {seg.id}: flight {seg.flight}, clip {seg.clip} at {format_timecode(seg.start, clip.fps)}, "
        f"{seg.frames / (clip.fps[0] / clip.fps[1]):.1f} s, kind {seg.kind}. Local: motion {level} "
        f"({s.motion:.2f}), sharpness {'low' if s.blur else 'ok'}, crash heuristic {'YES' if s.crash else 'no'}."
    )


def build_strip(project: Project, seg: Segment, clip: ClipInfo, proxy: Path) -> Path:
    out = project.path("frames", "strips", f"{clip.fingerprint}-{seg.start}-{seg.end}-{FRAME_PARAMS}.jpg")
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    pw = round(clip.width * 720 / clip.height / 2) * 2
    thumbs = frames_at(proxy, clip.fps, seg.start, strip_offsets(seg.frames), THUMB_W, pw, 720)
    make_strip(seg.id, thumbs).save(out, quality=85)
    return out


def _parse(result: dict, wanted: set[str], model: str) -> dict[str, Score]:
    scores = {}
    for entry in result.get("scores", []):
        sid = str(entry.get("segment_id", "")).strip()
        if sid not in wanted or sid in scores:
            continue
        scores[sid] = Score(
            score=float(min(10, max(1, int(round(float(entry["score"])))))),
            tags=[str(t).lower() for t in entry.get("tags", [])][:8],
            description=str(entry.get("description", "")),
            reason=str(entry.get("reason", "")),
            penalties=Penalties(**entry.get("penalties", {})),
            model=model,
        )
    return scores


def score_segments(
    project: Project,
    segments: list[Segment],
    clips: dict[str, tuple[ClipInfo, Path]],
    *,
    claude: Claude | None,
    model: str,
    effort: str,
    batch_size: int,
    progress=print,
    parallel: int = 4,
) -> dict[str, int]:
    """Attach Claude scores to segments: from cache when possible, otherwise in batches."""
    stats = {"cached": 0, "scored": 0, "unscored": 0, "requests": 0}
    todo = []
    for seg in segments:
        clip = clips[seg.clip][0]
        cached = project.db.cached_score(cache_key(clip.fingerprint, seg, model))
        if cached:
            seg.claude = Score.model_validate(cached)
            stats["cached"] += 1
        else:
            todo.append(seg)
    if not todo or claude is None:
        stats["unscored"] = len(todo)
        return stats
    progress(f"  building {len(todo)} frame strips")
    strips = {seg.id: build_strip(project, seg, clips[seg.clip][0], clips[seg.clip][1]) for seg in todo}
    batches = [todo[i : i + batch_size] for i in range(0, len(todo), batch_size)]

    def request(batch: list[Segment]) -> dict[str, Score]:
        from PIL import Image

        content = []
        for seg in batch:
            content.append({"type": "text", "text": segment_note(seg, clips[seg.clip][0])})
            content.append(image_block(Image.open(strips[seg.id])))
        ids = ", ".join(seg.id for seg in batch)
        content.append({"type": "text", "text": f"Score these {len(batch)} segments: {ids}."})
        call, _ = claude.call_tool(
            step="score",
            model=model,
            system=SCORE_SYSTEM,
            messages=[{"role": "user", "content": content}],
            tool=SCORE_TOOL,
            effort=effort,
            max_tokens=8000,
            cache_system=True,
        )
        return _parse(call.input, {seg.id for seg in batch}, model)

    results: dict[str, Score] = {}
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for i, scores in enumerate(pool.map(request, batches)):
            results.update(scores)
            progress(f"  scored batch {i + 1}/{len(batches)}")
    stats["requests"] = len(batches)
    missing = [seg for seg in todo if seg.id not in results]
    if missing:
        log.warning("re-asking for %d segments Claude skipped", len(missing))
        for batch in [missing[i : i + batch_size] for i in range(0, len(missing), batch_size)]:
            results.update(request(batch))
            stats["requests"] += 1
    for seg in todo:
        score = results.get(seg.id)
        if score is None:
            stats["unscored"] += 1
            continue
        seg.claude = score
        clip = clips[seg.clip][0]
        project.db.store_score(
            cache_key(clip.fingerprint, seg, model),
            fingerprint=clip.fingerprint,
            in_frame=seg.start,
            out_frame=seg.end,
            frame_params=FRAME_PARAMS,
            model=model,
            prompt_version=SCORE_PROMPT_VERSION,
            result=score.model_dump(),
        )
        stats["scored"] += 1
    return stats
