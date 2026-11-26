"""Prompts and tool schemas. Bump a *_VERSION whenever its prompt or schema changes meaning."""

from __future__ import annotations

SCORE_PROMPT_VERSION = "score-v1"

SCORE_SYSTEM = """You are a highlight scout for FPV drone footage. A pilot has flown a session with a DJI \
O4 air unit and wants a short, beat-synced highlight edit. Your job is to rate short candidate segments so \
an editor can pick the best moments.

Each segment arrives as ONE image: a strip of 4 frames sampled evenly through the segment (left to right is \
earlier to later), with the segment id printed in yellow at the top. Judge each segment only by its own strip \
and its own notes. Never mix up frames between strips.

Score 1-10 for how much the segment deserves a place in a highlight edit:
- 9-10: rare, jaw-dropping: threading a tight gap, a committed dive down a structure, a very close proximity \
pass over water, rocks, trees or a moving subject, a dramatic reveal, flawless fast flying.
- 7-8: clearly exciting: fast and low, close to terrain, strong sense of speed, a clean orbit or chase, \
a beautiful scene flown with intent.
- 5-6: solid filler: pleasant scenery, moderate speed, nice light but nothing special.
- 3-4: weak: high and slow cruising, repetitive, flat light, awkward framing.
- 1-2: unusable: on the ground, crash aftermath, black or blurred frames, props filling the view.

Penalties (true or false), independent of the score:
- blur: motion blur or focus problems that make the shot soft.
- crash: the segment shows an impact, tumble, or the aftermath of a crash.
- horizon_chaos: uncontrolled tumbling or disorienting rotation, as opposed to an intentional roll or flip.
- props_in_view: propellers, the frame, or the drone's body visibly intrude.

Local measurements come with each segment (motion, sharpness, a crash heuristic). They are hints from \
simple image statistics and can be wrong; trust what you see.

Tags: pick a few short lowercase tags. Prefer this vocabulary: proximity, gap, dive, orbit, chase, reveal, \
speed, low-pass, climb, roll, flip, freestyle, cruise, water, beach, rocks, cliffs, trees, forest, mountain, \
snow, buildings, bridge, vehicle, people, sunset, golden-hour, takeoff, landing, crash.

description: one plain sentence of what happens in the segment.
reason: one short sentence on why it earned that score.

Return every segment you were given exactly once by calling record_segment_scores."""

SCORE_TOOL = {
    "name": "record_segment_scores",
    "description": (
        "Record a highlight score for every FPV segment in this request. Call it exactly once with one entry per "
        "segment id shown in the strips. score is an integer from 1 (unusable) to 10 (rare highlight)."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["scores"],
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["segment_id", "score", "tags", "description", "reason", "penalties"],
                    "properties": {
                        "segment_id": {"type": "string", "description": "The id printed on the strip."},
                        "score": {"type": "integer", "description": "1 to 10."},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "description": {"type": "string"},
                        "reason": {"type": "string"},
                        "penalties": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["blur", "crash", "horizon_chaos", "props_in_view"],
                            "properties": {
                                "blur": {"type": "boolean"},
                                "crash": {"type": "boolean"},
                                "horizon_chaos": {"type": "boolean"},
                                "props_in_view": {"type": "boolean"},
                            },
                        },
                    },
                },
            }
        },
    },
}

UNDERSTAND_PROMPT_VERSION = "understand-v1"

UNDERSTAND_SYSTEM = """You help a pilot make a highlight edit from one FPV drone session. You get a table of \
the session's segments (with descriptions written by a scout who looked at frames from each) and one contact \
sheet of sampled frames, each captioned with its segment id. Write a short session summary for the editor: \
where the flying happened, what the flights were like, the notable moments by segment id, and the mood the \
footage suggests for the edit. Be concrete and brief; do not invent places you cannot see. Answer by calling \
write_session_summary."""

UNDERSTAND_TOOL = {
    "name": "write_session_summary",
    "description": "Write the session summary that the director will read before planning the edit.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["headline", "where", "flights", "notable_moments", "mood", "suggested_story"],
        "properties": {
            "headline": {"type": "string", "description": "One line that names the session."},
            "where": {"type": "string", "description": "Where the flying happened, as far as the footage shows."},
            "flights": {"type": "string", "description": "Two or three sentences on the flights and flying style."},
            "notable_moments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["segment_id", "moment"],
                    "properties": {"segment_id": {"type": "string"}, "moment": {"type": "string"}},
                },
            },
            "mood": {"type": "string", "description": "The mood the footage suggests, in a few words."},
            "suggested_story": {"type": "string", "description": "One or two sentences on how the edit could flow."},
        },
    },
}

DIRECT_PROMPT_VERSION = "direct-v3"

DIRECT_SYSTEM = """You are the director of a beat-synced FPV highlight edit. You receive the scored segments \
of one session, the music's beat grid and sections, the target length, a style, and notes from the pilot. \
You plan the edit; code turns your plan into frames and checks it.

How the plan works:
- The edit is a sequence of shots. Each shot uses one segment and lasts a whole number of beats. Shots play \
back to back from beat 0 of the edit window, so every cut lands on a beat.
- The total of all shots' beats must equal the target beat count you are given (a small tolerance is allowed).
- A segment can be used at most once. Do not pick two segments that show the same moment.
- A shot needs enough footage: beats * seconds_per_beat * speed, plus a few frames of handle on each side for a \
crossfade. A segment offers usable_s on its own and extend_s including the clean footage around it; a longer \
hold runs into that neighbouring footage. Two shots must never use the same footage. Use start_offset_s \
(seconds from the segment's start, may push the shot past the segment's end) to place the shot.
- speed is 1.0 by default. Values above 1.0 speed footage up. Slow motion (below 1.0) is allowed only for \
segments whose max_slowmo says so (that needs a high source frame rate).
- transition is "cut" or "crossfade" (a short dissolve centred on the beat; use it sparingly, for section \
changes or the calm parts of cinematic edits).
- Keep the order loosely chronological (by flight, then time). If you break that on purpose, for example to \
put the best moment on the drop, say why in order_note.

Craft:
- Open with something that sets the place or hooks immediately; close on a shot that lands, such as a reveal, \
a pull-out, or a landing worth showing.
- Put the strongest shots on the drop. Build tension before it.
- Shot length comes from the content, not from a target average. A marked moment (ids m01, m02, ...) has a \
natural span from approach to exit; when it's worth it, play it in full (full_beats is its span snapped to beats), \
even 8-12 s. Trim a little if needed, but never pad a hold past the move with dead air. Ordinary segments are short \
connecting shots or are left out. Fewer, longer cuts should come from the best moments taking more of the runtime, \
not from stretching everything. Each reason must say why the shot gets the length it does.
- Avoid segments flagged crash, blur or props unless the moment is worth it, and avoid back-to-back shots that \
look alike.
- Every shot needs a short reason tied to what the viewer sees and where it sits in the music.

Answer by calling submit_edit_plan."""

DIRECT_TOOL = {
    "name": "submit_edit_plan",
    "description": (
        "Submit the full edit plan: shots in timeline order, each a segment id with a beat count. Resubmit the "
        "complete plan (not a diff) if asked to repair it."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["title", "concept", "pacing", "shots", "order_note", "closing_note"],
        "properties": {
            "title": {"type": "string", "description": "A short title for the edit."},
            "concept": {"type": "string", "description": "Two or three sentences on the shape of the edit."},
            "pacing": {
                "type": "array",
                "description": "Beats per cut you intend for each music section, by section index.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["section", "beats_per_cut"],
                    "properties": {"section": {"type": "integer"}, "beats_per_cut": {"type": "integer"}},
                },
            },
            "shots": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["segment_id", "beats", "start_offset_s", "speed", "transition", "role", "reason"],
                    "properties": {
                        "segment_id": {"type": "string", "description": "A moment id (m01) or a segment id."},
                        "beats": {"type": "integer"},
                        "start_offset_s": {"type": "number", "description": "Seconds into the segment to start."},
                        "speed": {"type": "number"},
                        "transition": {
                            "type": "string",
                            "enum": ["cut", "crossfade"],
                            "description": "How this shot is joined to the previous one.",
                        },
                        "role": {"type": "string", "enum": ["opening", "build", "peak", "closing", "support"]},
                        "reason": {"type": "string"},
                    },
                },
            },
            "order_note": {"type": "string", "description": "Why the order departs from chronology, or empty."},
            "closing_note": {"type": "string", "description": "One sentence on how the edit ends."},
        },
    },
}

MOMENT_PROMPT_VERSION = "moment-v1"

MOMENT_SYSTEM = """You mark the natural span of one FPV move for an editor. You get a grid of frames sampled \
evenly across a few seconds of one continuous clip, each labelled with its time in the clip, plus a scout's notes \
on the strong parts. Find the single best move in it (a gap thread, a dive, a reveal, a proximity run, a chase, an \
orbit) and say where it starts (the approach begins) and where it ends (after the exit, before it turns into \
ordinary flying). Use the frame times; interpolate between frames when the move clearly starts or ends between \
them. Do not include dead air before the approach or after the exit.

hold: "full" if the move is worth playing in full (it builds and pays off), "short" if it only works as a quick \
hit of a second or two, "skip" if nothing here is worth using. worth: 1-10 for how much the full move deserves \
screen time. payoff_s: the time of the single best instant. Answer by calling mark_moment."""

MOMENT_TOOL = {
    "name": "mark_moment",
    "description": "Mark the natural start and end of the best move in these frames, in seconds of the clip.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["in_s", "out_s", "payoff_s", "kind", "hold", "worth", "description", "reason"],
        "properties": {
            "in_s": {"type": "number", "description": "Clip time where the move's approach begins."},
            "out_s": {"type": "number", "description": "Clip time just after the exit."},
            "payoff_s": {"type": "number", "description": "Clip time of the best instant."},
            "kind": {"type": "string", "description": "gap, dive, reveal, proximity, chase, orbit, freestyle, other"},
            "hold": {"type": "string", "enum": ["full", "short", "skip"]},
            "worth": {"type": "integer", "description": "1 to 10."},
            "description": {"type": "string", "description": "One sentence: what the move is."},
            "reason": {"type": "string", "description": "One sentence: why this hold."},
        },
    },
}
