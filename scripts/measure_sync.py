#!/usr/bin/env python3
"""Measure how well a rendered edit's cuts sit on the music's beats, from the rendered file.

Each cut is located in the picture by finding, within a few frames of where the EDL puts it, the output frame
that matches the incoming shot's first source frame (scaled and cropped the way the renderer frames it; plain
frame differences mislead on fast FPV motion). Beats come from re-running beat tracking on the rendered file's
own audio. A drift shows up as a non-zero slope of offset against time.

    uv run python scripts/measure_sync.py final.mp4 edl.json --tempo 126 [--json out.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from goodfeeling.media.frames import read_frames
from goodfeeling.pipeline.music import analyze_music
from goodfeeling.pipeline.strips import crop_to
from goodfeeling.schemas.edl import EDL
from goodfeeling.timebase import as_fraction

SEARCH = 3


def small(image: Image.Image, portrait: bool) -> np.ndarray:
    return np.asarray(image.resize((90, 160) if portrait else (160, 90)), dtype=np.float32)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("edl", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--tempo", type=float, default=120.0, help="Tempo hint for the beat tracker (BPM).")
    args = ap.parse_args()
    edl = EDL.load(args.edl)
    fps = as_fraction(edl.output.fps)
    w, h = edl.output.width, edl.output.height
    portrait = h > w
    beats = np.array(analyze_music(args.video, start_bpm=args.tempo).beats)
    rows = []
    for entry in edl.timeline()[1:]:
        shot = entry.shot
        if shot.transition != "cut":
            continue
        source = edl.sources[shot.clip]
        reference = read_frames(source.stabilized_path or source.path, shot.in_, 1, source.fps, 640, 360)[0]
        reference = small(crop_to(Image.fromarray(reference), w, h, shot.crop_x), portrait)
        lo = max(0, entry.start - SEARCH)
        window = read_frames(args.video, lo, 2 * SEARCH + 1, edl.output.fps, w // 4, h // 4)
        errors = [float(np.abs(small(Image.fromarray(f), portrait) - reference).mean()) for f in window]
        found = lo + int(np.argmin(errors))
        cut_s = float(found / fps)
        beat = float(beats[np.argmin(np.abs(beats - cut_s))])
        rows.append(
            {
                "shot": shot.id,
                "planned_frame": entry.start,
                "detected_frame": found,
                "match_error": round(min(errors), 2),
                "cut_s": round(cut_s, 4),
                "nearest_beat_s": round(beat, 4),
                "offset_ms": round((cut_s - beat) * 1000, 2),
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
    print(f"\n{'shot':<5} {'planned':>8} {'found':>6} {'match':>6} {'cut s':>9} {'beat s':>9} {'offset ms':>10}")
    for r in rows:
        print(
            f"{r['shot']:<5} {r['planned_frame']:>8} {r['detected_frame']:>6} {r['match_error']:>6.1f} "
            f"{r['cut_s']:>9.3f} {r['nearest_beat_s']:>9.3f} {r['offset_ms']:>10.2f}"
        )
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "cuts": rows}, indent=2) + "\n")


if __name__ == "__main__":
    main()
