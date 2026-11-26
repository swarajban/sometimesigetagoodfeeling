"""The server and jobs: what can't be seen by clicking. A job resumes after its worker dies, a dead worker's lease
expires, review choices persist and reach the director, and the API key never goes back to the browser."""

import json
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from goodfeeling.jobs import queue
from goodfeeling.pipeline.run import RunOptions, run
from goodfeeling.schemas.edl import EDL
from goodfeeling.server.app import create_app
from goodfeeling.settings import Settings
from goodfeeling.store.db import Database


@pytest.fixture(scope="module")
def config(tmp_path_factory):
    path = tmp_path_factory.mktemp("config") / "settings.json"
    path.write_text(json.dumps({"render": {"x264_preset": "ultrafast"}}))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GOODFEELING_CONFIG", str(path))
        patch.delenv("ANTHROPIC_API_KEY", raising=False)
        yield path


@pytest.fixture(scope="module")
def root(tmp_path_factory):
    """One data root for the module, so later tests reuse the proxies and signals of earlier ones."""
    return tmp_path_factory.mktemp("data")


def test_expired_lease_is_taken_over(tmp_path):
    db = Database(tmp_path / "app.db")
    job_id = queue.enqueue(db, "run", {}, project_id=None, edit=None)
    assert queue.claim(db, "other-host:1")["id"] == job_id
    assert queue.claim(db, "me:2") is None
    db.execute("UPDATE jobs SET lease_expires=? WHERE id=?", (time.time() - 1, job_id))
    taken = queue.claim(db, "me:2")
    assert taken["id"] == job_id and taken["attempts"] == 2
    assert not queue.renew(db, job_id, "other-host:1")
    assert queue.renew(db, job_id, "me:2")


def test_review_choices_persist_and_reach_the_director(media, root, config):
    settings = Settings()
    options = RunOptions(
        footage=media["footage"], music=media["music"], length=8, no_ai=True, data_root=root, render=False
    )
    first = run(options, settings, progress=lambda _: None)
    plan = json.loads(first.outputs["plan_json"].read_text())["plan"]
    used = [s["segment_id"] for s in plan["shots"]]
    segments = {s.id: s for s in first.segments}
    spare = sorted(
        (s for s in first.segments if s.id not in used and s.kind == "flight" and s.frames >= 4 * 60),
        key=lambda s: s.final_score,
    )
    assert spare, "fixture needs an unused segment to star"
    excluded, starred = used[0], spare[0].id

    client = TestClient(create_app(root), base_url="http://127.0.0.1", headers={"X-Goodfeeling": "1"})
    url = "/api/projects/footage/segments"
    assert client.put(f"{url}/{excluded}/choice", json={}, headers={"X-Goodfeeling": ""}).status_code == 403
    assert client.get("/api/projects", headers={"Host": "evil.example"}).status_code == 400
    assert client.put(f"{url}/{excluded}/choice", json={"include": False}).json()["include"] is False
    assert client.put(f"{url}/{starred}/choice", json={"star": True}).json()["star"] is True
    assert client.put(f"{url}/{used[-1]}/choice", json={"taste": -1}).json()["taste"] == -1
    rows = {s["id"]: s for s in client.get(url).json()}
    assert rows[excluded]["choice"]["include"] is False and not rows[excluded]["available"]
    assert rows[starred]["choice"]["star"] is True
    kinds = [
        (r["seg_id"], r["kind"], r["value"])
        for r in Database(root / "app.db").execute("SELECT seg_id, kind, value FROM feedback ORDER BY id")
    ]
    assert kinds == [(excluded, "include", 0), (starred, "star", 1), (used[-1], "dislike", 1)]

    second = run(options, settings, progress=lambda _: None)
    replanned = [s["segment_id"] for s in json.loads(second.outputs["plan_json"].read_text())["plan"]["shots"]]
    assert excluded not in replanned
    assert starred in replanned
    gone = segments[excluded]
    edl = EDL.load(second.outputs["edl"])
    for shot in edl.shots:
        if shot.clip == gone.clip:
            assert shot.out <= gone.start or shot.in_ >= gone.end, "an excluded segment's footage was used"


def test_job_of_a_dead_worker_resumes_on_restart(media, root, config):
    db = Database(root / "app.db")
    options = RunOptions(
        footage=media["footage"], music=media["music"], length=8, no_ai=True, data_root=root, render=False
    )
    job_id = queue.enqueue(db, "run", {"options": options.to_json()}, project_id=None, edit="default")
    dead = subprocess.Popen(["true"])
    dead.wait()
    import socket

    assert queue.claim(db, f"{socket.gethostname()}:{dead.pid}")["id"] == job_id
    queue.add_event(db, job_id, "Ingest")
    worker = subprocess.Popen(
        [sys.executable, "-m", "goodfeeling.jobs.worker", "--data-root", str(root)],
        env={**os.environ, "GOODFEELING_DATA_ROOT": str(root)},
    )
    try:
        deadline = time.time() + 240
        while queue.get(db, job_id)["status"] not in queue.FINISHED and time.time() < deadline:
            time.sleep(0.5)
    finally:
        worker.terminate()
        worker.wait(10)
    job = queue.get(db, job_id)
    assert job["status"] == "done", job["error"]
    assert job["attempts"] == 2
    messages = [e["message"] for e in queue.events_after(db, job_id, 0, limit=1000)]
    assert any("resuming" in m for m in messages)
    assert "Resumed from the last checkpoint" in messages
    result = json.loads(job["result_json"])
    assert (root / "projects" / "footage" / result["outputs"]["edl"]).exists()


def test_api_key_is_never_returned_in_full(tmp_path, config):
    config.write_text("{}")
    client = TestClient(create_app(tmp_path / "data"), base_url="http://127.0.0.1", headers={"X-Goodfeeling": "1"})
    key = "sk-ant-test-0123456789abcdef"
    response = client.put("/api/settings", json={"api_key": key, "pace": "slower"})
    assert response.status_code == 200
    assert key not in response.text and key not in client.get("/api/settings").text
    assert response.json()["api_key"] == {"set": True, "source": "settings", "hint": "…cdef"}
    assert json.loads(config.read_text())["anthropic_api_key"] == key
    assert oct(config.stat().st_mode & 0o777) == "0o600"
