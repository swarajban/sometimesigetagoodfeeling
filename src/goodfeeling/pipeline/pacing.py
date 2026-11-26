"""Pacing in seconds: how long shots should be, converted to beats at the track's tempo.

FPV moves need time to read (approach, pass, exit), so pacing targets a shot length in seconds rather than a
fixed beat count, and a floor keeps sustained quick cutting out; only short accent bursts may go below it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PACE_NAMES = {"slower": 0.75, "normal": 1.0, "faster": 1.33}
BURST_MIN, BURST_MAX = 2, 4
BURST_EVERY_S = 30.0

TARGETS = {
    ("landscape", "fast"): (3.0, 2.0, 5.0, 1.5),
    ("landscape", "cinematic"): (5.5, 3.5, 8.0, 1.5),
    ("reel", "fast"): (2.5, 1.5, 4.0, 1.2),
    ("reel", "cinematic"): (3.5, 2.0, 5.0, 1.2),
}


def parse_pace(value: str | float) -> float:
    """`slower`, `normal`, `faster`, or a multiplier on how often it cuts (1.2 = 20% more cuts)."""
    if isinstance(value, str) and value.strip().lower() in PACE_NAMES:
        return PACE_NAMES[value.strip().lower()]
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"pace must be slower, normal, faster, or a number like 1.2, not {value!r}") from exc
    if not 0.4 <= number <= 2.5:
        raise ValueError("pace must be between 0.4 and 2.5")
    return number


@dataclass(frozen=True)
class Pacing:
    target_s: float
    min_s: float
    max_s: float
    floor_s: float

    def for_energy(self, energy: float) -> float:
        """Shot length for a section: longer holds when it's quiet, shorter when it's loud (energy 0-1)."""
        return min(self.max_s, max(self.min_s, self.target_s * (1.25 - 0.5 * energy)))


def pacing_for(edit_format: str, style: str, pace: float = 1.0) -> Pacing:
    target, low, high, floor = TARGETS.get((edit_format, style), TARGETS[("landscape", "fast")])
    return Pacing(target / pace, low / pace, high / pace, floor / pace)


def beats_for(seconds: float, seconds_per_beat: float) -> int:
    return max(1, round(seconds / seconds_per_beat))


def floor_beats(pacing: Pacing, seconds_per_beat: float) -> int:
    return max(1, math.ceil(pacing.floor_s / seconds_per_beat - 1e-6))


def pacing_problems(lengths_s: list[float], floor_s: float) -> list[tuple[int, str]]:
    """Shots under the floor must come as accent bursts of 2-4 quick cuts, at most one burst per 30 s."""
    problems: list[tuple[int, str]] = []
    runs: list[tuple[int, int]] = []
    start = None
    for i, length in enumerate([*lengths_s, math.inf]):
        short = length < floor_s - 1e-3
        if short and start is None:
            start = i
        elif not short and start is not None:
            runs.append((start, i))
            start = None
    allowed = max(1, int(sum(lengths_s) // BURST_EVERY_S))
    bursts = 0
    for a, b in runs:
        count = b - a
        if count < BURST_MIN:
            problems.append(
                (
                    a,
                    f"{lengths_s[a]:.2f} s is under the {floor_s:.2f} s floor; only a burst of "
                    f"{BURST_MIN}-{BURST_MAX} quick cuts may go shorter",
                )
            )
        elif count > BURST_MAX:
            problems.append(
                (a, f"{count} quick cuts in a row under the {floor_s:.2f} s floor; a burst is at most {BURST_MAX}")
            )
        else:
            bursts += 1
            if bursts > allowed:
                problems.append((a, f"too many quick-cut bursts: at most {allowed} for this length"))
    return problems
