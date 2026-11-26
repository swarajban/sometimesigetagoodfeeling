"""Shot length rules. Length comes from the content (see moments); these are only the guard rails.

- a floor no shot goes under, except a short accent burst of 2-4 quick cuts;
- a cap on filler: only marked moments, or segments scoring at least LONG_SCORE, may hold longer;
- pace biases how readily the director holds and how much connective footage it keeps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PACE_NAMES = {"slower": 0.75, "normal": 1.0, "faster": 1.33}
BURST_MIN, BURST_MAX = 2, 4
BURST_EVERY_S = 30.0
LONG_SCORE = 8.0

RULES = {
    ("landscape", "fast"): (1.5, 2.5),
    ("landscape", "cinematic"): (1.5, 3.5),
    ("reel", "fast"): (1.2, 2.0),
    ("reel", "cinematic"): (1.2, 2.5),
}


def parse_pace(value: str | float) -> float:
    """`slower`, `normal`, `faster`, or a multiplier (above 1 keeps more short connecting shots)."""
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
class Rules:
    floor_s: float
    filler_s: float
    pace: float

    @property
    def filler_share(self) -> float:
        """Roughly how much of the runtime connective shots get; the rest goes to moments."""
        return min(0.6, max(0.1, 0.3 * self.pace))

    def describe_pace(self) -> str:
        if self.pace < 0.95:
            return "Pace is set slower: hold the big moments readily and keep connecting shots to a minimum."
        if self.pace > 1.05:
            return "Pace is set faster: still hold the big moments, but use more short connecting hits between them."
        return "Pace is normal."


def rules_for(edit_format: str, style: str, pace: float = 1.0) -> Rules:
    floor, filler = RULES.get((edit_format, style), RULES[("landscape", "fast")])
    return Rules(floor_s=floor, filler_s=filler, pace=pace)


def beats_at_most(seconds: float, seconds_per_beat: float) -> int:
    return max(1, math.floor(seconds / seconds_per_beat + 1e-6))


def floor_beats(rules: Rules, seconds_per_beat: float) -> int:
    return max(1, math.ceil(rules.floor_s / seconds_per_beat - 1e-6))


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
