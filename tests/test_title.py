"""The intro title is drawn over the opening footage, across a cut, and leaves every later frame alone."""

import numpy as np

from goodfeeling.media.encoders import choose_encoder
from goodfeeling.media.ffmpeg import count_video_packets
from goodfeeling.media.frames import read_frames
from goodfeeling.media.probe import probe
from goodfeeling.pipeline.render import render
from goodfeeling.schemas.edl import EDL, MusicRef, Output, Shot, Source, Title
from goodfeeling.settings import RenderSettings


def test_title_overlays_the_opening_only(media, tmp_path):
    clip = probe(sorted(media["footage"].glob("*.MP4"))[0])
    source = Source(path=clip.path, fingerprint=clip.fingerprint, fps=clip.fps, frames=clip.frames)
    shots = [Shot(id="s01", clip="c01", in_=0, out=60), Shot(id="s02", clip="c01", in_=200, out=260)]
    music = MusicRef(path=str(media["music"]), fingerprint="m", fade_in_frames=0, fade_out_frames=0)
    plain = EDL(output=Output(width=640, height=360, fps=clip.fps), music=music, sources={"c01": source}, shots=shots)
    lines = ["Sometimes I get a good feeling", "Thornton Beach", "Nov 25, 2026"]
    title = Title(lines=lines, fade_in_frames=10, hold_frames=60, fade_out_frames=10)
    titled = plain.model_copy(update={"title": title})
    encoder = choose_encoder(RenderSettings(encoder="libx264"), 360)
    a = render(plain, tmp_path / "plain.mp4", encoder, tmp_path / "cache", progress=lambda _: None)
    b = render(titled, tmp_path / "titled.mp4", encoder, tmp_path / "cache", progress=lambda _: None)
    assert count_video_packets(b) == count_video_packets(a) == 120
    fa, fb = (read_frames(p, 0, 120, clip.fps, 640, 360).astype(int) for p in (a, b))
    centre = (slice(140, 220), slice(120, 520))
    assert np.abs(fa[0] - fb[0]).mean() < 1.0
    for i in (30, 65):
        assert np.abs(fb[i][centre] - fa[i][centre]).mean() > 8.0
    assert np.abs(fa[100] - fb[100]).mean() < 1.5
