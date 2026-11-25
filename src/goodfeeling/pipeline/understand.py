"""Understand: one Claude call that writes a session summary from descriptions plus a contact sheet."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

from goodfeeling.ai.client import Claude, image_block
from goodfeeling.ai.prompts import UNDERSTAND_SYSTEM, UNDERSTAND_TOOL
from goodfeeling.pipeline.strips import GAP, LABEL_H, THUMB_W, contact_sheet
from goodfeeling.schemas.clip import ClipInfo
from goodfeeling.schemas.segment import Segment
from goodfeeling.timebase import format_timecode

log = logging.getLogger(__name__)

SHEET_SEGMENTS = 16


def sample_for_sheet(segments: list[Segment], count: int = SHEET_SEGMENTS) -> list[Segment]:
    """Top-scoring segments, topped up with an even spread so every flight shows."""
    ranked = sorted(segments, key=lambda s: -s.final_score)
    chosen = {s.id: s for s in ranked[: count // 2]}
    step = max(1, len(segments) // max(1, count - len(chosen)))
    for seg in segments[::step]:
        if len(chosen) >= count:
            break
        chosen.setdefault(seg.id, seg)
    return sorted(chosen.values(), key=lambda s: (s.flight, s.clip, s.start))


def thumb_from_strip(strip_path: Path, index: int = 1) -> Image.Image:
    strip = Image.open(strip_path)
    height = strip.size[1] - LABEL_H
    x = index * (THUMB_W + GAP)
    return strip.crop((x, LABEL_H, x + THUMB_W, LABEL_H + height))


def segment_table(segments: list[Segment], clips: dict[str, ClipInfo]) -> str:
    lines = ["id | flight | clip@time | seconds | score | tags | description"]
    for seg in segments:
        clip = clips[seg.clip]
        desc = seg.claude.description if seg.claude else "(not scored by Claude)"
        tags = ",".join(seg.claude.tags) if seg.claude else ""
        lines.append(
            f"{seg.id} | {seg.flight} | {seg.clip}@{format_timecode(seg.start, clip.fps)} | "
            f"{seg.frames / (clip.fps[0] / clip.fps[1]):.1f} | {seg.final_score:.1f} | {tags} | {desc}"
        )
    return "\n".join(lines)


def summarize(
    segments: list[Segment],
    clips: dict[str, ClipInfo],
    strips: dict[str, Path],
    *,
    claude: Claude | None,
    model: str,
    effort: str,
) -> tuple[dict, Image.Image | None]:
    picks = [s for s in sample_for_sheet(segments) if s.id in strips]
    sheet = contact_sheet([(s.id, thumb_from_strip(strips[s.id])) for s in picks]) if picks else None
    if claude is None:
        return offline_summary(segments, clips), sheet
    content = [{"type": "text", "text": "Segments:\n" + segment_table(segments, clips)}]
    if sheet is not None:
        content += [{"type": "text", "text": "Contact sheet (captions are segment ids):"}, image_block(sheet)]
    content.append({"type": "text", "text": "Write the session summary."})
    call, _ = claude.call_tool(
        step="understand",
        model=model,
        system=UNDERSTAND_SYSTEM,
        messages=[{"role": "user", "content": content}],
        tool=UNDERSTAND_TOOL,
        effort=effort,
        max_tokens=8000,
    )
    return {**call.input, "source": "claude"}, sheet


def offline_summary(segments: list[Segment], clips: dict[str, ClipInfo]) -> dict:
    flights = sorted({s.flight for s in segments})
    total = sum(s.frames / (clips[s.clip].fps[0] / clips[s.clip].fps[1]) for s in segments)
    best = sorted(segments, key=lambda s: -s.final_score)[:5]
    return {
        "headline": f"{len(flights)} flight(s), {len(segments)} usable segments",
        "where": "Unknown (no Claude summary; run with an API key for one).",
        "flights": f"{total / 60:.1f} minutes of usable footage across {len(clips)} clip(s).",
        "notable_moments": [{"segment_id": s.id, "moment": f"highest local score {s.final_score:.1f}"} for s in best],
        "mood": "energetic" if sum(s.signals.motion for s in segments) / max(1, len(segments)) > 0.2 else "calm",
        "suggested_story": "Chronological, with the strongest motion on the drop.",
        "source": "offline",
    }


def summary_markdown(summary: dict) -> str:
    lines = [
        f"# {summary.get('headline', 'Session summary')}",
        "",
        f"**Where:** {summary.get('where', '')}",
        "",
        f"**Flights:** {summary.get('flights', '')}",
        "",
        f"**Mood:** {summary.get('mood', '')}",
        "",
        "## Notable moments",
        "",
    ]
    lines += [f"- `{m['segment_id']}`: {m['moment']}" for m in summary.get("notable_moments", [])]
    lines += [
        "",
        "## Suggested story",
        "",
        summary.get("suggested_story", ""),
        "",
        f"_Source: {summary.get('source', 'unknown')}_",
        "",
    ]
    return "\n".join(lines)
