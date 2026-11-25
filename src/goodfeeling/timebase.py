"""Integer frames at rational frame rates. Float seconds never decide a cut."""

from __future__ import annotations

import math
import re
from fractions import Fraction

Rate = tuple[int, int]

_TIMECODE = re.compile(r"^(?:(\d+):)?(\d+):(\d{1,2})(?:\.(\d{1,6}))?$")


def as_fraction(fps: Rate | Fraction) -> Fraction:
    if isinstance(fps, Fraction):
        return fps
    num, den = fps
    return Fraction(int(num), int(den))


def normalize_rate(fps: Rate | Fraction | str) -> Rate:
    if isinstance(fps, str):
        num, _, den = fps.partition("/")
        value = Fraction(int(num), int(den or 1))
    else:
        value = as_fraction(fps)
    if value <= 0:
        raise ValueError(f"frame rate must be positive, got {fps}")
    return (value.numerator, value.denominator)


def round_half_up(value: Fraction) -> int:
    return math.floor(value + Fraction(1, 2))


def frames_to_seconds(frames: int, fps: Rate) -> Fraction:
    return Fraction(frames) / as_fraction(fps)


def seconds_to_frames(seconds: float | Fraction, fps: Rate) -> int:
    if not isinstance(seconds, Fraction):
        seconds = Fraction(seconds).limit_denominator(10**9)
    return round_half_up(seconds * as_fraction(fps))


def seek_seconds(frame: int, fps: Rate) -> float:
    """Seek target that makes ffmpeg's accurate seek return exactly `frame` first.

    Aiming half a frame early keeps timestamp rounding from skipping the wanted frame.
    """
    return max(0.0, float((Fraction(frame) - Fraction(1, 2)) / as_fraction(fps)))


def parse_timecode(text: str) -> Fraction:
    match = _TIMECODE.match(text.strip())
    if not match:
        raise ValueError(f"expected MM:SS.mmm or HH:MM:SS.mmm, got {text!r}")
    hours, minutes, seconds, fraction = match.groups()
    total = Fraction(int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds))
    if fraction:
        total += Fraction(int(fraction), 10 ** len(fraction))
    return total


def format_timecode(frames: int, fps: Rate) -> str:
    millis = round_half_up(frames_to_seconds(frames, fps) * 1000)
    minutes, rem = divmod(millis, 60_000)
    return f"{minutes:02d}:{rem // 1000:02d}.{rem % 1000:03d}"


def output_frames_for_source(source_frames: int, src_fps: Rate, out_fps: Rate, speed: float) -> int:
    """Timeline length of a source span played at a constant speed."""
    ratio = as_fraction(src_fps) * Fraction(speed).limit_denominator(1000) / as_fraction(out_fps)
    return round_half_up(Fraction(source_frames) / ratio)


def source_frames_for_output(output_frames: int, src_fps: Rate, out_fps: Rate, speed: float) -> int:
    """Smallest source span whose timeline length is exactly `output_frames`."""
    ratio = as_fraction(src_fps) * Fraction(speed).limit_denominator(1000) / as_fraction(out_fps)
    frames = max(0, math.ceil((Fraction(output_frames) - Fraction(1, 2)) * ratio))
    while output_frames_for_source(frames, src_fps, out_fps, speed) < output_frames:
        frames += 1
    return frames


def source_per_output(src_fps: Rate, out_fps: Rate, speed: float) -> Fraction:
    return as_fraction(src_fps) * Fraction(speed).limit_denominator(1000) / as_fraction(out_fps)


def mp4_timescale(fps: Rate) -> int:
    num, den = normalize_rate(fps)
    return num if den != 1 else num * 1000
