"""SQLite-backed job queue with leases.

A worker claims a job with a lease and renews it while it runs. A worker that dies stops renewing, its lease
expires, and the next claim picks the job up again. The engine's steps are idempotent and checkpointed, so running
a job again resumes it.
"""

from __future__ import annotations

import json
import os
import socket
import time
from typing import Any

from goodfeeling.store.db import Database

LEASE_S = 30.0
MAX_ATTEMPTS = 4
ACTIVE = ("queued", "running", "approval")
FINISHED = ("done", "failed", "cancelled")


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def enqueue(db: Database, kind: str, params: dict[str, Any], *, project_id: int | None, edit: str | None) -> int:
    now = time.time()
    cur = db.execute(
        "INSERT INTO jobs (project_id, kind, edit, params_json, status, created_at, updated_at) "
        "VALUES (?,?,?,?, 'queued', ?, ?)",
        (project_id, kind, edit, json.dumps(params), now, now),
    )
    return int(cur.lastrowid)


def get(db: Database, job_id: int) -> dict[str, Any] | None:
    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def claim(db: Database, owner: str) -> dict[str, Any] | None:
    """Take the oldest queued job, or a running one whose lease expired."""
    while True:
        now = time.time()
        row = db.execute(
            "UPDATE jobs SET status='running', lease_owner=?, lease_expires=?, attempts=attempts+1, "
            "started_at=COALESCE(started_at, ?), updated_at=? "
            "WHERE id=(SELECT id FROM jobs WHERE status='queued' OR (status='running' AND lease_expires<?) "
            "ORDER BY id LIMIT 1) RETURNING *",
            (owner, now + LEASE_S, now, now, now),
        ).fetchone()
        if row is None:
            return None
        job = dict(row)
        if job["attempts"] <= MAX_ATTEMPTS:
            return job
        finish(db, job["id"], owner, "failed", error=f"gave up after {MAX_ATTEMPTS} attempts")


def renew(db: Database, job_id: int, owner: str) -> bool:
    """Extend the lease. False when the job is no longer ours (the lease was lost)."""
    cur = db.execute(
        "UPDATE jobs SET lease_expires=? WHERE id=? AND lease_owner=? AND status='running'",
        (time.time() + LEASE_S, job_id, owner),
    )
    return cur.rowcount == 1


def cancel_requested(db: Database, job_id: int) -> bool:
    row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def requeue_orphans(db: Database) -> int:
    """Queue again the running jobs whose worker process on this machine is gone, so a restart resumes them at once
    instead of waiting for the lease to run out."""
    host = socket.gethostname()
    count = 0
    for row in db.execute("SELECT id, lease_owner FROM jobs WHERE status='running'").fetchall():
        owner_host, _, pid = (row["lease_owner"] or "").rpartition(":")
        if owner_host != host or not pid.isdigit() or _alive(int(pid)):
            continue
        cur = db.execute(
            "UPDATE jobs SET status='queued', lease_owner=NULL, lease_expires=NULL, updated_at=? "
            "WHERE id=? AND status='running' AND lease_owner=?",
            (time.time(), row["id"], row["lease_owner"]),
        )
        if cur.rowcount:
            add_event(db, row["id"], "The worker stopped mid-job; resuming from the last checkpoint")
            count += 1
    return count


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def release(db: Database, job_id: int, owner: str, message: str) -> None:
    """Hand a running job back to the queue because the worker is shutting down; it doesn't count as an attempt."""
    db.execute(
        "UPDATE jobs SET status='queued', lease_owner=NULL, lease_expires=NULL, attempts=MAX(0, attempts-1), "
        "updated_at=? WHERE id=? AND lease_owner=? AND status='running'",
        (time.time(), job_id, owner),
    )
    add_event(db, job_id, message)


def finish(
    db: Database,
    job_id: int,
    owner: str | None,
    status: str,
    *,
    result: dict | None = None,
    error: str | None = None,
    estimate: dict | None = None,
) -> None:
    now = time.time()
    done = status in FINISHED
    db.execute(
        "UPDATE jobs SET status=?, result_json=COALESCE(?, result_json), error=?, "
        "attempts=CASE WHEN ?='approval' THEN MAX(0, attempts-1) ELSE attempts END, "
        "estimate_json=COALESCE(?, estimate_json), lease_owner=NULL, lease_expires=NULL, updated_at=?, "
        "finished_at=? WHERE id=? AND (? IS NULL OR lease_owner=?)",
        (
            status,
            json.dumps(result) if result is not None else None,
            error,
            status,
            json.dumps(estimate) if estimate is not None else None,
            now,
            now if done else None,
            job_id,
            owner,
            owner,
        ),
    )


def set_stage(db: Database, job_id: int, stage: str | None, done: int | None, total: int | None) -> None:
    db.execute(
        "UPDATE jobs SET stage=COALESCE(?, stage), step_done=?, step_total=?, updated_at=? WHERE id=?",
        (stage, done, total, time.time(), job_id),
    )


def add_event(db: Database, job_id: int, message: str) -> None:
    db.execute("INSERT INTO job_events (job_id, message, created_at) VALUES (?,?,?)", (job_id, message, time.time()))


def events_after(db: Database, job_id: int, after: int, limit: int = 500) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT id, message, created_at FROM job_events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
        (job_id, after, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def approve(db: Database, job_id: int) -> bool:
    """Approve the spend in the job's estimate (up to its worst case) and queue it again."""
    job = get(db, job_id)
    if job is None or job["status"] != "approval" or not job["estimate_json"]:
        return False
    worst = json.loads(job["estimate_json"])["worst"]
    cur = db.execute(
        "UPDATE jobs SET status='queued', approved_usd=?, updated_at=? WHERE id=? AND status='approval'",
        (worst, time.time(), job_id),
    )
    if cur.rowcount:
        add_event(db, job_id, f"Approved up to ${worst:.2f}")
    return bool(cur.rowcount)


def cancel(db: Database, job_id: int) -> bool:
    """Cancel a job: at once if it is waiting, or at the worker's next checkpoint if it is running."""
    job = get(db, job_id)
    if job is None or job["status"] in FINISHED:
        return False
    now = time.time()
    db.execute(
        "UPDATE jobs SET cancel_requested=1, updated_at=?, "
        "finished_at=CASE WHEN status IN ('queued','approval') THEN ? ELSE finished_at END, "
        "status=CASE WHEN status IN ('queued','approval') THEN 'cancelled' ELSE status END WHERE id=?",
        (now, now, job_id),
    )
    add_event(db, job_id, "Cancel requested")
    return True
