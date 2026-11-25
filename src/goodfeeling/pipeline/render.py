"""Render an EDL: per-piece cached intermediates with identical stream parameters, joined by stream copy.

Each shot becomes a body piece; each crossfade becomes its own short piece built from handles outside the
neighbouring shots' beat-aligned spans, centred on the cut, so it never moves a later cut off the beat.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from goodfeeling.media.color import REC709_TAGS, to_rec709
from goodfeeling.media.encoders import Encoder
from goodfeeling.media.ffmpeg import count_video_packets, ffprobe_json, run_ffmpeg
from goodfeeling.media.fingerprint import fingerprint
from goodfeeling.schemas.edl import EDL, Shot
from goodfeeling.timebase import as_fraction, mp4_timescale, seek_seconds

log = logging.getLogger(__name__)

RENDER_VERSION = "r1"


@dataclass
class Span:
    shot: Shot
    rel_start: int
    count: int


@dataclass
class Piece:
    kind: str
    start: int
    count: int
    spans: list[Span]
    fade_in: int = 0
    fade_out: int = 0


def render_problems(edl: EDL) -> list[tuple[int, str]]:
    """Rules any EDL must meet to render, hand-edited ones included: (shot index, problem)."""
    problems = [
        (i, f"shot {s.id}: speed ramps (more than one keyframe) arrive in phase 3")
        for i, s in enumerate(edl.shots)
        if len(s.speed) != 1
    ]
    timeline = edl.timeline()
    out = as_fraction(edl.output.fps)
    for prev, cur in zip(timeline, timeline[1:], strict=False):
        if cur.shot.transition != "crossfade":
            continue
        h = cur.shot.transition_frames
        nxt = timeline[cur.index + 1].shot if cur.index + 1 < len(timeline) else None
        outgoing = nxt.transition_frames if nxt is not None and nxt.transition == "crossfade" else 0
        src_prev, src_cur = edl.sources[prev.shot.clip], edl.sources[cur.shot.clip]
        tail = math.ceil(h * as_fraction(src_prev.fps) * Fraction(prev.shot.constant_speed) / out)
        head = math.ceil(h * as_fraction(src_cur.fps) * Fraction(cur.shot.constant_speed) / out)
        if h < 1 or 2 * h > prev.length or h + outgoing > cur.length:
            problems.append((cur.index, f"shot {cur.shot.id}: too short for its {h}-frame crossfade"))
        elif prev.shot.out + tail > src_prev.frames or cur.shot.in_ - head < 0:
            problems.append((cur.index, f"shot {cur.shot.id}: not enough footage outside the shots for the crossfade"))
    return problems


def plan_pieces(edl: EDL) -> list[Piece]:
    timeline = edl.timeline()
    pieces: list[Piece] = []
    for i, entry in enumerate(timeline):
        shot = entry.shot
        h_in = shot.transition_frames if i > 0 and shot.transition == "crossfade" else 0
        nxt = timeline[i + 1].shot if i + 1 < len(timeline) else None
        h_out = nxt.transition_frames if nxt is not None and nxt.transition == "crossfade" else 0
        body = entry.length - h_in - h_out
        if body > 0:
            pieces.append(Piece("body", entry.start + h_in, body, [Span(shot, h_in, body)]))
        if h_out:
            pieces.append(
                Piece(
                    "crossfade",
                    entry.start + entry.length - h_out,
                    2 * h_out,
                    [Span(shot, entry.length - h_out, 2 * h_out), Span(nxt, -h_out, 2 * h_out)],
                )
            )
    total = sum(p.count for p in pieces)
    if pieces:
        first, last = pieces[0], pieces[-1]
        first.fade_in = min(edl.music.fade_in_frames, first.count)
        last.fade_out = min(edl.music.fade_out_frames, last.count)
        if last.fade_out < edl.music.fade_out_frames:
            log.info(
                "picture fade-out clamped to the last piece (%d of %d frames)", last.fade_out, edl.music.fade_out_frames
            )
    assert total == edl.total_frames()
    return pieces


def _span_input(edl: EDL, span: Span) -> tuple[list[str], str]:
    source = edl.sources[span.shot.clip]
    speed = span.shot.constant_speed
    ratio = as_fraction(source.fps) * Fraction(speed).limit_denominator(1000) / as_fraction(edl.output.fps)
    first = max(0, math.floor(span.shot.in_ + span.rel_start * ratio))
    path = source.stabilized_path or source.path
    num, den = edl.output.fps
    w, h = edl.output.width, edl.output.height
    chain = [f"setpts=(PTS-STARTPTS)/{float(speed)!r}", f"fps={num}/{den}"]
    chain += to_rec709(_transfer(path), source.lut)
    chain += [
        f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bicubic",
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
        "format=yuv420p",
    ]
    return ["-ss", f"{seek_seconds(first, source.fps):.6f}", "-i", path], ",".join(chain)


_TRANSFERS: dict[str, str | None] = {}


def _transfer(path: str) -> str | None:
    if path not in _TRANSFERS:
        stream = ffprobe_json(path, "-select_streams", "v:0", "-show_entries", "stream=color_transfer")["streams"]
        _TRANSFERS[path] = stream[0].get("color_transfer") if stream else None
    return _TRANSFERS[path]


def _fades(piece: Piece, edl: EDL) -> list[str]:
    fps = as_fraction(edl.output.fps)
    out = []
    if piece.fade_in:
        out.append(f"fade=t=in:st=0:d={float(piece.fade_in / fps):.6f}")
    if piece.fade_out:
        start = (piece.count - piece.fade_out) / fps
        out.append(f"fade=t=out:st={float(start):.6f}:d={float(piece.fade_out / fps):.6f}")
    return out


def _content_id(path: str | None) -> str | None:
    return fingerprint(path) if path and Path(path).exists() else path


def piece_key(edl: EDL, piece: Piece, encoder: Encoder) -> str:
    spans = []
    for span in piece.spans:
        source = edl.sources[span.shot.clip]
        spans.append(
            {
                "fp": source.fingerprint,
                "stab": _content_id(source.stabilized_path),
                "lut": _content_id(source.lut),
                "fps": source.fps,
                "in": span.shot.in_,
                "out": span.shot.out,
                "speed": span.shot.speed,
                "rel": span.rel_start,
                "count": span.count,
            }
        )
    payload = {
        "v": RENDER_VERSION,
        "kind": piece.kind,
        "count": piece.count,
        "spans": spans,
        "fade": [piece.fade_in, piece.fade_out],
        "out": edl.output.model_dump(mode="json"),
        "enc": list(encoder.args),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]


def render_piece(edl: EDL, piece: Piece, encoder: Encoder, out: Path) -> Path:
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part.mp4")
    inputs, chains = [], []
    for span in piece.spans:
        args, chain = _span_input(edl, span)
        inputs += args
        chains.append(chain)
    fades = _fades(piece, edl)
    tail = [*fades, "tpad=stop_mode=clone:stop=1"]
    if piece.kind == "crossfade":
        duration = float(piece.count / as_fraction(edl.output.fps))
        graph = (
            f"[0:v]{chains[0]}[a];[1:v]{chains[1]}[b];"
            f"[a][b]xfade=transition=fade:duration={duration:.6f}:offset=0,{','.join(tail)}[v]"
        )
    else:
        graph = f"[0:v]{chains[0]},{','.join(tail)}[v]"
    run_ffmpeg(
        [
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-an",
            "-frames:v",
            str(piece.count),
            *encoder.args,
            *REC709_TAGS,
            "-video_track_timescale",
            str(mp4_timescale(edl.output.fps)),
            str(tmp),
        ]
    )
    produced = count_video_packets(tmp)
    if produced != piece.count:
        raise RuntimeError(f"piece {out.name} has {produced} frames, expected {piece.count}")
    os.replace(tmp, out)
    return out


def render(
    edl: EDL,
    out_path: Path,
    encoder: Encoder,
    cache_dir: Path,
    *,
    audio_bitrate: str = "320k",
    progress=print,
    parallel: int = 2,
) -> Path:
    problems = render_problems(edl)
    if problems:
        raise ValueError("; ".join(message for _, message in problems))
    for shot in edl.shots:
        source = edl.sources[shot.clip]
        if not Path(source.stabilized_path or source.path).exists():
            raise FileNotFoundError(f"source for {shot.clip} not found: {source.path} (fix its path in the EDL)")
    pieces = plan_pieces(edl)
    paths = [cache_dir / f"{piece_key(edl, p, encoder)}.mp4" for p in pieces]
    fresh = sum(1 for p in paths if not p.exists())
    progress(f"  {len(pieces)} pieces, {fresh} to encode with {encoder.name}")

    def work(item: tuple[Piece, Path]) -> Path:
        return render_piece(edl, item[0], encoder, item[1])

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for done, _ in enumerate(pool.map(work, zip(pieces, paths, strict=True)), start=1):
            if done % 5 == 0 or done == len(pieces):
                progress(f"  encoded {done}/{len(pieces)}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    listing = cache_dir / f"concat-{os.getpid()}.txt"
    listing.write_text("".join("file '" + p.as_posix().replace("'", "'\\''") + "'\n" for p in paths))
    fps = as_fraction(edl.output.fps)
    total = edl.total_frames()
    seconds = float(total / fps)
    start = float(edl.music.start_frame / fps)
    fade_in = float(edl.music.fade_in_frames / fps)
    fade_out = float(edl.music.fade_out_frames / fps)
    audio = ["apad"]
    if fade_in:
        audio.append(f"afade=t=in:st=0:d={fade_in:.6f}")
    if fade_out:
        audio.append(f"afade=t=out:st={max(0.0, seconds - fade_out):.6f}:d={fade_out:.6f}")
    tmp = out_path.with_name(out_path.stem + ".part" + out_path.suffix)
    try:
        run_ffmpeg(
            [
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-ss",
                f"{start:.6f}",
                "-i",
                edl.music.path,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-af",
                ",".join(audio),
                "-c:a",
                "aac",
                "-b:a",
                audio_bitrate,
                "-t",
                f"{seconds:.6f}",
                "-video_track_timescale",
                str(mp4_timescale(edl.output.fps)),
                "-movflags",
                "+faststart",
                str(tmp),
            ]
        )
    finally:
        listing.unlink(missing_ok=True)
    produced = count_video_packets(tmp)
    if produced != total:
        raise RuntimeError(f"final render has {produced} frames, expected {total}")
    os.replace(tmp, out_path)
    return out_path
