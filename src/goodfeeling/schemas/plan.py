from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SectionPacing(BaseModel):
    section: int
    label: str = ""
    beats_per_cut: int


class PlannedShot(BaseModel):
    segment_id: str
    beats: int
    start_offset_s: float = 0.0
    speed: float = 1.0
    transition: Literal["cut", "crossfade"] = "cut"
    role: str = ""
    reason: str = ""


class EditPlan(BaseModel):
    """What the director returns: an ordered list of shots measured in beats."""

    title: str = ""
    concept: str = ""
    pacing: list[SectionPacing] = Field(default_factory=list)
    shots: list[PlannedShot]
    order_note: str = ""
    closing_note: str = ""
    source: str = "claude"
