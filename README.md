# Sometimes I Get a Good Feeling

Turns a folder of raw DJI O4 FPV clips plus a music track into a beat-synced highlight edit. Claude scores the
footage and directs the edit; code checks the plan, snaps every cut to a beat frame, and renders.

There is a local web app (`goodfeeling serve`) for importing, watching progress, reading the director's plan, and
reviewing segments, and the same engine as command line steps.

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

## The app

The web app needs Node.js once, to build it (`brew install node`):

```sh
cd web && npm install && npm run build && cd ..
uv run goodfeeling serve
```

`serve` starts the server on http://127.0.0.1:8765, or the next free port if that one is taken (it never listens
beyond this machine), starts the worker that runs jobs, and opens the browser at the URL it prints. Ctrl-C stops both.
`--port` picks the port and stops if it's taken; `--no-open` and `--data-root` do what they say.

- **Projects** lists every session with its edits and its Claude spend. Opening one never re-scores it.
- **Import** takes a footage folder and a music file (type a path or browse), length, style, resolution, format,
  pace, and where in the song to start: click the track, or pick one of the suggested stretches. Proxies and
  analysis run locally; the job then waits for you to approve the Claude estimate before any call.
- **Overview** shows the render, the director's plan with its reason for every shot (click a shot to jump to it),
  the session summary, the music sections with the stretch the edit uses, the contact sheet, and a switch between
  the project's edits (the default edit and a reel).
- **Review** is every candidate segment, including those left out, with a looping preview on hover, its score,
  tags, description, and Claude's reason. Include or exclude a segment, star it as a must-have, like or dislike it,
  then **Re-plan**: the director runs again with those choices (scores come from the cache) and the edit is
  re-rendered. Excluded segments are never used, not even as footage a neighbouring shot runs into; starred ones
  must be. Each like, dislike, star, and include/exclude is also recorded in the `feedback` table.
- **Claude log** shows every prompt and response for the project, with tokens and cost.
- **Settings** edits the settings file: API key (shown only by its last four characters), models and effort per
  step, scoring weights, pace, the style note, and the spend limit per run.

Jobs survive restarts. The worker leases each job and renews the lease while it runs; if the server stops, the job
is handed back and resumes from the engine's checkpoints on the next start (finished proxies, cached scores, and
rendered pieces are kept), and if a worker dies outright its lease runs out and the job is picked up again.

### Developing the app

```sh
uv run goodfeeling serve --dev       # the API on :8765 plus the Vite dev server with hot reload on :5173
cd web && npm run gen:api            # after changing the API: regenerate src/api/schema.d.ts from FastAPI's schema
cd web && npm run build              # typecheck and build into web/dist, which serve uses when not in --dev
```

Requests that change something must carry an `X-Goodfeeling: 1` header (the web app sends it), and the server only
answers to `127.0.0.1` and `localhost`, so other web pages can't drive it. To serve it behind a tunnel or preview
proxy, list the extra host names in `GOODFEELING_ALLOWED_HOSTS` (comma-separated), and set `GOODFEELING_LOCK_SPEND=1`
so nobody can change the API key or the spend limit from the browser.

`--dev` runs `npm install` the first time if `web/node_modules` is missing. The web app is React, TypeScript, and
Vite with TanStack Query and Tailwind CSS; its API types are generated, so a server change that breaks the UI fails
the typecheck.

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

### Shot length comes from the content

A shot lasts as long as what's in it deserves.

- **Moments.** After scoring, adjacent strong segments of the same continuous stretch of a clip are joined into
  candidate moments. The best candidates (about one per 20 s of footage, 6 to 30) get a second look: a grid of 16
  time-labelled frames across the candidate and a few seconds either side goes to Claude (Sonnet, low effort). It
  returns where the move starts and ends, what it is, how much it's worth, and whether it deserves a full hold or
  only works as a short hit. Second looks are cached by content like scores, and the cost is printed in the estimate
  before anything is spent. On the sample, 10 second looks cost $0.067, about $0.007 each, so a 20-minute session
  (capped at 30 looks) costs about $0.20.
- **The director** plays the best moments from approach to exit (8-12 s is fine), and uses ordinary segments as
  short connecting shots or leaves them out. Every shot's reason says why it gets its length.
- **The validator** no longer targets an average. It keeps the floor (1.5 s landscape, 1.2 s reel; only a burst of
  2-4 quick cuts may go under, at most one per 30 s) and caps filler at 2.5 s (2 s for reels, 3.5 s cinematic). Only a
  marked moment, or a segment scoring 8+, may hold longer. A moment may not be held past its span, so nothing is
  padded with dead air. Two shots may never use the same footage.
- **`--pace slower|normal|faster`** or a multiplier is a bias, not a target. Slower holds the big moments more
  readily and keeps fewer connecting shots; faster keeps more short hits between them. `"pace"` in settings sets
  the default.

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
app.db                         projects, clips, segments, moments, score and moment caches, Claude log, edits,
                               jobs and their progress, review choices, feedback events
cache/shots/                   rendered pieces, keyed by content, reused across re-renders
projects/<name>/
  proxies/                     8-bit 720p H.264 proxies (keyframe every 15 frames)
  analysis/                    clips.json, signals, segments.json, moments.json, music.json (hand-editable), summary
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
  "models": { "score": "claude-sonnet-5-5", "moments": "claude-sonnet-5-5", "understand": "claude-opus-5-5", "direct": "claude-opus-5-5" },
  "effort": { "score": "low", "moments": "low", "understand": "medium", "direct": "medium" },
  "weights": { "claude": 0.7, "motion": 0.15, "sharpness": 0.1, "exposure": 0.05 },
  "style_note": "I like low proximity passes over water; skip high cruising.",
  "pace": "normal",
  "props_penalty": 0.0,
  "lut": "/path/to/DJI D-Log M to Rec.709.cube",
  "max_cost_usd": 5.0,
  "render": { "encoder": "auto", "bitrate_1080": "24M", "bitrate_4k": "80M", "videotoolbox_quality": null }
}
```

`props_penalty` is taken off a segment's blended score when Claude sees propellers in view. It is 0 by default, so
prop tips at the frame edge cost nothing; the flag still shows in Review and in what the director reads.

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
cd web && npm ci && npm run build
```

The tests are deliberately few: originals untouched after a full run, EDL validation and round trips (including
`"MM:SS.mmm"`), cuts landing on beat frames without drift, a job resuming after its worker dies, review choices
reaching the director, and the API key never going back to the browser, on tiny synthetic ffmpeg fixtures.
