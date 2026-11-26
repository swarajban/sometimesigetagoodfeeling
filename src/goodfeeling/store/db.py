"""SQLite store: WAL mode, busy timeout, numbered migrations."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    """
    CREATE TABLE projects (
        id INTEGER PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        footage_dir TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE clips (
        project_id INTEGER NOT NULL REFERENCES projects(id),
        clip_id TEXT NOT NULL,
        path TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        info_json TEXT NOT NULL,
        flight_json TEXT NOT NULL,
        proxy_path TEXT,
        PRIMARY KEY (project_id, clip_id)
    );
    CREATE TABLE segments (
        project_id INTEGER NOT NULL REFERENCES projects(id),
        seg_id TEXT NOT NULL,
        clip_id TEXT NOT NULL,
        segment_json TEXT NOT NULL,
        PRIMARY KEY (project_id, seg_id)
    );
    CREATE TABLE score_cache (
        key TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        in_frame INTEGER NOT NULL,
        out_frame INTEGER NOT NULL,
        frame_params TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        result_json TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE ai_log (
        id INTEGER PRIMARY KEY,
        project_id INTEGER REFERENCES projects(id),
        step TEXT NOT NULL,
        model TEXT NOT NULL,
        request_json TEXT NOT NULL,
        response_json TEXT NOT NULL,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    );
    CREATE TABLE edl_revisions (
        project_id INTEGER NOT NULL REFERENCES projects(id),
        revision INTEGER NOT NULL,
        path TEXT NOT NULL,
        origin TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (project_id, revision)
    );
    CREATE TABLE project_state (
        project_id INTEGER PRIMARY KEY REFERENCES projects(id),
        current_revision INTEGER
    );
    """,
]


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for index, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.execute("BEGIN")
            try:
                for statement in filter(str.strip, sql.split(";")):
                    self.conn.execute(statement)
                self.conn.execute(f"PRAGMA user_version={index}")
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        self.conn.close()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def project_id(self, slug: str, name: str, footage_dir: str) -> int:
        now = time.time()
        row = self.execute("SELECT id, footage_dir FROM projects WHERE slug=?", (slug,)).fetchone()
        if row:
            if row["footage_dir"] != footage_dir:
                raise ValueError(f"project '{slug}' already belongs to {row['footage_dir']}; pass --project")
            self.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, row["id"]))
            return int(row["id"])
        cur = self.execute(
            "INSERT INTO projects (slug, name, footage_dir, created_at, updated_at) VALUES (?,?,?,?,?)",
            (slug, name, footage_dir, now, now),
        )
        return int(cur.lastrowid)

    def cached_score(self, key: str) -> dict[str, Any] | None:
        row = self.execute("SELECT result_json FROM score_cache WHERE key=?", (key,)).fetchone()
        return json.loads(row["result_json"]) if row else None

    def store_score(
        self,
        key: str,
        *,
        fingerprint: str,
        in_frame: int,
        out_frame: int,
        frame_params: str,
        model: str,
        prompt_version: str,
        result: dict[str, Any],
    ) -> None:
        self.execute(
            "INSERT OR REPLACE INTO score_cache (key, fingerprint, in_frame, out_frame, frame_params, model, "
            "prompt_version, result_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                key,
                fingerprint,
                in_frame,
                out_frame,
                frame_params,
                model,
                prompt_version,
                json.dumps(result),
                time.time(),
            ),
        )

    def project_cost(self, project_id: int) -> float:
        row = self.execute("SELECT COALESCE(SUM(cost_usd),0) AS c FROM ai_log WHERE project_id=?", (project_id,))
        return float(row.fetchone()["c"])
