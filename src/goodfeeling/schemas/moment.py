from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Moment(BaseModel):
    """A move worth showing: one or more adjacent strong segments of a clip, trimmed to where the move really
    starts and ends. Segments stay as they are; moments sit beside them."""

    id: str
    clip: str
    flight: int
    start: int
    end: int
    peak: int | None = None
    kind: str = "move"
    hold: Literal["full", "short"] = "full"
    worth: float
    description: str = ""
    reason: str = ""
    segment_ids: list[str] = Field(default_factory=list)
    source: str = "local"

    @property
    def frames(self) -> int:
        return self.end - self.start
