"""The worker process: claims jobs, runs the engine's steps, streams progress into job_events.

`goodfeeling serve` starts it and stops it. It also stops on its own when the serve process dies (it checks its
parent every few seconds), handing its job back to the queue so the next start resumes it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from goodfeeling.jobs import queue
from goodfeeling.settings import load_settings
from goodfeeling.store.db import Database

log = logging.getLogger("goodfeeling.worker")

HEARTBEAT_S = 3.0
STAGES = {
    "Project": "prepare",
    "Ingest": "ingest",
    "Segment": "segment",
    "Music": "music",
    "Cost estimate": "estimate",
    "Score": "score",
    "Understand": "understand",
    "Direct": "direct",
    "Render": "render",
}
COUNT = re.compile(r"(\d+)/(\d+)\s*$")


class Interrupted(BaseException):
    """Raised in the main thread when the running job must stop: cancel, shutdown, or a lost lease."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NeedsApproval(Exception):
    def __init__(self, estimate: dict):
        super().__init__("waiting for approval")
        self.estimate = estimate


@dataclass
class State:
    job_id: int | None = None
    stop_reason: str | None = None
    stop_job: int | None = None
    shutting_down: bool = False


def stage_of(line: str) -> str | None:
    if line.startswith(" "):
        return None
    return next((stage for prefix, stage in STAGES.items() if line.startswith(prefix)), None)


def estimate_dict(estimates, expected: float, worst: float, limit: float) -> dict:
    return {
        "steps": [
            {
                "step": e.step,
                "model": e.model,
                "requests": e.requests,
                "input_tokens": e.input_tokens,
                "output_tokens": e.output_tokens,
                "cost": round(e.cost, 4),
            }
            for e in estimates
        ],
        "expected": round(expected, 4),
        "worst": round(worst, 4),
        "limit": limit,
    }


def run_job(db: Database, job: dict, owner: str) -> None:
    from goodfeeling.ai.client import AiError
    from goodfeeling.pipeline.run import CostRefused, RunOptions, run
    from goodfeeling.pipeline.stretch import StretchError
    from goodfeeling.store.project import UnsafeWrite

    job_id = job["id"]
    params = json.loads(job["params_json"])
    settings = load_settings()

    def progress(line: str) -> None:
        line = str(line)
        queue.add_event(db, job_id, line)
        print(f"[job {job_id}] {line}", flush=True)
        stage = stage_of(line)
        count = COUNT.search(line)
        if stage or count:
            queue.set_stage(db, job_id, stage, *(map(int, count.groups()) if count else (None, None)))

    def gate(estimates, expected: float, worst: float) -> None:
        if worst > settings.max_cost_usd:
            return
        approved = queue.get(db, job_id)["approved_usd"]
        if approved is not None and worst <= approved + 0.005:
            return
        raise NeedsApproval(estimate_dict(estimates, expected, worst, settings.max_cost_usd))

    resumed = bool(queue.events_after(db, job_id, 0, limit=1))
    queue.add_event(db, job_id, "Resumed from the last checkpoint" if resumed else "Started")
    try:
        options = RunOptions.from_json(params["options"])
        result = run(options, settings, progress=progress, estimate_hook=gate)
    except NeedsApproval as exc:
        if queue.cancel_requested(db, job_id):
            queue.add_event(db, job_id, "Cancelled")
            queue.finish(db, job_id, owner, "cancelled", error="cancelled")
            return
        queue.add_event(db, job_id, f"Waiting for approval: about ${exc.estimate['expected']:.2f}")
        queue.finish(db, job_id, owner, "approval", estimate=exc.estimate)
        return
    except (CostRefused, StretchError, ValueError, FileNotFoundError, AiError, UnsafeWrite, RuntimeError) as exc:
        queue.add_event(db, job_id, f"Failed: {exc}")
        queue.finish(db, job_id, owner, "failed", error=str(exc))
        return
    except Exception as exc:
        queue.add_event(db, job_id, "Failed:\n" + traceback.format_exc(limit=8))
        queue.finish(db, job_id, owner, "failed", error=f"{type(exc).__name__}: {exc}")
        return
    root = result.project.root
    outputs = {k: str(Path(v).relative_to(root)) for k, v in result.outputs.items()}
    queue.add_event(db, job_id, f"Done. Claude cost this run: ${result.cost:.4f}")
    queue.finish(
        db,
        job_id,
        owner,
        "done",
        result={"cost": result.cost, "outputs": outputs, "score_stats": result.score_stats},
    )


def heartbeat(db: Database, state: State, owner: str, parent: int | None) -> None:
    while True:
        time.sleep(HEARTBEAT_S)
        if parent is not None and os.getppid() != parent:
            state.stop_reason = "shutdown"
            os.kill(os.getpid(), signal.SIGTERM)
            return
        job_id = state.job_id
        if job_id is None or state.stop_reason:
            continue
        reason = None
        if not queue.renew(db, job_id, owner):
            job = queue.get(db, job_id)
            if job and job["status"] == "running" and job["lease_owner"] not in (None, owner):
                reason = "lost"
        elif queue.cancel_requested(db, job_id):
            reason = "cancel"
        if reason and state.job_id == job_id:
            state.stop_reason, state.stop_job = reason, job_id
            os.kill(os.getpid(), signal.SIGUSR1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="goodfeeling-worker")
    parser.add_argument("--data-root")
    parser.add_argument("--parent", type=int)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s worker %(levelname)s %(message)s")
    root = load_settings().resolved_data_root(args.data_root)
    db = Database(root / "app.db")
    owner = queue.worker_id()
    state = State()

    def interrupt(signum, _frame):
        if signum == signal.SIGTERM:
            state.shutting_down = True
            state.stop_reason = "shutdown"
        if state.job_id is not None and (signum == signal.SIGTERM or state.stop_job == state.job_id):
            raise Interrupted(state.stop_reason or "shutdown")
        if state.shutting_down:
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGUSR1, interrupt)
    threading.Thread(target=heartbeat, args=(db, state, owner, args.parent), daemon=True).start()
    resumed = queue.requeue_orphans(db)
    print(f"worker {owner} on {root}" + (f", resuming {resumed} job(s)" if resumed else ""), flush=True)
    while not state.shutting_down:
        job = queue.claim(db, owner)
        if job is None:
            try:
                time.sleep(0.5)
            except Interrupted:
                break
            continue
        state.job_id, state.stop_reason, state.stop_job = job["id"], None, None
        try:
            run_job(db, job, owner)
        except Interrupted as exc:
            if exc.reason == "cancel":
                queue.add_event(db, job["id"], "Cancelled")
                queue.finish(db, job["id"], owner, "cancelled", error="cancelled")
            elif exc.reason == "shutdown":
                queue.release(db, job["id"], owner, "Paused: the server is stopping; it resumes on the next start")
            else:
                queue.add_event(db, job["id"], "Lost the job's lease; another worker took it over")
        finally:
            state.job_id = None
    sys.exit(0)


if __name__ == "__main__":
    main()
