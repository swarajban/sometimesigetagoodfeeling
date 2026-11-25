from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path

from goodfeeling.media.ffmpeg import ffprobe_json
from goodfeeling.media.fingerprint import fingerprint
from goodfeeling.schemas.clip import ClipInfo
from goodfeeling.timebase import normalize_rate, seconds_to_frames

_DEPTH = re.compile(r"p(\d{2})(?:le|be)$")


def bit_depth(pix_fmt: str, bits_per_raw_sample: str | None) -> int:
    if bits_per_raw_sample and bits_per_raw_sample.isdigit():
        return int(bits_per_raw_sample)
    match = _DEPTH.search(pix_fmt or "")
    return int(match.group(1)) if match else 8


def rotation_of(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            return int(round(float(side["rotation"]))) % 360
    tag = (stream.get("tags") or {}).get("rotate")
    return int(tag) % 360 if tag else 0


def probe(path: str | Path) -> ClipInfo:
    path = Path(path)
    data = ffprobe_json(path, "-show_format", "-show_streams")
    video = next(
        (
            s
            for s in data.get("streams", [])
            if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise ValueError(f"{path} has no video stream")
    fmt = data.get("format", {})
    fps = normalize_rate(video.get("r_frame_rate") or video.get("avg_frame_rate"))
    avg = video.get("avg_frame_rate") or "0/1"
    avg_value = Fraction(*map(int, avg.split("/"))) if avg not in ("0/0", "0/1") else Fraction(0)
    vfr = bool(avg_value) and abs(float(avg_value) - fps[0] / fps[1]) / (fps[0] / fps[1]) > 0.005
    if vfr and fps[0] / fps[1] / float(avg_value) >= 1.5:
        fps = normalize_rate(Fraction(avg_value).limit_denominator(1001))
    duration = float(video.get("duration") or fmt.get("duration") or 0.0)
    frames = int(video["nb_frames"]) if str(video.get("nb_frames", "")).isdigit() else 0
    if frames <= 0:
        frames = seconds_to_frames(duration, fps)
    tags = {**(fmt.get("tags") or {}), **(video.get("tags") or {})}
    width, height = int(video["width"]), int(video["height"])
    rotation = rotation_of(video)
    if rotation in (90, 270):
        width, height = height, width
    return ClipInfo(
        path=str(path.resolve()),
        size=path.stat().st_size,
        fingerprint=fingerprint(path),
        codec=video.get("codec_name", "unknown"),
        profile=video.get("profile"),
        width=width,
        height=height,
        fps=fps,
        frames=frames,
        duration_s=duration,
        pix_fmt=video.get("pix_fmt", "unknown"),
        bit_depth=bit_depth(video.get("pix_fmt", ""), video.get("bits_per_raw_sample")),
        color_transfer=video.get("color_transfer"),
        color_primaries=video.get("color_primaries"),
        color_space=video.get("color_space"),
        color_range=video.get("color_range"),
        rotation=rotation,
        creation_time=tags.get("creation_time"),
        has_audio=any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        variable_frame_rate=vfr,
    )
