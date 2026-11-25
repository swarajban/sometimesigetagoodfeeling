"""Labelled frame strips (one per segment) and contact sheets."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from goodfeeling.media.ffmpeg import run_ffmpeg
from goodfeeling.timebase import Rate, seek_seconds

STRIP_FRAMES = 4
THUMB_W = 384
LABEL_H = 30
GAP = 4
FRAME_PARAMS = f"strip{STRIP_FRAMES}-{THUMB_W}w-q85-v1"


def font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def frames_at(
    path: Path, fps: Rate, start: int, offsets: list[int], width: int, src_w: int, src_h: int
) -> list[Image.Image]:
    """Decode frames start+offset (offsets ascending) in one pass with accurate seek."""
    height = round(width * src_h / src_w / 2) * 2
    select = "+".join(f"eq(n\\,{o})" for o in offsets)
    proc = run_ffmpeg(
        [
            "-ss",
            f"{seek_seconds(start, fps):.6f}",
            "-i",
            str(path),
            "-an",
            "-vf",
            f"select='{select}',scale={width}:{height}:flags=bicubic",
            "-fps_mode",
            "passthrough",
            "-frames:v",
            str(len(offsets)),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        capture=True,
    )
    data = np.frombuffer(proc.stdout, dtype=np.uint8).reshape(-1, height, width, 3)
    images = [Image.fromarray(frame) for frame in data]
    while len(images) < len(offsets) and images:
        images.append(images[-1])
    return images


def strip_offsets(frames: int, count: int = STRIP_FRAMES) -> list[int]:
    return sorted({min(frames - 1, int(frames * (2 * i + 1) / (2 * count))) for i in range(count)})


def make_strip(label: str, thumbs: list[Image.Image]) -> Image.Image:
    w, h = thumbs[0].size
    strip = Image.new("RGB", (len(thumbs) * w + (len(thumbs) - 1) * GAP, h + LABEL_H), (0, 0, 0))
    for i, thumb in enumerate(thumbs):
        strip.paste(thumb, (i * (w + GAP), LABEL_H))
    draw = ImageDraw.Draw(strip)
    draw.text((8, 3), f"SEGMENT {label}", fill=(255, 220, 0), font=font(22))
    return strip


def contact_sheet(items: list[tuple[str, Image.Image]], columns: int = 4, thumb_w: int = 320) -> Image.Image:
    thumbs = []
    for caption, image in items:
        thumb = image.copy()
        thumb.thumbnail((thumb_w, thumb_w))
        thumbs.append((caption, thumb))
    tw = thumb_w
    th = max(t.size[1] for _, t in thumbs) if thumbs else 180
    rows = max(1, -(-len(thumbs) // columns))
    caption_h = 26
    sheet = Image.new("RGB", (columns * (tw + GAP) + GAP, rows * (th + caption_h + GAP) + GAP), (16, 16, 16))
    draw = ImageDraw.Draw(sheet)
    for i, (caption, thumb) in enumerate(thumbs):
        x = GAP + (i % columns) * (tw + GAP)
        y = GAP + (i // columns) * (th + caption_h + GAP)
        sheet.paste(thumb, (x, y + caption_h))
        draw.text((x + 4, y + 3), caption, fill=(240, 240, 240), font=font(16))
    return sheet
