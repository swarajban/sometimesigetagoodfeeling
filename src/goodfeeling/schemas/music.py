from __future__ import annotations

from pydantic import BaseModel, Field

from goodfeeling.timebase import Rate, seconds_to_frames


class MusicSection(BaseModel):
    label: str
    start_beat: int
    end_beat: int
    start_s: float
    end_s: float
    energy: float


class MusicAnalysis(BaseModel):
    """Hand-editable analysis of the music track (`music.json`)."""

    version: int = 1
    path: str
    fingerprint: str
    duration_s: float
    tempo: float
    beats: list[float]
    grid: str = "tracked"
    downbeat_phase: int = 0
    downbeat_phase_is_estimate: bool = True
    sections: list[MusicSection] = Field(default_factory=list)
    sections_are_estimate: bool = True
    beat_energy: list[float] = Field(default_factory=list)
    drop_beat: int | None = None
    notes: list[str] = Field(default_factory=list)

    def beat_frames(self, fps: Rate, start_frame: int = 0) -> list[int]:
        """Absolute output frame of each beat, measured from the edit's music start."""
        return [seconds_to_frames(t, fps) - start_frame for t in self.beats]

    def downbeats(self) -> list[int]:
        return [i for i in range(len(self.beats)) if i % 4 == self.downbeat_phase % 4]

    def section_at(self, beat: int) -> MusicSection | None:
        for section in self.sections:
            if section.start_beat <= beat < section.end_beat:
                return section
        return self.sections[-1] if self.sections else None
