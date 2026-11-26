"""Decoding frames to numpy for analysis and to images for Claude."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from PIL import Image

from goodfeeling.media.ffmpeg import FFmpegError, ffmpeg_bin, run_ffmpeg
from goodfeeling.timebase import Rate, seek_seconds


def iter_samples(path: str | Path, sample_fps: float | str, height: int) -> Iterator[tuple[int, np.ndarray]]:
    """Yield (sample_index, RGB frame) at a fixed sample rate, scaled to `height`."""
    probe_w = _scaled_width(path, height)
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-an",
        "-vf",
        f"fps={sample_fps},scale={probe_w}:{height}:flags=area",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    frame_bytes = probe_w * height * 3
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
        index = 0
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield index, np.frombuffer(buf, dtype=np.uint8).reshape(height, probe_w, 3)
            index += 1
        proc.wait()
        if proc.returncode not in (0, None) and index == 0:
            raise FFmpegError(f"could not decode {path}: {proc.stderr.read().decode(errors='replace')[-500:]}")


def _scaled_width(path: str | Path, height: int) -> int:
    from goodfeeling.media.ffmpeg import ffprobe_json

    stream = ffprobe_json(path, "-select_streams", "v:0", "-show_entries", "stream=width,height")["streams"][0]
    width = round(int(stream["width"]) * height / int(stream["height"]))
    return width + (width % 2)


def extract_frame(path: str | Path, frame: int, fps: Rate, width: int) -> Image.Image:
    """Decode exactly frame `frame` (0-based) using accurate seek."""
    proc = run_ffmpeg(
        [
            "-ss",
            f"{seek_seconds(frame, fps):.6f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-an",
            "-vf",
            f"scale={width}:-2:flags=bicubic",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        capture=True,
    )
    from io import BytesIO

    return Image.open(BytesIO(proc.stdout)).convert("RGB")


def read_frames(path: str | Path, start: int, count: int, fps: Rate, width: int, height: int) -> np.ndarray:
    """Decode `count` consecutive frames starting at `start` as an RGB array."""
    proc = run_ffmpeg(
        [
            "-ss",
            f"{seek_seconds(start, fps):.6f}",
            "-i",
            str(path),
            "-frames:v",
            str(count),
            "-fps_mode",
            "passthrough",
            "-an",
            "-vf",
            f"scale={width}:{height}:flags=area",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        capture=True,
    )
    data = np.frombuffer(proc.stdout, dtype=np.uint8)
    return data.reshape(-1, height, width, 3)
