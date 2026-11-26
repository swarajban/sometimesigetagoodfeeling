"""Command line: `goodfeeling run | score | render --from-edl | doctor`."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from goodfeeling import DISPLAY_NAME

app = typer.Typer(
    help=f"{DISPLAY_NAME}: beat-synced highlight edits from raw FPV footage.",
    no_args_is_help=True,
    add_completion=False,
)

DataRoot = Annotated[
    Path | None,
    typer.Option("--data-root", help="Where projects live (default ~/Movies/Good Feeling, or GOODFEELING_DATA_ROOT)."),
]


def _settings(max_cost: float | None = None, models: dict | None = None):
    from goodfeeling.settings import load_settings

    settings = load_settings()
    if max_cost is not None:
        settings.max_cost_usd = max_cost
    for step, model in (models or {}).items():
        if model:
            setattr(settings.models, step, model)
    return settings


def _confirm(yes: bool):
    if yes or not sys.stdin.isatty():
        return None
    return lambda question: typer.confirm(question, default=True)


@app.command()
def run(
    footage: Annotated[Path, typer.Argument(exists=True, file_okay=False, help="Folder of original clips.")],
    music: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="Music track.")],
    length: Annotated[float, typer.Option(help="Target length in seconds.")] = 150.0,
    style: Annotated[str, typer.Option(help="fast or cinematic.")] = "fast",
    res: Annotated[str, typer.Option(help="1080 or 4k.")] = "1080",
    project: Annotated[str | None, typer.Option(help="Project name (default: the footage folder's name).")] = None,
    data_root: DataRoot = None,
    drop_at: Annotated[float | None, typer.Option(help="Seconds into the track where the drop is.")] = None,
    fps: Annotated[
        str | None, typer.Option(help="Output frame rate, e.g. 60000/1001 (default: the footage's).")
    ] = None,
    no_ai: Annotated[
        bool, typer.Option("--no-ai", help="Skip Claude; use local signals and the deterministic director.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask before spending.")] = False,
    max_cost: Annotated[float | None, typer.Option(help="Refuse to start if the estimate exceeds this (USD).")] = None,
    model_score: Annotated[str | None, typer.Option(help="Model for segment scoring.")] = None,
    model_understand: Annotated[str | None, typer.Option(help="Model for the session summary.")] = None,
    model_direct: Annotated[str | None, typer.Option(help="Model for directing and repair.")] = None,
) -> None:
    """Ingest, score, analyse the music, direct, and render final.mp4."""
    from goodfeeling.pipeline.run import CostRefused, RunOptions
    from goodfeeling.pipeline.run import run as run_pipeline

    if style not in ("fast", "cinematic"):
        raise typer.BadParameter("style must be fast or cinematic")
    if res not in ("1080", "4k"):
        raise typer.BadParameter("res must be 1080 or 4k")
    settings = _settings(max_cost, {"score": model_score, "understand": model_understand, "direct": model_direct})
    started = time.time()
    try:
        result = run_pipeline(
            RunOptions(
                footage=footage,
                music=music,
                length=length,
                style=style,
                res=res,
                project=project,
                data_root=data_root,
                drop_at=drop_at,
                no_ai=no_ai,
                fps=fps,
            ),
            settings,
            confirm=_confirm(yes),
        )
    except CostRefused as exc:
        typer.echo(f"Stopped: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"\nDone in {time.time() - started:.0f} s. Claude cost this run: ${result.cost:.4f}")
    for name, path in result.outputs.items():
        typer.echo(f"  {name:<14} {path}")


@app.command()
def score(
    footage: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    project: Annotated[str | None, typer.Option()] = None,
    data_root: DataRoot = None,
    no_ai: Annotated[bool, typer.Option("--no-ai")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
    max_cost: Annotated[float | None, typer.Option()] = None,
    model_score: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Ingest and score segments, then print the table."""
    from goodfeeling.pipeline.run import CostRefused, RunOptions
    from goodfeeling.pipeline.run import run as run_pipeline
    from goodfeeling.timebase import format_timecode

    settings = _settings(max_cost, {"score": model_score})
    try:
        result = run_pipeline(
            RunOptions(footage=footage, project=project, data_root=data_root, no_ai=no_ai),
            settings,
            confirm=_confirm(yes),
        )
    except CostRefused as exc:
        typer.echo(f"Stopped: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(
        f"\n{'segment':<9} {'kind':<8} {'at':>9} {'sec':>5} {'claude':>6} {'local':>5} {'final':>5}  description"
    )
    fps_of = {
        row["clip_id"]: row
        for row in result.project.db.execute(
            "SELECT clip_id, info_json FROM clips WHERE project_id=?", (result.project.id,)
        )
    }
    import json

    for seg in result.segments:
        fps = tuple(json.loads(fps_of[seg.clip]["info_json"])["fps"])
        claude = f"{seg.claude.score:.0f}" if seg.claude else "-"
        desc = seg.claude.description if seg.claude else ""
        typer.echo(
            f"{seg.id:<9} {seg.kind:<8} {format_timecode(seg.start, fps):>9} "
            f"{seg.frames * fps[1] / fps[0]:>5.1f} {claude:>6} {seg.local_score:>5.1f} {seg.final_score:>5.1f}"
            f"  {desc[:70]}"
        )
    typer.echo(f"\n{result.score_stats}. Claude cost this run: ${result.cost:.4f}")


@app.command()
def render(
    from_edl: Annotated[Path, typer.Option("--from-edl", exists=True, dir_okay=False, help="EDL to render.")],
    out: Annotated[Path | None, typer.Option(help="Output file (default: next to the project's renders).")] = None,
    res: Annotated[str | None, typer.Option(help="Override the EDL's output size: 1080 or 4k.")] = None,
    data_root: DataRoot = None,
) -> None:
    """Render an EDL (for example a hand-edited one) with nothing but the file and the footage."""
    from goodfeeling.media.encoders import choose_encoder
    from goodfeeling.pipeline.render import render as render_edl
    from goodfeeling.pipeline.run import RESOLUTIONS
    from goodfeeling.schemas.edl import EDL
    from goodfeeling.store.project import ensure_outside

    settings = _settings()
    edl = EDL.load(from_edl)
    if res:
        width, height = RESOLUTIONS[res]
        edl = edl.model_copy(update={"output": edl.output.model_copy(update={"width": width, "height": height})})
    root = settings.resolved_data_root(data_root)
    edl_path = from_edl.resolve()
    if out is None:
        project_dir = edl_path.parent.parent if edl_path.parent.name == "edits" else None
        renders = project_dir / "renders" if project_dir and (project_dir / "renders").is_dir() else root / "renders"
        out = renders / f"{edl_path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.mp4"
    out = ensure_outside(out, [Path(s.path).parent for s in edl.sources.values()])
    encoder = choose_encoder(settings.render, edl.output.height)
    started = time.time()
    render_edl(
        edl, out, encoder, root / "cache" / "shots", audio_bitrate=settings.render.audio_bitrate, progress=typer.echo
    )
    typer.echo(f"Rendered {out} in {time.time() - started:.0f} s")


@app.command()
def doctor(
    file: Annotated[Path | None, typer.Argument(exists=True, dir_okay=False, help="A clip to inspect.")] = None,
) -> None:
    """Report ffmpeg, encoders, settings, and (for a file) codec, bit depth, frame rate, and colour transfer."""
    from goodfeeling.doctor import file_report, system_report

    for line in system_report(_settings()):
        typer.echo(line)
    if file:
        typer.echo("")
        for line in file_report(file):
            typer.echo(line)


if __name__ == "__main__":
    app()
