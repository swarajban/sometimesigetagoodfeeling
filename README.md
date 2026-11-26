# Sometimes I Get a Good Feeling

Turns a folder of raw DJI O4 FPV clips plus a music track into a beat-synced highlight edit. Claude scores the
footage and directs the edit; code checks the plan, snaps every cut to a beat frame, and renders.

Phase 1 is the engine and the command line. The web app (`web/`) and server arrive in later phases.

## Setup (macOS, Apple Silicon)

```sh
brew install ffmpeg uv
git clone https://github.com/swarajban/sometimesigetagoodfeeling.git
cd sometimesigetagoodfeeling
uv sync
export ANTHROPIC_API_KEY=...        # or put it in the settings file (below); never in the repo
uv run goodfeeling doctor
```

On Linux, install ffmpeg from your package manager; renders use libx264 there.

## Commands

```sh
uv run goodfeeling run ./footage --music song.mp3 --length 150 --style fast --res 1080
uv run goodfeeling run ./footage --music song.mp3 --music-start 1:20 --length 120
uv run goodfeeling run ./footage --music song.mp3 --format reel
uv run goodfeeling music song.mp3 --length 120
uv run goodfeeling score ./footage
uv run goodfeeling render --from-edl "~/Movies/Good Feeling/projects/footage/edits/edl.json"
uv run goodfeeling doctor [clip.MP4]
```

`run` ingests and groups clips into flights, makes 720p proxies, segments, scores segments with Claude,
analyses the music, writes a session summary, directs the edit, validates it, and renders `final.mp4`.
Before the first Claude call it prints a cost estimate and, in a terminal, asks before spending
(`--yes` skips the question; `--max-cost` refuses to start above a limit). Useful options: `--style
cinematic`, `--res 4k`, `--pace slower`, `--drop-at 64.5` (seconds into the track), `--project NAME`, `--no-ai` (local signals
and the deterministic director, no API calls), `--model-score`, `--model-direct`.

### Choosing the stretch of the song

By default `run` picks the stretch itself (from the first downbeat, or later if that would leave the drop out).
To choose it, run `goodfeeling music song.mp3 --length 120`: it prints the tempo, duration, a table of sections
with start and end times, labels and energy, and the default stretch for that length plus a few alternatives, each
as the exact `--music-start` value to paste. Then:

- `--music-start 1:20` (seconds or `MM:SS(.mmm)`) starts the edit on the downbeat nearest that time, and
  `--length` sets the end.
- `--music-start 1:20 --music-end 3:20` uses that stretch; it sets the length, and `--length` is ignored.
- If the chosen stretch has no drop, the director puts its strongest shots on the stretch's highest-energy section.

### Vertical reels (TikTok, Instagram Reels)

`--format reel` makes a 1080x1920 edit, 30 s unless you pass `--length`. Every shot is centre-cropped from the
source to 9:16 and scaled to fill. The director opens on one of the strongest shots in the first second, cuts
faster, and prefers shots whose detail sits in the centre of the frame. Unless `--music-start` pins it, the music
starts on the track's highest-energy section. A reel of already-scored footage makes no scoring calls; only the
director call is new.

Each shot in the EDL has an optional `crop_x` (0 = keep the left edge, 0.5 = centre, the default, 1 = the right
edge). Set it by hand for a shot whose subject is off-centre, then re-render with `render --from-edl`.

### Pacing

Pacing is a target shot length in seconds, converted to beats at the track's tempo, so a 90 BPM and a 170 BPM track
cut at a similar rate:

| | average shot | most shots | floor |
|---|---:|---:|---:|
| `fast` landscape | about 3 s | 2-5 s | 1.5 s |
| `cinematic` landscape | about 5.5 s | 3.5-8 s | 1.5 s |
| reel (`fast`) | about 2.5 s | 1.5-4 s | 1.2 s |

Quiet sections hold longer and loud ones a little shorter. The validator rejects shots under the floor, except a
burst of 2-4 quick cuts used as an accent (at most one per 30 s), so sustained quick cutting isn't possible at
normal pace. A hold longer than its segment runs on into the clean footage around it in the same clip (never into a
dropped span or a crash, and never into footage another shot uses).

`--pace slower|normal|faster` or a multiplier (`--pace 1.2` cuts 20% more often; `0.75` is `slower`) scales all of
this; `"pace"` in the settings file sets the default.

### Named edits

A project can hold several edits. `--edit NAME` names one; a reel is named `reel` unless you say otherwise. The
default edit keeps the paths below; a named edit lives beside it in `edits/NAME/` and `renders/NAME/`, so a
reel never overwrites the landscape edit.

`render --from-edl` re-renders any EDL, including one you edited by hand. Shot `in`/`out` accept frame numbers
or `"MM:SS.mmm"` strings.

Without an API key, `run` still works: scores come from local signals and the deterministic director plans.

## Where things go

Original footage is only ever opened read-only, and nothing is written next to it. Everything else lives under
the data root, `~/Movies/Good Feeling/` by default (override with `--data-root`, `GOODFEELING_DATA_ROOT`, or
`data_root` in the settings file):

```
app.db                         projects, clips, segments, score cache, Claude log, edits and their revisions
cache/shots/                   rendered pieces, keyed by content, reused across re-renders
projects/<name>/
  proxies/                     8-bit 720p H.264 proxies (keyframe every 15 frames)
  analysis/                    clips.json, signals, segments.json, music.json (hand-editable), summary
  frames/                      labelled frame strips sent to Claude
  edits/edl.json               the default edit (plus revisions/0001.json ...), plan.md, plan.json
  edits/summary.md             session summary, shared by every edit
  edits/<name>/edl.json        a named edit, e.g. edits/reel/ (with its own revisions/, plan.md, plan.json)
  renders/final.mp4            the default edit's render, and contact_sheet.jpg
  renders/<name>/final.mp4     a named edit's render and contact sheet
  logs/ai.jsonl, logs/run.log  every prompt and response (images replaced by size and hash), run log
```

## Settings

Optional JSON at `~/Library/Application Support/Good Feeling/settings.json` on macOS
(`~/.config/goodfeeling/settings.json` elsewhere, or `GOODFEELING_CONFIG`). Every field is optional:

```json
{
  "data_root": "~/Movies/Good Feeling",
  "models": { "score": "claude-sonnet-5-5", "understand": "claude-opus-5-5", "direct": "claude-opus-5-5" },
  "effort": { "score": "low", "understand": "medium", "direct": "medium" },
  "weights": { "claude": 0.7, "motion": 0.15, "sharpness": 0.1, "exposure": 0.05 },
  "style_note": "I like low proximity passes over water; skip high cruising.",
  "pace": "normal",
  "lut": "/path/to/DJI D-Log M to Rec.709.cube",
  "max_cost_usd": 5.0,
  "render": { "encoder": "auto", "bitrate_1080": "24M", "bitrate_4k": "80M", "videotoolbox_quality": null }
}
```

Prices for the cost estimate and meter live in one table, `src/goodfeeling/ai/pricing.py`.

## How the edit stays on the beat

Time is integer frames at rational frame rates (`60000/1001`, never `59.94`). Each beat is converted once to an
absolute output frame, and every shot is a whole number of beats, so a cut's frame is always a beat's frame and
rounding never accumulates. Crossfades borrow frames from outside the neighbouring shots and are centred on the
cut, so they never move a later cut. `scripts/measure_sync.py final.mp4 edl.json --tempo <bpm>` checks a render
from the outside: it finds the cuts in the picture and the beats in the rendered audio and compares them.

## Sample media

`scripts/fetch_sample.py` downloads the freely licensed sample used during development (four CC BY 3.0 4K FPV
clips from Wikimedia Commons and a CC BY 4.0 track by Kevin MacLeod; sources and licences are in
`scripts/sample_media.json` and the generated `CREDITS.md`). Media is never committed.

## Mac handoff: run it on a real DJI O4 clip

Copy-paste on an Apple Silicon Mac. Use a folder of real O4 clips (at least one, ideally a flight split across two
files) and any music file.

```sh
brew install ffmpeg uv
git clone https://github.com/swarajban/sometimesigetagoodfeeling.git && cd sometimesigetagoodfeeling
git checkout phase-1-engine-cli
uv sync
export ANTHROPIC_API_KEY=...
FOOTAGE=~/Movies/O4                       # folder with DJI_*.MP4 files from the O4
MUSIC=~/Music/track.mp3

# 1. Tools and encoders: expect hevc_videotoolbox and h264_videotoolbox to say "works".
uv run goodfeeling doctor
# 2. One real clip: codec, bit depth, frame rate, colour transfer, DJI name, tags.
uv run goodfeeling doctor "$(ls "$FOOTAGE"/DJI_*.MP4 | head -1)"
# 3. The same sample flow as the cloud run (needs the key; prints the estimate first).
uv run goodfeeling run "$FOOTAGE" --music "$MUSIC" --length 60 --style fast --res 1080
# 4. Second run: should report {'cached': N, 'scored': 0, ...} and make no scoring calls.
uv run goodfeeling run "$FOOTAGE" --music "$MUSIC" --length 60 --style fast --res 1080 --yes
# 5. Re-render from the EDL, and a 4K render.
uv run goodfeeling render --from-edl "$HOME/Movies/Good Feeling/projects/$(basename "$FOOTAGE")/edits/edl.json"
uv run goodfeeling render --from-edl "$HOME/Movies/Good Feeling/projects/$(basename "$FOOTAGE")/edits/edl.json" --res 4k
# 6. Check the render.
P="$HOME/Movies/Good Feeling/projects/$(basename "$FOOTAGE")"
ffprobe -v error -show_entries stream=codec_name,codec_tag_string,pix_fmt,width,height,r_frame_rate "$P/renders/final.mp4"
uv run python scripts/measure_sync.py "$P/renders/final.mp4" "$P/edits/edl.json" --tempo "$(python3 -c "import json;print(json.load(open('$P/analysis/music.json'))['tempo'])")"
```

What the Mac run has to confirm, because the cloud build machine (Linux, no DJI files) could not:

1. `doctor` lists `hevc_videotoolbox` as working and `run` renders with it; `final.mp4` is HEVC tagged `hvc1`,
   plays in QuickTime and Photos, and imports into iMovie.
2. Concatenating VideoToolbox-encoded pieces by stream copy plays without glitches at the cuts.
3. A real O4 file probes correctly: rational frame rate (for example `60000/1001`), frame count, 10-bit HEVC when
   recorded that way, and the colour transfer DJI writes. Note what `doctor` prints for a D-Log M clip: DJI does
   not seem to tag D-Log M, so the LUT is a setting today.
4. The 8-bit proxy of a 10-bit HEVC clip has the same frame count as the original.
5. Flight grouping on real file names: a recording split across files is reported as one flight
   (`analysis/clips.json`, `continues_previous: true`), and separate flights split. Whether `creation_time` is
   reliable on the O4 is unverified; grouping uses the filename first.
6. `measure_sync.py` reports detected cut frames equal to planned ones and no drift.
7. Render speed on the Mac at 1080p and 4K.

## Development

```sh
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run pytest -q
```

The tests are deliberately few: originals untouched after a full run, EDL validation and round trips (including
`"MM:SS.mmm"`), and cuts landing on beat frames without drift, on tiny synthetic ffmpeg fixtures.
