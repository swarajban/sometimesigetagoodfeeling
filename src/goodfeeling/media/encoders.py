"""Pick the final encoder: VideoToolbox on macOS when it actually works, libx264 elsewhere."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

from goodfeeling.media.ffmpeg import ffmpeg_bin
from goodfeeling.settings import RenderSettings

KNOWN = ("hevc_videotoolbox", "h264_videotoolbox", "libx264", "libx265")


@dataclass(frozen=True)
class Encoder:
    name: str
    args: tuple[str, ...] = field(default_factory=tuple)

    @property
    def hardware(self) -> bool:
        return self.name.endswith("_videotoolbox")


@lru_cache(maxsize=1)
def listed_encoders() -> frozenset[str]:
    proc = subprocess.run([ffmpeg_bin(), "-hide_banner", "-encoders"], capture_output=True, text=True)
    names = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return frozenset(names)


@lru_cache(maxsize=8)
def encoder_works(name: str) -> bool:
    """Listed is not enough for VideoToolbox; try a tiny encode."""
    if name not in listed_encoders():
        return False
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=30:duration=0.2",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        name,
        "-f",
        "null",
        "-",
    ]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def choose_encoder(settings: RenderSettings, height: int) -> Encoder:
    wanted = settings.encoder
    if wanted == "auto":
        order = ("hevc_videotoolbox", "h264_videotoolbox", "libx264")
        wanted = next((name for name in order if encoder_works(name)), "libx264")
    elif not encoder_works(wanted):
        raise RuntimeError(f"encoder {wanted} is not usable with this ffmpeg (run 'goodfeeling doctor')")
    bitrate = settings.bitrate_4k if height > 1080 else settings.bitrate_1080
    if wanted == "hevc_videotoolbox":
        rate = ("-q:v", str(settings.videotoolbox_quality)) if settings.videotoolbox_quality else ("-b:v", bitrate)
        return Encoder(wanted, ("-c:v", wanted, "-profile:v", "main", "-tag:v", "hvc1", *rate, "-pix_fmt", "yuv420p"))
    if wanted == "h264_videotoolbox":
        rate = ("-q:v", str(settings.videotoolbox_quality)) if settings.videotoolbox_quality else ("-b:v", bitrate)
        return Encoder(wanted, ("-c:v", wanted, "-profile:v", "high", *rate, "-pix_fmt", "yuv420p"))
    if wanted == "libx265":
        return Encoder(
            wanted,
            (
                "-c:v",
                wanted,
                "-preset",
                settings.x264_preset,
                "-crf",
                str(settings.x264_crf + 4),
                "-tag:v",
                "hvc1",
                "-pix_fmt",
                "yuv420p",
                "-x265-params",
                "log-level=error",
            ),
        )
    return Encoder(
        "libx264",
        (
            "-c:v",
            "libx264",
            "-preset",
            settings.x264_preset,
            "-crf",
            str(settings.x264_crf),
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
        ),
    )
