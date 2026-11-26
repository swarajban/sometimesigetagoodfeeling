from __future__ import annotations

import json
import logging
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    pass


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise FFmpegError("ffmpeg not found on PATH (macOS: brew install ffmpeg)")
    return found


def ffprobe_bin() -> str:
    found = shutil.which("ffprobe")
    if not found:
        raise FFmpegError("ffprobe not found on PATH (macOS: brew install ffmpeg)")
    return found


def run_ffmpeg(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args]
    log.debug("ffmpeg %s", " ".join(cmd[1:]))
    proc = subprocess.run(cmd, capture_output=True, text=not capture)
    if proc.returncode != 0:
        err = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        raise FFmpegError(f"ffmpeg failed ({proc.returncode}): {err.strip()[-2000:]}\n{' '.join(cmd)}")
    return proc


def ffprobe_json(path: str | Path, *extra: str) -> dict:
    cmd = [ffprobe_bin(), "-v", "error", "-print_format", "json", *extra, str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def count_video_packets(path: str | Path) -> int:
    data = ffprobe_json(path, "-select_streams", "v:0", "-count_packets", "-show_entries", "stream=nb_read_packets")
    return int(data["streams"][0]["nb_read_packets"])


@lru_cache(maxsize=1)
def ffmpeg_version() -> str:
    proc = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True, text=True)
    return proc.stdout.splitlines()[0] if proc.stdout else "unknown"


@lru_cache(maxsize=1)
def available_filters() -> frozenset[str]:
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-filters"], capture_output=True, text=True)
    _, _, table = proc.stdout.partition("------")
    return frozenset(parts[1] for parts in map(str.split, table.splitlines()) if len(parts) >= 2)
