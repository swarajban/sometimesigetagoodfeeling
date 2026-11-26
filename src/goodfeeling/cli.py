"""Command line: `goodfeeling serve | run | score | music | render --from-edl | doctor`."""

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
def serve(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1 for the app.")] = 8765,
    data_root: DataRoot = None,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the app in the browser.")] = True,
    dev: Annotated[
        bool, typer.Option("--dev", help="Also run the Vite dev server with hot reload, and open that instead.")
    ] = False,
    dev_port: Annotated[int, typer.Option(help="Port for the Vite dev server with --dev.")] = 5173,
) -> None:
    """Start the web app and its worker on 127.0.0.1; stopping it stops the worker too."""
    from goodfeeling.server.serve import serve as run_server

    run_server(port, str(data_root) if data_root else None, open_browser, dev, dev_port)


@app.command(hidden=True)
def openapi() -> None:
    """Print the API's OpenAPI schema (the web app's types are generated from it)."""
    import json

    from goodfeeling.server.app import create_app

    typer.echo(json.dumps(create_app().openapi(), indent=2))


@app.command()
def run(
    footage: Annotated[Path, typer.Argument(exists=True, file_okay=False, help="Folder of original clips.")],
    music: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="Music track.")],
    length: Annotated[
        float | None, typer.Option(help="Target length in seconds (default 150, or 30 for a reel).")
    ] = None,
    style: Annotated[str, typer.Option(help="fast or cinematic.")] = "fast",
    res: Annotated[str, typer.Option(help="1080 or 4k.")] = "1080",
    edit_format: Annotated[
        str, typer.Option("--format", help="landscape, or reel for a 9:16 vertical edit (1080x1920).")
    ] = "landscape",
    edit: Annotated[
        str | None, typer.Option(help="Name of this edit (default: 'default', or 'reel' for --format reel).")
    ] = None,
    music_start: Annotated[
        str | None,
        typer.Option(help="Where in the track the edit starts: seconds or MM:SS(.mmm); snaps to a downbeat."),
    ] = None,
    pace: Annotated[
        str | None,
        typer.Option(help="How often it cuts: slower, normal, faster, or a multiplier like 1.2 (default: settings)."),
    ] = None,
    music_end: Annotated[
        str | None, typer.Option(help="Where in the track the edit ends; with --music-start it sets the length.")
    ] = None,
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
    if edit_format not in ("landscape", "reel"):
        raise typer.BadParameter("format must be landscape or reel")
    from goodfeeling.pipeline.pacing import parse_pace
    from goodfeeling.pipeline.stretch import StretchError, parse_time

    try:
        start_s = parse_time(music_start) if music_start else None
        end_s = parse_time(music_end) if music_end else None
        pace_value = parse_pace(pace) if pace else None
    except (StretchError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
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
                format=edit_format,
                edit=edit,
                music_start=start_s,
                music_end=end_s,
                pace=pace_value,
            ),
            settings,
            confirm=_confirm(yes),
        )
    except (CostRefused, StretchError, ValueError) as exc:
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
def music(
    track: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Music file.")],
    length: Annotated[float, typer.Option(help="Edit length to suggest stretches for, in seconds.")] = 150.0,
) -> None:
    """Show a track's tempo and sections, and the --music-start values worth trying."""
    from goodfeeling.pipeline.music import analyze_music
    from goodfeeling.pipeline.stretch import fmt, suggestions

    analysis = analyze_music(track)
    spb = 60 / analysis.tempo
    typer.echo(
        f"{track.name}: {fmt(analysis.duration_s)} long, {analysis.tempo:.1f} BPM, {len(analysis.beats)} beats "
        f"({analysis.grid} grid)"
    )
    typer.echo("Sections (labels and downbeats are estimates):")
    typer.echo(f"  {'#':>2}  {'start':>9}  {'end':>9}  {'label':<6}  energy")
    for i, s in enumerate(analysis.sections):
        bar = "#" * round(s.energy * 20)
        typer.echo(f"  {i:>2}  {fmt(s.start_s):>9}  {fmt(s.end_s):>9}  {s.label:<6}  {s.energy:.2f} {bar}")
    typer.echo(f"\nStretches for a {length:.0f} s edit (paste the flag into goodfeeling run):")
    for why, start, beats in suggestions(analysis, length):
        begin = analysis.beats[start]
        typer.echo(f"  --music-start {fmt(begin)}   {fmt(begin)}-{fmt(begin + beats * spb)}  {why}")


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
    from goodfeeling.pipeline.run import output_size
    from goodfeeling.schemas.edl import EDL
    from goodfeeling.store.project import ensure_outside

    settings = _settings()
    edl = EDL.load(from_edl)
    if res:
        portrait = edl.output.height > edl.output.width
        width, height = output_size(res, "reel" if portrait else "landscape")
        edl = edl.model_copy(update={"output": edl.output.model_copy(update={"width": width, "height": height})})
    root = settings.resolved_data_root(data_root)
    edl_path = from_edl.resolve()
    if out is None:
        edits = next((p for p in edl_path.parents if p.name == "edits"), None)
        renders = root / "renders"
        if edits is not None and (edits.parent / "renders").is_dir():
            renders = edits.parent / "renders" / edl_path.parent.relative_to(edits)
        out = renders / f"{edl_path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.mp4"
    out = ensure_outside(out, [Path(s.path).parent for s in edl.sources.values()])
    encoder = choose_encoder(settings.render, max(edl.output.width, edl.output.height))
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
