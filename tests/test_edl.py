import json

import pytest
from pydantic import ValidationError

from goodfeeling.schemas.edl import EDL
from goodfeeling.timebase import format_timecode, parse_timecode, seconds_to_frames


def sample() -> dict:
    return {
        "schema": 1,
        "output": {"width": 1920, "height": 1080, "fps": [60000, 1001], "color": "rec709"},
        "music": {"path": "/m.mp3", "fingerprint": "f", "start_frame": 0, "fade_in_frames": 30, "fade_out_frames": 90},
        "sources": {
            "c03": {
                "path": "/a.mp4",
                "fingerprint": "x",
                "fps": [60000, 1001],
                "frames": 14400,
                "stabilized_path": None,
                "lut": None,
            }
        },
        "shots": [
            {
                "id": "s01",
                "clip": "c03",
                "in": 5120,
                "out": 5480,
                "speed": [[0, 1.0]],
                "transition": "cut",
                "score": 8.4,
                "tags": ["gap"],
                "description": "d",
                "reason": "r",
                "starred": False,
            },
            {"id": "s02", "clip": "c03", "in": "01:40.000", "out": "01:42.500", "transition": "crossfade"},
        ],
    }


def test_round_trip_and_timecodes(tmp_path):
    edl = EDL.model_validate(sample())
    second = edl.shots[1]
    assert second.in_ == seconds_to_frames(100, (60000, 1001)) == 5994
    assert second.out == 6144
    assert second.transition_frames == 8
    path = edl.save(tmp_path / "edl.json")
    raw = json.loads(path.read_text())
    assert raw["shots"][1]["in"] == 5994 and "in_" not in raw["shots"][1]
    assert EDL.load(path) == edl
    timeline = edl.timeline()
    assert [e.start for e in timeline] == [0, 360]
    assert edl.total_frames() == 360 + 150


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["shots"][0].update(clip="nope"),
        lambda d: d["shots"][0].update(out=5000),
        lambda d: d["shots"][0].update(bogus=1),
        lambda d: d.update(schema=2),
        lambda d: d["shots"][1].update(id="s01"),
    ],
)
def test_rejects_bad_edls(mutate):
    data = sample()
    mutate(data)
    with pytest.raises(ValidationError):
        EDL.model_validate(data)


def test_timecode_helpers():
    assert parse_timecode("01:02.5") == 62.5
    assert parse_timecode("1:00:00.250") == 3600.25
    assert format_timecode(5994, (60000, 1001)) == "01:40.000"
    with pytest.raises(ValueError):
        parse_timecode("abc")
