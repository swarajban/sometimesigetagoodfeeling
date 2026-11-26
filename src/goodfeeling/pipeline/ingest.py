"""Ingest: probe every clip, group files into flights, resolve stabilized siblings, make proxies."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from goodfeeling.media.fingerprint import fingerprint
from goodfeeling.media.probe import probe
from goodfeeling.media.proxy import PROXY_VERSION, make_proxy
from goodfeeling.schemas.clip import ClipInfo, FlightInfo
from goodfeeling.store.project import Project

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov"}
DJI_NEW = re.compile(r"^DJI_(\d{14})_(\d{4})(?:_[A-Z])?", re.IGNORECASE)
DJI_OLD = re.compile(r"^DJI_(\d{4})(?:_[A-Z])?$", re.IGNORECASE)
SPLIT_TOLERANCE_S = 3.0


def find_videos(folder: Path) -> list[Path]:
    found = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in VIDEO_EXTENSIONS or path.stem.lower().endswith("_stabilized"):
            continue
        found.append(path)
    return found


def parse_dji_name(name: str) -> tuple[datetime | None, int | None]:
    stem = Path(name).stem
    match = DJI_NEW.match(stem)
    if match:
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S"), int(match.group(2))
    match = DJI_OLD.match(stem)
    if match:
        return None, int(match.group(1))
    return None, None


def _creation(clip: ClipInfo) -> datetime | None:
    if not clip.creation_time:
        return None
    try:
        stamp = datetime.fromisoformat(clip.creation_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.astimezone(UTC).replace(tzinfo=None) if stamp.tzinfo else stamp


def group_flights(clips: list[ClipInfo], flight_gap_s: float) -> list[FlightInfo]:
    """Order files and split them into flights.

    DJI's filename (timestamp and sequence number) decides first; creation_time only breaks
    ties or fills in when the name carries no time, because the air unit's clock is unreliable.
    Consecutive files whose start lands within a few seconds of the previous file's end are a
    split recording (`continues_previous`); gaps longer than `flight_gap_s` start a new flight.
    """
    rows = []
    for clip in clips:
        name_time, seq = parse_dji_name(clip.path)
        start = name_time or _creation(clip)
        rows.append((clip, name_time, seq, start))
    rows.sort(key=lambda r: (r[3] or datetime.max, r[2] if r[2] is not None else 1 << 30, Path(r[0].path).name))
    flights: list[FlightInfo] = []
    flight, order = 0, 0
    previous = None
    for index, (clip, name_time, seq, start) in enumerate(rows):
        continues = False
        new_flight = previous is None
        if previous is not None:
            prev_clip, _, prev_seq, prev_start = previous
            if start is None or prev_start is None:
                new_flight = True
            else:
                gap = (start - prev_start).total_seconds() - prev_clip.duration_s
                sequential = seq is None or prev_seq is None or seq == prev_seq + 1
                continues = sequential and abs(gap) <= SPLIT_TOLERANCE_S
                new_flight = not continues and gap > flight_gap_s
        if new_flight:
            flight += 1
            order = 0
        flights.append(
            FlightInfo(
                clip_id=f"c{index + 1:02d}",
                path=clip.path,
                flight=flight,
                order_in_flight=order,
                continues_previous=continues and not new_flight,
                name_time=name_time.isoformat() if name_time else None,
                sequence=seq,
            )
        )
        order += 1
        previous = (clip, name_time, seq, start)
    return flights


def resolve_stabilized(clip: ClipInfo, stabilized_dir: str | None) -> str | None:
    """Gyroflow hook: use `<name>_stabilized.<ext>` only if frame count and rate match the original."""
    original = Path(clip.path)
    candidates = [original.with_name(f"{original.stem}_stabilized{ext}") for ext in (original.suffix, ".mp4", ".mov")]
    if stabilized_dir:
        candidates += [
            Path(stabilized_dir).expanduser() / f"{original.stem}_stabilized{ext}"
            for ext in (original.suffix, ".mp4", ".mov")
        ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        info = probe(candidate)
        if info.fps == clip.fps and abs(info.frames - clip.frames) <= 1:
            return str(candidate)
        log.warning(
            "ignoring %s: %d frames at %s/%s, original has %d at %s/%s",
            candidate,
            info.frames,
            *info.fps,
            clip.frames,
            *clip.fps,
        )
    return None


def ingest(
    project: Project, *, flight_gap_s: float, stabilized_dir: str | None = None, lut: str | None = None, progress=print
) -> list[tuple[FlightInfo, ClipInfo]]:
    paths = find_videos(project.footage_dir)
    if not paths:
        raise FileNotFoundError(f"no .mp4 or .mov files in {project.footage_dir}")
    cached = {
        row["path"]: row
        for row in project.db.execute(
            "SELECT path, fingerprint, info_json FROM clips WHERE project_id=?", (project.id,)
        )
    }
    clips = []
    for path in paths:
        row = cached.get(str(path.resolve()))
        info = ClipInfo.model_validate_json(row["info_json"]) if row else None
        if info is None or info.size != path.stat().st_size:
            info = probe(path)
        clips.append(info)
    flights = group_flights(clips, flight_gap_s)
    by_path = {clip.path: clip for clip in clips}
    project.db.execute("DELETE FROM clips WHERE project_id=?", (project.id,))
    result = []
    for flight in flights:
        clip = by_path[flight.path]
        flight.stabilized_path = resolve_stabilized(clip, stabilized_dir)
        lut_tag = f"-lut{fingerprint(lut)[-8:]}" if lut else ""
        proxy = project.path("proxies", f"{clip.fingerprint}-{PROXY_VERSION}{lut_tag}.mp4")
        if not proxy.exists():
            progress(
                f"  proxy {flight.clip_id} {Path(clip.path).name} ({clip.codec} {clip.bit_depth}-bit "
                f"{clip.width}x{clip.height} @ {clip.fps[0]}/{clip.fps[1]})"
            )
        make_proxy(clip, proxy, lut)
        project.db.execute(
            "INSERT INTO clips (project_id, clip_id, path, fingerprint, info_json, flight_json, proxy_path) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                project.id,
                flight.clip_id,
                clip.path,
                clip.fingerprint,
                clip.model_dump_json(),
                flight.model_dump_json(),
                str(proxy),
            ),
        )
        result.append((flight, clip))
    project.write_json("analysis/clips.json", [{"flight": f.model_dump(), "clip": c.model_dump()} for f, c in result])
    return result


def load_clips(project: Project) -> list[tuple[FlightInfo, ClipInfo, Path]]:
    rows = project.db.execute(
        "SELECT flight_json, info_json, proxy_path FROM clips WHERE project_id=? ORDER BY clip_id", (project.id,)
    ).fetchall()
    return [
        (
            FlightInfo.model_validate(json.loads(r["flight_json"])),
            ClipInfo.model_validate_json(r["info_json"]),
            Path(r["proxy_path"]),
        )
        for r in rows
    ]
