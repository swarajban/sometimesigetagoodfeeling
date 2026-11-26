"""Cuts land on beat frames, with no cumulative drift, at NTSC rates."""

from fractions import Fraction

import pytest

from goodfeeling.pipeline.direct import Context, build_edl, select
from goodfeeling.pipeline.stretch import StretchError, choose_window
from goodfeeling.schemas.clip import ClipInfo, FlightInfo
from goodfeeling.schemas.moment import Moment
from goodfeeling.schemas.music import MusicAnalysis, MusicSection
from goodfeeling.schemas.plan import EditPlan, PlannedShot
from goodfeeling.schemas.segment import Segment
from goodfeeling.timebase import seconds_to_frames


def make_context(out_fps, src_fps, tempo=125.0, beats=400, offset=0.37):
    spb = 60.0 / tempo
    music = MusicAnalysis(
        path="/m",
        fingerprint="m",
        duration_s=offset + beats * spb + 1,
        tempo=tempo,
        beats=[offset + i * spb for i in range(beats)],
        grid="regular",
        sections=[
            MusicSection(
                label="main", start_beat=0, end_beat=beats, start_s=offset, end_s=offset + beats * spb, energy=1.0
            )
        ],
    )
    clips, flights, segments = {}, {}, {}
    for n in range(1, 81):
        cid = f"c{n:02d}"
        frames = 20 * src_fps[0] // src_fps[1]
        clips[cid] = ClipInfo(
            path=f"/f/{cid}.mp4",
            size=1,
            fingerprint=cid,
            codec="h264",
            width=1920,
            height=1080,
            fps=src_fps,
            frames=frames,
            duration_s=20.0,
            pix_fmt="yuv420p",
            bit_depth=8,
        )
        flights[cid] = FlightInfo(clip_id=cid, path=f"/f/{cid}.mp4", flight=1, order_in_flight=n)
        segments[f"{cid}s01"] = Segment(
            id=f"{cid}s01",
            clip=cid,
            flight=1,
            start=0,
            end=frames,
            final_score=9,
            phash=f"{n * 0x1111111111:016x}"[-16:],
        )
    window, _ = choose_window(music, out_fps, 300 * spb)
    return Context(
        segments=segments,
        clips=clips,
        flights=flights,
        music=music,
        window=window,
        out_fps=out_fps,
        width=1920,
        height=1080,
        style="fast",
        target_s=300 * spb,
    )


@pytest.mark.parametrize(
    "out_fps,src_fps", [((60000, 1001), (60000, 1001)), ((30000, 1001), (60000, 1001)), ((24000, 1001), (30000, 1001))]
)
def test_cuts_on_beats_without_drift(out_fps, src_fps):
    ctx = make_context(out_fps, src_fps)
    pattern = [6, 6, 6, 7]
    shots, total = [], 0
    for i in range(80):
        beats = pattern[i % 4]
        if total + beats > ctx.window.beats:
            beats = ctx.window.beats - total
        if beats <= 0:
            break
        shots.append(PlannedShot(segment_id=f"c{i + 1:02d}s01", beats=beats, reason="r"))
        total += beats
    edl, violations = build_edl(ctx, EditPlan(shots=shots))
    assert violations == [] and edl is not None
    fps = Fraction(*out_fps)
    beat_index = 0
    worst = Fraction(0)
    for entry, planned in zip(edl.timeline(), shots, strict=True):
        beat_time = Fraction(ctx.music.beats[ctx.window.start_beat + beat_index]).limit_denominator(10**9)
        start_time = Fraction(entry.start + ctx.window.start_frame) / fps
        expected = seconds_to_frames(ctx.music.beats[ctx.window.start_beat + beat_index], out_fps)
        assert entry.start + ctx.window.start_frame == expected
        worst = max(worst, abs(start_time - beat_time))
        beat_index += planned.beats
    assert worst <= Fraction(1, 2) / fps
    assert beat_index == ctx.window.beats

    naive, drift = 0, 0
    per_beat = round(60 / ctx.music.tempo * float(fps))
    for planned in shots:
        naive += planned.beats * per_beat
    drift = abs(naive - edl.total_frames())
    assert drift >= 3, "the naive per-shot rounding this guards against should visibly drift"


def test_slower_source_never_accumulates():
    ctx = make_context((60000, 1001), (30000, 1001))
    shots = [PlannedShot(segment_id=f"c{i + 1:02d}s01", beats=[6, 6, 6, 7][i % 4], reason="r") for i in range(48)]
    edl, violations = build_edl(ctx, EditPlan(shots=shots))
    assert violations == [] and edl is not None
    beat = 0
    for entry, planned in zip(edl.timeline(), shots, strict=True):
        assert abs(entry.start - ctx.window.frame_of(beat)) <= 1
        beat += planned.beats
    assert edl.total_frames() - ctx.window.frame_of(beat) in (-1, 0, 1)


def test_pinned_stretch_snaps_to_downbeats_and_sets_length():
    ctx = make_context((30000, 1001), (30000, 1001), tempo=120.0, beats=400, offset=0.25)
    music = ctx.music
    window, notes = choose_window(music, (30000, 1001), 60, start_s=21.0)
    assert music.beats[window.start_beat] == 20.25 and window.start_beat % 4 == 0
    assert window.beats == 120 and notes
    window, _ = choose_window(music, (30000, 1001), 60, start_s=20.25, end_s=50.3)
    assert window.beats == 60
    for bad in (
        {"start_s": -1},
        {"start_s": 999},
        {"start_s": 20.0, "end_s": 21.0},
        {"start_s": 30, "end_s": 20},
        {"end_s": 30},
    ):
        with pytest.raises(StretchError):
            choose_window(music, (30000, 1001), 60, **bad)


def test_stretch_without_a_drop_peaks_on_its_loudest_section():
    from goodfeeling.schemas.music import MusicSection

    ctx = make_context((30000, 1001), (30000, 1001), tempo=120.0, beats=200, offset=0.0)
    ctx.music.sections = [
        MusicSection(label="main", start_beat=0, end_beat=48, start_s=0, end_s=24, energy=0.3),
        MusicSection(label="main", start_beat=48, end_beat=96, start_s=24, end_s=48, energy=0.9),
        MusicSection(label="drop", start_beat=96, end_beat=200, start_s=48, end_s=100, energy=1.0),
    ]
    ctx.window, _ = choose_window(ctx.music, (30000, 1001), 40, start_s=0.0)
    assert ctx.peak() == (48, 80)
    plan = select(ctx)
    edl, violations = build_edl(ctx, plan)
    assert violations == []


def test_pacing_floor_allows_only_short_bursts():
    from goodfeeling.pipeline.pacing import pacing_problems

    assert pacing_problems([3.0, 1.0, 3.0], 1.5)
    assert not pacing_problems([3.0, 1.0, 1.0, 0.8, 3.0, 2.5], 1.5)
    assert pacing_problems([3.0] + [1.0] * 5 + [3.0], 1.5)
    assert pacing_problems([1.0, 1.0, 3.0, 1.0, 1.0, 3.0], 1.5), "one burst per 30 s"
    ctx = make_context((30000, 1001), (30000, 1001))
    edl, violations = build_edl(
        ctx, EditPlan(shots=[PlannedShot(segment_id=f"c{i + 1:02d}s01", beats=2, reason="r") for i in range(150)])
    )
    assert any(v.code == "pace" for v in violations), "sustained 2-beat cutting is rejected at normal pace"
    plan = select(ctx)
    assert min(p.beats for p in plan.shots) * 60 / ctx.music.tempo >= ctx.rules.floor_s


def test_long_hold_extends_into_clean_neighbouring_footage_only():
    from goodfeeling.pipeline.direct import extents

    ctx = make_context((30000, 1001), (30000, 1001), tempo=120.0)
    a = Segment(id="c01s01", clip="c01", flight=1, start=0, end=60, final_score=9, phash="0" * 16)
    b = Segment(id="c01s02", clip="c01", flight=1, start=60, end=300, final_score=5, phash="f" * 16)
    ctx.segments = {"c01s01": a, "c01s02": b, **{k: v for k, v in ctx.segments.items() if v.clip != "c01"}}
    ctx.extent = extents(list(ctx.segments.values()))
    rest = [PlannedShot(segment_id=f"c{i:02d}s01", beats=6, reason="r") for i in range(2, 60)]
    plan = EditPlan(shots=[PlannedShot(segment_id="c01s01", beats=5, reason="r"), *rest])
    edl, violations = build_edl(ctx, plan)
    assert not [v for v in violations if v.code in ("too_short", "overlap")]
    assert edl.shots[0].in_ == 0 and edl.shots[0].out > a.end
    b.signals.crash = True
    ctx.extent = extents(list(ctx.segments.values()))
    _, violations = build_edl(ctx, plan)
    assert any(v.code == "too_short" for v in violations)


def test_moments_hold_in_full_and_filler_stays_short():

    ctx = make_context((30000, 1001), (30000, 1001), tempo=120.0)
    for seg in ctx.segments.values():
        seg.final_score = 6.0
    big = Moment(id="m01", clip="c05", flight=1, start=30, end=30 + 300, worth=9.5, hold="full", kind="gap")
    ctx.segments["m01"] = Segment(
        id="m01", clip="c05", flight=1, start=big.start, end=big.end, kind="moment", final_score=9.5, phash="1" * 16
    )
    del ctx.segments["c05s01"]
    ctx.moments = {"m01": big}
    plan = select(ctx)
    edl, violations = build_edl(ctx, plan)
    assert violations == []
    lengths = {p.segment_id: p.beats * 0.5 for p in plan.shots}
    assert 9.0 <= lengths["m01"] <= 10.6, "the moment plays its ~10 s span"
    assert all(v <= ctx.rules.filler_s + 0.25 for k, v in lengths.items() if k != "m01")
    padded = EditPlan(
        shots=[
            PlannedShot(segment_id="m01", beats=24, reason="r"),
            PlannedShot(segment_id="c01s01", beats=8, reason="r"),
        ]
    )
    _, violations = build_edl(ctx, padded)
    assert {v.code for v in violations} >= {"hold"}


@pytest.mark.parametrize("with_moment", [True, False])
def test_thin_footage_still_plans_a_shorter_valid_edit(with_moment):
    ctx = make_context((30000, 1001), (30000, 1001), tempo=120.0)
    full = ctx.window.beats
    ordinary = ctx.segments["c01s01"]
    ordinary.end, ordinary.final_score = 300, 6.0
    ctx.segments = {"c01s01": ordinary}
    if with_moment:
        ctx.moments = {"m01": Moment(id="m01", clip="c01", flight=1, start=300, end=480, worth=9.0, hold="full")}
        ctx.segments["m01"] = Segment(
            id="m01", clip="c01", flight=1, start=300, end=480, kind="moment", final_score=9.0, phash="f" * 16
        )
    plan = select(ctx)
    edl, violations = build_edl(ctx, plan)
    assert violations == [] and edl is not None
    assert ctx.window.beats < full and any("footage was limited" in note for note in ctx.notes)
    assert len({p.segment_id for p in plan.shots}) == len(ctx.segments), "every bit of footage gets used"
