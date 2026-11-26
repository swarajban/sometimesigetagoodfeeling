from __future__ import annotations

from pydantic import BaseModel, Field


class SegmentSignals(BaseModel):
    motion: float = 0.0
    motion_peak: float = 0.0
    coherence: float = 0.0
    rotation: float = 0.0
    sharpness: float = 0.0
    exposure: float = 0.0
    clipped: float = 0.0
    crash: bool = False
    blur: bool = False
    scene_cut_inside: bool = False


class Penalties(BaseModel):
    blur: bool = False
    crash: bool = False
    horizon_chaos: bool = False
    props_in_view: bool = False


class Score(BaseModel):
    score: float
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    reason: str = ""
    penalties: Penalties = Field(default_factory=Penalties)
    model: str = ""


class Segment(BaseModel):
    id: str
    clip: str
    flight: int
    start: int
    end: int
    kind: str = "flight"
    signals: SegmentSignals = Field(default_factory=SegmentSignals)
    phash: str = "0"
    local_score: float = 0.0
    local_parts: dict[str, float] = Field(default_factory=dict)
    claude: Score | None = None
    final_score: float = 0.0

    @property
    def frames(self) -> int:
        return self.end - self.start
