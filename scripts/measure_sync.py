#!/usr/bin/env python3
"""Measure how well a rendered edit's cuts sit on the music's beats, from the rendered file alone.

Cuts are found in the picture (largest frame difference within a few frames of each cut the EDL
predicts), beats are found by re-running beat tracking on the rendered file's own audio, and the two
are compared. A drift shows up as a non-zero slope of offset against time.

    uv run python scripts/measure_sync.py final.mp4 edl.json [--json out.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from goodfeeling.media.frames import iter_samples
from goodfeeling.pipeline.music import analyze_music
from goodfeeling.schemas.edl import EDL
from goodfeeling.timebase import as_fraction

SEARCH = 6


def frame_differences(video: Path, fps: float) -> np.ndarray:
    frames = [f[..., 1].astype(np.float32) for _, f in iter_samples(video, fps, 144)]
    return np.array([0.0] + [float(np.abs(b - a).mean()) for a, b in zip(frames, frames[1:], strict=False)])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("edl", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--tempo", type=float, default=120.0, help="Tempo hint for the beat tracker (BPM).")
    args = ap.parse_args()
    edl = EDL.load(args.edl)
    fps = as_fraction(edl.output.fps)
    diffs = frame_differences(args.video, float(fps))
    beats = np.array(analyze_music(args.video, start_bpm=args.tempo).beats)
    rows = []
    for entry in edl.timeline()[1:]:
        if entry.shot.transition != "cut":
            continue
        lo, hi = max(1, entry.start - SEARCH), min(len(diffs) - 1, entry.start + SEARCH)
        found = lo + int(np.argmax(diffs[lo : hi + 1]))
        cut_s = float(found / fps)
        beat = float(beats[np.argmin(np.abs(beats - cut_s))])
        rows.append(
            {
                "shot": entry.shot.id,
                "planned_frame": entry.start,
                "detected_frame": found,
                "cut_s": round(cut_s, 4),
                "nearest_beat_s": round(beat, 4),
                "offset_ms": round((cut_s - beat) * 1000, 2),
                "offset_frames": round((cut_s - beat) * float(fps), 3),
            }
        )
    offsets = np.array([r["offset_ms"] for r in rows])
    times = np.array([r["cut_s"] for r in rows])
    slope = float(np.polyfit(times, offsets, 1)[0]) * 60 if len(rows) > 2 else 0.0
    summary = {
        "cuts_measured": len(rows),
        "detected_equals_planned": sum(r["planned_frame"] == r["detected_frame"] for r in rows),
        "frame_ms": round(1000 / float(fps), 3),
        "mean_abs_offset_ms": round(float(np.abs(offsets).mean()), 2) if rows else None,
        "max_abs_offset_ms": round(float(np.abs(offsets).max()), 2) if rows else None,
        "drift_ms_per_minute": round(slope, 3),
        "first_half_mean_ms": round(float(offsets[: len(rows) // 2].mean()), 2) if rows else None,
        "second_half_mean_ms": round(float(offsets[len(rows) // 2 :].mean()), 2) if rows else None,
    }
    print(json.dumps(summary, indent=2))
    print(f"\n{'shot':<5} {'planned':>8} {'found':>6} {'cut s':>9} {'beat s':>9} {'offset ms':>10}")
    for r in rows:
        print(
            f"{r['shot']:<5} {r['planned_frame']:>8} {r['detected_frame']:>6} {r['cut_s']:>9.3f} "
            f"{r['nearest_beat_s']:>9.3f} {r['offset_ms']:>10.2f}"
        )
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "cuts": rows}, indent=2) + "\n")


if __name__ == "__main__":
    main()
