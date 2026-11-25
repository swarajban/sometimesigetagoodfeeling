"""Full run on synthetic footage: outputs exist, cuts hit the planned frames, originals are untouched."""

import hashlib
import json
import os
import stat

import pytest
from typer.testing import CliRunner

from goodfeeling.cli import app
from goodfeeling.media.frames import read_frames
from goodfeeling.pipeline.render import plan_pieces
from goodfeeling.pipeline.run import RunOptions, run
from goodfeeling.schemas.edl import EDL
from goodfeeling.settings import Settings
from tests.conftest import read_barcode


def snapshot(folder):
    state = {}
    for path in sorted(folder.rglob("*")):
        info = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        state[str(path.relative_to(folder))] = (digest, info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode))
    return state


def check_cuts(final, edl):
    """Every body piece starts on exactly the source frame the EDL asks for."""
    scale = edl.output.height // 360
    for piece in plan_pieces(edl):
        if piece.kind != "body":
            continue
        span = piece.spans[0]
        skip = piece.fade_in
        frame = read_frames(final, piece.start + skip, 1, edl.output.fps, edl.output.width, edl.output.height)[0]
        assert read_barcode(frame, scale) == span.shot.in_ + span.rel_start + skip, f"piece at {piece.start}"


@pytest.fixture(scope="module")
def result(media, tmp_path_factory):
    data_root = tmp_path_factory.mktemp("data")
    settings = Settings()
    settings.render.x264_preset = "ultrafast"
    footage = media["footage"]
    before = snapshot(footage)
    os.chmod(footage, 0o555)
    try:
        out = run(
            RunOptions(footage=footage, music=media["music"], length=8, style="fast", data_root=data_root, no_ai=True),
            settings,
            progress=lambda _: None,
        )
    finally:
        os.chmod(footage, 0o755)
    assert snapshot(footage) == before
    return out, data_root


def test_run_outputs(result):
    out, _ = result
    for key in ("edl", "plan_md", "plan_json", "summary_md", "contact_sheet", "final"):
        assert out.outputs[key].exists(), key
    edl = EDL.load(out.outputs["edl"])
    assert edl.output.fps == (60000, 1001)
    assert not any(str(p).startswith(str(out.project.footage_dir)) for p in out.outputs.values())
    check_cuts(out.outputs["final"], edl)


def test_render_from_hand_edited_edl(result, tmp_path):
    out, data_root = result
    raw = json.loads(out.outputs["edl"].read_text())
    first = raw["shots"][0]
    length = first["out"] - first["in"]
    first["in"] = "00:01.000"
    first["out"] = 60 + length
    if len(raw["shots"]) > 1:
        raw["shots"][1]["transition"] = "crossfade"
    edited = tmp_path / "hand.json"
    edited.write_text(json.dumps(raw))
    target = tmp_path / "hand.mp4"
    cli = CliRunner().invoke(
        app, ["render", "--from-edl", str(edited), "--out", str(target), "--data-root", str(data_root)]
    )
    assert cli.exit_code == 0, cli.output
    edl = EDL.load(edited)
    assert edl.shots[0].in_ == 60
    check_cuts(target, edl)


def test_render_refuses_to_write_into_footage(result, media):
    out, data_root = result
    cli = CliRunner().invoke(
        app,
        [
            "render",
            "--from-edl",
            str(out.outputs["edl"]),
            "--out",
            str(media["footage"] / "x.mp4"),
            "--data-root",
            str(data_root),
        ],
    )
    assert cli.exit_code != 0
    assert not (media["footage"] / "x.mp4").exists()
