"""`goodfeeling doctor`: what this machine can do, and what a given file looks like."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

from goodfeeling import DISPLAY_NAME, __version__
from goodfeeling.media.encoders import KNOWN, choose_encoder, encoder_works, listed_encoders
from goodfeeling.media.ffmpeg import available_filters, ffmpeg_version, ffprobe_json
from goodfeeling.media.probe import probe
from goodfeeling.pipeline.ingest import parse_dji_name
from goodfeeling.settings import Settings, config_path


def system_report(settings: Settings) -> list[str]:
    lines = [f"{DISPLAY_NAME} {__version__}", f"python {sys.version.split()[0]} on {platform.platform()}"]
    ffmpeg = shutil.which("ffmpeg")
    lines.append(f"ffmpeg: {ffmpeg or 'NOT FOUND (brew install ffmpeg)'}")
    if not ffmpeg:
        return lines
    lines.append(f"  {ffmpeg_version()}")
    lines.append(f"ffprobe: {shutil.which('ffprobe') or 'NOT FOUND'}")
    lines.append("encoders:")
    listed = listed_encoders()
    for name in KNOWN:
        state = "works" if encoder_works(name) else ("listed but failed a test encode" if name in listed else "absent")
        lines.append(f"  {name:<18} {state}")
    try:
        chosen = choose_encoder(settings.render, 1080)
        lines.append(f"  final renders use: {chosen.name} ({' '.join(chosen.args)})")
    except RuntimeError as exc:
        lines.append(f"  final renders: {exc}")
    filters = available_filters()
    lines.append(
        "filters: "
        + ", ".join(
            f"{f} {'yes' if f in filters else 'NO'}" for f in ("xfade", "lut3d", "zscale", "tonemap", "fps", "tpad")
        )
    )
    root = settings.resolved_data_root()
    lines.append(f"data root: {root} ({'exists' if root.exists() else 'will be created'})")
    lines.append(f"settings file: {config_path()} ({'found' if config_path().exists() else 'not found, defaults'})")
    key_from = (
        "ANTHROPIC_API_KEY"
        if os.environ.get("ANTHROPIC_API_KEY")
        else ("settings file" if settings.anthropic_api_key else None)
    )
    lines.append(f"Claude API key: {'set via ' + key_from if key_from else 'not set (runs use local signals only)'}")
    m = settings.models
    lines.append(f"models: score={m.score} understand={m.understand} direct={m.direct}")
    return lines


def file_report(path: Path) -> list[str]:
    info = probe(path)
    raw = ffprobe_json(path, "-show_format", "-show_streams")
    video = next(s for s in raw["streams"] if s.get("codec_type") == "video")
    name_time, seq = parse_dji_name(path.name)
    fps = info.fps[0] / info.fps[1]
    lines = [
        f"file: {info.path}",
        f"  size: {info.size} bytes, fingerprint {info.fingerprint}",
        f"  codec: {info.codec} profile {info.profile or '-'}, tag {video.get('codec_tag_string', '-')}",
        f"  resolution: {info.width}x{info.height}, rotation {info.rotation}",
        f"  frame rate: {info.fps[0]}/{info.fps[1]} ({fps:.3f} fps), avg {video.get('avg_frame_rate')}"
        + (" VARIABLE" if info.variable_frame_rate else ""),
        f"  frames: {info.frames}, duration {info.duration_s:.3f} s",
        f"  pixel format: {info.pix_fmt}, bit depth {info.bit_depth}",
        f"  colour: transfer {info.color_transfer or '-'}, primaries {info.color_primaries or '-'}, "
        f"matrix {info.color_space or '-'}, range {info.color_range or '-'}",
        f"  creation_time: {info.creation_time or '-'}",
        f"  audio: {'yes' if info.has_audio else 'no'}",
        "  DJI name: "
        + (
            f"time {name_time.isoformat() if name_time else '-'}, sequence {seq}"
            if seq is not None
            else "not a DJI-style name"
        ),
    ]
    tags = {**(raw.get("format", {}).get("tags") or {}), **(video.get("tags") or {})}
    if tags:
        lines.append("  tags: " + ", ".join(f"{k}={v}" for k, v in sorted(tags.items())))
    hints = []
    if info.bit_depth > 8:
        hints.append(f"{info.bit_depth}-bit source: proxies and renders convert to 8-bit yuv420p")
    if info.is_hdr:
        hints.append(f"{info.color_transfer} transfer: tone-mapped to Rec.709 (needs zscale)")
    elif info.bit_depth > 8:
        hints.append(
            "10-bit without an HDR transfer may be D-Log M, which DJI does not tag; if the image looks "
            'flat, set "lut" in settings to DJI\'s D-Log M to Rec.709 LUT'
        )
    if info.variable_frame_rate:
        hints.append("variable frame rate: frame indices are computed from the nominal rate")
    lines += [f"  note: {h}" for h in hints]
    return lines
