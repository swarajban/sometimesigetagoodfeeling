from __future__ import annotations

from pydantic import BaseModel

from goodfeeling.timebase import Rate, frames_to_seconds


class ClipInfo(BaseModel):
    path: str
    size: int
    fingerprint: str
    codec: str
    profile: str | None = None
    width: int
    height: int
    fps: Rate
    frames: int
    duration_s: float
    pix_fmt: str
    bit_depth: int
    color_transfer: str | None = None
    color_primaries: str | None = None
    color_space: str | None = None
    color_range: str | None = None
    rotation: int = 0
    creation_time: str | None = None
    has_audio: bool = False
    variable_frame_rate: bool = False

    @property
    def seconds(self) -> float:
        return float(frames_to_seconds(self.frames, self.fps))

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer in {"arib-std-b67", "smpte2084"}


class FlightInfo(BaseModel):
    clip_id: str
    path: str
    flight: int
    order_in_flight: int
    continues_previous: bool = False
    name_time: str | None = None
    sequence: int | None = None
    stabilized_path: str | None = None
