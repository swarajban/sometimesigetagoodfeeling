"""Local signals on ~160p frames at ~5 fps: flow, coherence, rotation, sharpness, exposure, crashes."""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np
from pydantic import BaseModel

from goodfeeling.media.frames import iter_samples
from goodfeeling.timebase import Rate, seconds_to_frames

log = logging.getLogger(__name__)

SAMPLE_FPS = 5.0
SAMPLE_HEIGHT = 160
SIGNALS_VERSION = "s1"


class ClipSignals(BaseModel):
    version: str = SIGNALS_VERSION
    fingerprint: str
    sample_fps: float
    frames: list[int]
    motion: list[float]
    coherence: list[float]
    rotation: list[float]
    sharpness: list[float]
    exposure: list[float]
    clipped: list[float]
    dhash: list[str]
    scene_cuts: list[int]

    def times(self) -> np.ndarray:
        return np.arange(len(self.frames)) / self.sample_fps


def dhash(gray: np.ndarray) -> str:
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _flow_stats(prev: np.ndarray, cur: np.ndarray) -> tuple[float, float, float]:
    flow = cv2.calcOpticalFlowFarneback(prev, cur, None, 0.5, 3, 15, 3, 5, 1.2, 0)
    width = prev.shape[1]
    mag = np.linalg.norm(flow, axis=2)
    mean_mag = float(mag.mean()) / width
    mean_vec = flow.reshape(-1, 2).mean(axis=0)
    coherence = float(np.linalg.norm(mean_vec) / (mag.mean() + 1e-6))
    h, w = prev.shape
    ys, xs = np.mgrid[0:h, 0:w]
    rx, ry = xs - w / 2, ys - h / 2
    radius2 = rx * rx + ry * ry + 1e-6
    angular = (rx * flow[..., 1] - ry * flow[..., 0]) / radius2
    return mean_mag, min(1.0, coherence), float(np.median(angular))


def scene_cuts(proxy: Path) -> list[int]:
    """PySceneDetect content cuts as frame indices (a cheap signal; FPV has few)."""
    try:
        from scenedetect import ContentDetector, detect
    except ImportError:
        return []
    try:
        scenes = detect(str(proxy), ContentDetector(threshold=27.0), show_progress=False)
    except Exception as exc:
        log.warning("scene detection failed on %s: %s", proxy, exc)
        return []
    return [int(start.frame_num) for start, _ in scenes[1:]]


def analyze_clip(proxy: Path, fps: Rate, fingerprint: str) -> ClipSignals:
    motion, coherence, rotation, sharpness, exposure, clipped, hashes, frames = [], [], [], [], [], [], [], []
    prev = None
    for index, rgb in iter_samples(proxy, SAMPLE_FPS, SAMPLE_HEIGHT):
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        frames.append(seconds_to_frames(index / SAMPLE_FPS, fps))
        if prev is None:
            motion.append(0.0)
            coherence.append(0.0)
            rotation.append(0.0)
        else:
            m, c, r = _flow_stats(prev, gray)
            motion.append(m * SAMPLE_FPS)
            coherence.append(c)
            rotation.append(r * SAMPLE_FPS)
        sharpness.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
        exposure.append(float(gray.mean()) / 255.0)
        clipped.append(float(((gray < 8) | (gray > 247)).mean()))
        hashes.append(dhash(gray))
        prev = gray
    if len(motion) > 1:
        motion[0], coherence[0], rotation[0] = motion[1], coherence[1], rotation[1]
    return ClipSignals(
        fingerprint=fingerprint,
        sample_fps=SAMPLE_FPS,
        frames=frames,
        motion=motion,
        coherence=coherence,
        rotation=rotation,
        sharpness=sharpness,
        exposure=exposure,
        clipped=clipped,
        dhash=hashes,
        scene_cuts=scene_cuts(proxy),
    )


def smooth(values: np.ndarray, width: int) -> np.ndarray:
    if len(values) == 0 or width <= 1:
        return values
    kernel = np.ones(width) / width
    padded = np.pad(values, (width // 2, width - 1 - width // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def crash_flags(sig: ClipSignals) -> np.ndarray:
    """Per-sample crash indicator: tumble, sudden flow reversal, or sharpness collapse after motion."""
    motion = np.asarray(sig.motion)
    rot = np.abs(np.asarray(sig.rotation))
    sharp = np.asarray(sig.sharpness)
    coh = np.asarray(sig.coherence)
    n = len(motion)
    flags = np.zeros(n, dtype=bool)
    if n < 3:
        return flags
    busy = np.percentile(motion, 75)
    baseline = np.array([np.median(sharp[max(0, i - 10) : i]) if i > 0 else sharp[0] for i in range(n)])
    tumble = (rot > 1.5) & (coh < 0.35) & (motion > busy)
    collapse = (sharp < 0.25 * baseline) & (np.roll(motion, 1) > busy)
    spike = motion > max(4 * np.median(motion), 0.6)
    after = np.zeros(n, dtype=bool)
    for i in range(1, n - 3):
        if spike[i] and motion[i + 1 : i + 4].mean() < 0.05:
            after[i] = True
    flags = tumble | collapse | after
    flags[0] = False
    return flags


def idle_spans(sig: ClipSignals, min_seconds: float = 3.0, threshold: float = 0.01) -> list[tuple[int, int]]:
    """Sample index spans where nothing moves (sitting on the ground, hovering still)."""
    motion = smooth(np.asarray(sig.motion), 5)
    spans, start = [], None
    for i, value in enumerate(motion):
        if value < threshold and start is None:
            start = i
        elif value >= threshold and start is not None:
            if (i - start) / sig.sample_fps >= min_seconds:
                spans.append((start, i))
            start = None
    if start is not None and (len(motion) - start) / sig.sample_fps >= min_seconds:
        spans.append((start, len(motion)))
    return spans
