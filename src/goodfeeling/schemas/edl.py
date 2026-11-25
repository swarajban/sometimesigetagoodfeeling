"""The EDL: the self-contained, versioned description of an edit."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from goodfeeling.timebase import (
    Rate,
    normalize_rate,
    output_frames_for_source,
    parse_timecode,
    seconds_to_frames,
)

SCHEMA_VERSION = 1


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int
    height: int
    fps: Rate
    color: Literal["rec709"] = "rec709"

    @field_validator("fps", mode="before")
    @classmethod
    def _rate(cls, value: Any) -> Rate:
        return normalize_rate(tuple(value) if isinstance(value, list) else value)


class MusicRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    fingerprint: str
    start_frame: int = 0
    fade_in_frames: int = 30
    fade_out_frames: int = 90


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    fingerprint: str
    fps: Rate
    frames: int
    stabilized_path: str | None = None
    lut: str | None = None

    @field_validator("fps", mode="before")
    @classmethod
    def _rate(cls, value: Any) -> Rate:
        return normalize_rate(tuple(value) if isinstance(value, list) else value)


class Shot(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    clip: str
    in_: int = Field(alias="in")
    out: int
    speed: list[tuple[int, float]] = Field(default_factory=lambda: [(0, 1.0)])
    transition: Literal["cut", "crossfade"] = "cut"
    transition_frames: int = 8
    score: float | None = None
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    reason: str = ""
    starred: bool = False

    @property
    def constant_speed(self) -> float:
        return float(self.speed[0][1]) if self.speed else 1.0


class TimelineEntry(BaseModel):
    shot: Shot
    index: int
    start: int
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length


class EDL(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: int = Field(default=SCHEMA_VERSION, alias="schema")
    output: Output
    music: MusicRef
    sources: dict[str, Source]
    shots: list[Shot]

    @model_validator(mode="before")
    @classmethod
    def _normalize_timecodes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        sources = data.get("sources") or {}
        for shot in data.get("shots") or []:
            if not isinstance(shot, dict):
                continue
            source = sources.get(shot.get("clip"))
            for key in ("in", "out"):
                value = shot.get(key)
                if isinstance(value, str):
                    if source is None:
                        raise ValueError(f"shot {shot.get('id')}: unknown clip {shot.get('clip')!r}")
                    fps = (
                        source.fps
                        if isinstance(source, Source)
                        else normalize_rate(tuple(source["fps"]) if isinstance(source["fps"], list) else source["fps"])
                    )
                    shot[key] = seconds_to_frames(parse_timecode(value), fps)
        return data

    @model_validator(mode="after")
    def _check(self) -> EDL:
        if self.schema_ != SCHEMA_VERSION:
            raise ValueError(f"unsupported EDL schema {self.schema_}; this build reads schema {SCHEMA_VERSION}")
        ids = set()
        for shot in self.shots:
            if shot.id in ids:
                raise ValueError(f"duplicate shot id {shot.id}")
            ids.add(shot.id)
            if shot.clip not in self.sources:
                raise ValueError(f"shot {shot.id}: unknown clip {shot.clip!r}")
            if shot.out <= shot.in_:
                raise ValueError(f"shot {shot.id}: out ({shot.out}) must be after in ({shot.in_})")
        return self

    def timeline(self) -> list[TimelineEntry]:
        entries: list[TimelineEntry] = []
        position = 0
        for index, shot in enumerate(self.shots):
            source = self.sources[shot.clip]
            length = output_frames_for_source(shot.out - shot.in_, source.fps, self.output.fps, shot.constant_speed)
            entries.append(TimelineEntry(shot=shot, index=index, start=position, length=length))
            position += length
        return entries

    def total_frames(self) -> int:
        return sum(entry.length for entry in self.timeline())

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json", by_alias=True), indent=2) + "\n"

    @classmethod
    def load(cls, path: str | Path) -> EDL:
        return cls.model_validate(json.loads(Path(path).read_text()))

    def save(self, path: str | Path) -> Path:
        return write_atomic(Path(path), self.to_json())


def write_atomic(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
