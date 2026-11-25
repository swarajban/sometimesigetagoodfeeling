"""Tiny synthetic fixtures made with ffmpeg: two NTSC-rate clips with a frame-index barcode, and a click track."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

FPS = "60000/1001"
WIDTH, HEIGHT = 640, 360
BITS, BAR_H = 16, 40
TEMPO = 120.0
FIRST_CLICK = 0.25


def _clip(path: Path, seconds: float, scroll: str, creation: str) -> None:
    graph = (
        f"[0:v]scroll={scroll},format=yuv420p[bg];"
        f"[1:v]geq=lum='if(mod(floor(N/pow(2,floor(X/{WIDTH // BITS}))),2),235,16)':cb=128:cr=128[bar];"
        f"[bg][bar]overlay=0:{HEIGHT - BAR_H}:shortest=1"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"nullsrc=size={WIDTH}x{BAR_H}:rate={FPS}:duration={seconds}",
            "-filter_complex",
            graph,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "12",
            "-pix_fmt",
            "yuv420p",
            "-metadata",
            f"creation_time={creation}",
            str(path),
        ],
        check=True,
    )


def click_track(path: Path, seconds: float, loud_from: float) -> None:
    sr = 44100
    y = np.zeros(int(sr * seconds), dtype=np.float32)
    t = np.arange(int(0.02 * sr)) / sr
    k = 0
    while FIRST_CLICK + k * 60 / TEMPO < seconds - 0.1:
        tc = FIRST_CLICK + k * 60 / TEMPO
        amp = (1.0 if k % 4 == 0 else 0.5) * (1.0 if tc >= loud_from else 0.3)
        i = int(round(tc * sr))
        y[i : i + len(t)] += amp * np.sin(2 * np.pi * 1000 * t) * np.exp(-t * 200)
        k += 1
    noise = np.random.default_rng(0).standard_normal(len(y) - int(loud_from * sr)).astype(np.float32)
    y[int(loud_from * sr) :] += 0.05 * noise
    sf.write(path, y, sr)


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("media")
    footage = root / "footage"
    footage.mkdir()
    _clip(footage / "DJI_20250101120000_0001_D.MP4", 16, "h=0.004", "2025-01-01T12:00:00Z")
    _clip(footage / "DJI_20250101120016_0002_D.MP4", 16, "v=0.004", "2025-01-01T12:00:16Z")
    music = root / "click.wav"
    click_track(music, 30, loud_from=12)
    return {"footage": footage, "music": music, "root": root}


def read_barcode(frame: np.ndarray, scale: int) -> int:
    row = frame[(HEIGHT - BAR_H // 2) * scale, :, 0]
    step = WIDTH // BITS * scale
    return sum(1 << b for b in range(BITS) if row[b * step + step // 2] > 128)
