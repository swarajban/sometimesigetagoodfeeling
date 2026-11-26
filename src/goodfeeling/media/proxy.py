"""Seek-friendly 8-bit proxies: 720p H.264, CFR, no B-frames, a keyframe every 15 frames."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from goodfeeling.media.color import REC709_TAGS, clip_color_chain
from goodfeeling.media.ffmpeg import count_video_packets, run_ffmpeg
from goodfeeling.schemas.clip import ClipInfo

log = logging.getLogger(__name__)

PROXY_HEIGHT = 720
PROXY_VERSION = "p1"
PROXY_THREADS = 8


def proxy_filters(clip: ClipInfo, lut: str | None) -> str:
    chain = clip_color_chain(clip, lut)
    chain += [f"scale=-2:{PROXY_HEIGHT}:flags=bicubic", "setsar=1", "format=yuv420p"]
    return ",".join(chain)


def make_proxy(clip: ClipInfo, out: Path, lut: str | None = None) -> Path:
    """Encode a proxy whose frame N is the source's frame N; resumable via temp-then-rename."""
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")
    num, den = clip.fps
    run_ffmpeg(
        [
            "-i",
            clip.path,
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            proxy_filters(clip, lut),
            "-fps_mode",
            "cfr",
            "-r",
            f"{num}/{den}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-profile:v",
            "high",
            "-bf",
            "0",
            "-g",
            "15",
            "-keyint_min",
            "15",
            "-sc_threshold",
            "0",
            "-threads",
            str(PROXY_THREADS),
            *REC709_TAGS,
            "-movflags",
            "+faststart",
            str(tmp),
        ]
    )
    produced = count_video_packets(tmp)
    if abs(produced - clip.frames) > 1:
        log.warning("proxy for %s has %d frames, source reports %d", clip.path, produced, clip.frames)
    os.replace(tmp, out)
    return out
