"""Cuts land on beat frames, with no cumulative drift, at NTSC rates."""

from fractions import Fraction

import pytest

from goodfeeling.pipeline.direct import Context, build_edl, choose_window
from goodfeeling.schemas.clip import ClipInfo, FlightInfo
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
            final_score=5,
            phash=f"{n * 0x1111111111:016x}"[-16:],
        )
    window = choose_window(music, out_fps, 300 * spb)
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
    pattern = [3, 4, 5, 3]
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
