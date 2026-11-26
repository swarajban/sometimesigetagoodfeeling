"""Which stretch of the song an edit is cut to: the default pick, a reel's pick, or a hand-picked one."""

from __future__ import annotations

from dataclasses import dataclass

from goodfeeling.schemas.music import MusicAnalysis, MusicSection
from goodfeeling.timebase import Rate, parse_timecode, seconds_to_frames

MIN_STRETCH_S = 4.0


class StretchError(ValueError):
    pass


@dataclass
class EditWindow:
    start_beat: int
    beats: int
    start_frame: int
    frames: list[int]

    def frame_of(self, beat: int) -> int:
        return self.frames[beat]


def parse_time(text: str) -> float:
    """Seconds (`80.5`) or a timestamp (`1:20`, `01:20.500`, `0:01:20`)."""
    try:
        return float(parse_timecode(text)) if ":" in text else float(text)
    except ValueError as exc:
        raise StretchError(f"can't read {text!r} as seconds or MM:SS(.mmm)") from exc


def fmt(seconds: float) -> str:
    millis = round(seconds * 1000)
    minutes, rem = divmod(millis, 60_000)
    return f"{minutes:02d}:{rem // 1000:02d}.{rem % 1000:03d}"


def target_beats(music: MusicAnalysis, target_s: float) -> int:
    return max(4, int(round(target_s * music.tempo / 60 / 4)) * 4)


def usable_beats(music: MusicAnalysis) -> int:
    return len(music.beats) - 1


def _downbeats(music: MusicAnalysis) -> list[int]:
    return [i for i in (music.downbeats() or [0]) if i < usable_beats(music)]


def _nearest_downbeat(music: MusicAnalysis, seconds: float) -> int:
    return min(_downbeats(music), key=lambda i: abs(music.beats[i] - seconds))


def peak_section(music: MusicAnalysis) -> MusicSection | None:
    return max(music.sections, key=lambda s: s.energy) if music.sections else None


def _fit(music: MusicAnalysis, start: int, count: int) -> int:
    """Move a start earlier, on downbeats, until `count` beats fit before the song ends."""
    latest = usable_beats(music) - count
    if start <= latest:
        return start
    earlier = [i for i in _downbeats(music) if i <= max(0, latest)]
    return earlier[-1] if earlier else _downbeats(music)[0]


def default_start(music: MusicAnalysis, target_s: float) -> int:
    """From the first downbeat, unless that leaves the drop out; then centre the drop at about 60%."""
    start = _downbeats(music)[0]
    beats = music.beats
    if music.drop_beat is not None and beats[music.drop_beat] - beats[start] > 0.85 * target_s:
        wanted = max(0.0, min(beats[music.drop_beat] - 0.6 * target_s, beats[-1] - target_s))
        start = _nearest_downbeat(music, wanted)
    return start


def peak_start(music: MusicAnalysis, target_s: float) -> int:
    """For short edits: start on the highest-energy section, moved earlier only if the song would run out."""
    peak = peak_section(music)
    if peak is None:
        return _downbeats(music)[0]
    start = _nearest_downbeat(music, peak.start_s)
    return _fit(music, start, min(target_beats(music, target_s), usable_beats(music)))


def around_start(music: MusicAnalysis, section: MusicSection, target_s: float) -> int:
    wanted = max(0.0, section.start_s - 0.35 * target_s)
    start = _nearest_downbeat(music, wanted)
    return _fit(music, start, min(target_beats(music, target_s), usable_beats(music)))


def make_window(music: MusicAnalysis, out_fps: Rate, start: int, count: int) -> EditWindow:
    start_frame = seconds_to_frames(music.beats[start], out_fps)
    frames = [seconds_to_frames(music.beats[start + k], out_fps) - start_frame for k in range(count + 1)]
    return EditWindow(start_beat=start, beats=count, start_frame=start_frame, frames=frames)


def choose_window(
    music: MusicAnalysis,
    out_fps: Rate,
    target_s: float,
    *,
    start_s: float | None = None,
    end_s: float | None = None,
    prefer: str = "drop",
) -> tuple[EditWindow, list[str]]:
    """The edit's stretch, as whole beats from a downbeat. Returns the window and notes worth printing."""
    notes: list[str] = []
    usable = usable_beats(music)
    if end_s is not None and start_s is None:
        raise StretchError("--music-end needs --music-start")
    if start_s is None:
        start = peak_start(music, target_s) if prefer == "peak" else default_start(music, target_s)
        count = min(target_beats(music, target_s), usable - start)
        count -= count % 4 if count > 4 else 0
        return make_window(music, out_fps, start, count), notes
    duration = music.duration_s
    if not 0 <= start_s < duration:
        raise StretchError(f"--music-start {fmt(start_s)} is outside the track (0 to {fmt(duration)})")
    start = _nearest_downbeat(music, start_s)
    if abs(music.beats[start] - start_s) > 0.01:
        notes.append(f"--music-start {fmt(start_s)} snapped to the downbeat at {fmt(music.beats[start])}")
    if end_s is not None:
        if end_s > duration + 0.05:
            raise StretchError(f"--music-end {fmt(end_s)} is past the end of the track ({fmt(duration)})")
        if end_s <= start_s:
            raise StretchError("--music-end must be after --music-start")
        ends = [i for i in [*_downbeats(music), usable] if i > start]
        end = min(ends, key=lambda i: abs(music.beats[i] - end_s)) if ends else start
        count = end - start
        if abs(music.beats[min(end, usable)] - end_s) > 0.01:
            notes.append(f"--music-end {fmt(end_s)} snapped to {fmt(music.beats[end])}")
    else:
        wanted = target_beats(music, target_s)
        count = min(wanted, usable - start)
        if count > 4 and count < wanted:
            count -= count % 4
        if count < wanted:
            notes.append(
                f"the track ends {count * 60 / music.tempo:.1f} s after {fmt(music.beats[start])}, "
                f"so the edit is shorter than {target_s:.0f} s"
            )
    seconds = count * 60 / music.tempo
    if count < 4 or seconds < MIN_STRETCH_S:
        raise StretchError(
            f"the stretch from {fmt(music.beats[start])} is only {seconds:.1f} s "
            f"({count} beats); pick at least {MIN_STRETCH_S:.0f} s"
        )
    return make_window(music, out_fps, start, count), notes


def suggestions(music: MusicAnalysis, target_s: float) -> list[tuple[str, int, int]]:
    """(why, start beat, beats) for the default pick and a few alternatives."""
    count = min(target_beats(music, target_s), usable_beats(music))
    picks = [
        ("default for this length (keeps the drop inside)", default_start(music, target_s)),
        ("starts on the highest-energy section (what a reel picks)", peak_start(music, target_s)),
    ]
    loud = sorted(music.sections, key=lambda s: -s.energy)
    top = loud[0].energy if loud else 0
    for section in loud:
        if section.energy >= 0.75 * top:
            index = music.sections.index(section)
            picks.append(
                (
                    f"builds into section {index} ({section.label}, energy {section.energy:.2f})",
                    around_start(music, section, target_s),
                )
            )
    seen: list[int] = []
    result = []
    for why, start in picks:
        if any(abs(start - other) < 8 for other in seen):
            continue
        seen.append(start)
        result.append((why, start, min(count, usable_beats(music) - start)))
    return result[:4]
