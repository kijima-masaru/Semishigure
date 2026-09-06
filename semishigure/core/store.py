"""SQLite run store: one row per run plus samples, calls and events."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from semishigure.secrets import DEFAULT_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, scenario_name TEXT, scenario_yaml TEXT, pbx TEXT,
  started_at REAL, finished_at REAL, summary TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS samples (run_id INTEGER, t REAL, data TEXT);
CREATE TABLE IF NOT EXISTS calls (run_id INTEGER, call_id TEXT, role TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS events (run_id INTEGER, t REAL, kind TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS samples_run ON samples(run_id);
CREATE INDEX IF NOT EXISTS calls_run ON calls(run_id);
"""


class RunStore:
    def __init__(self, path: Path | None = None):
        self.path = path or (DEFAULT_DIR / "runs.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def create_run(self, name: str, scenario_name: str, scenario_yaml: str, pbx: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (name, scenario_name, scenario_yaml, pbx, started_at) VALUES (?, ?, ?, ?, ?)",
            (name, scenario_name, scenario_yaml, json.dumps(pbx), time.time()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def add_samples(self, run_id: int, samples: list[dict]) -> None:
        if not samples:
            return
        self.db.executemany("INSERT INTO samples (run_id, t, data) VALUES (?, ?, ?)", [(run_id, s["t"], json.dumps(s)) for s in samples])
        self.db.commit()

    def add_calls(self, run_id: int, calls: list[dict]) -> None:
        if not calls:
            return
        self.db.executemany("INSERT INTO calls (run_id, call_id, role, data) VALUES (?, ?, ?, ?)", [(run_id, c["id"], c["role"], json.dumps(c)) for c in calls])
        self.db.commit()

    def add_events(self, run_id: int, events: list[dict]) -> None:
        if not events:
            return
        self.db.executemany("INSERT INTO events (run_id, t, kind, data) VALUES (?, ?, ?, ?)", [(run_id, e["t"], e["kind"], json.dumps(e)) for e in events])
        self.db.commit()

    def finish_run(self, run_id: int, summary: dict) -> None:
        self.db.execute("UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?", (time.time(), json.dumps(summary), run_id))
        self.db.commit()

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT id, name, scenario_name, pbx, started_at, finished_at, summary FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            {"id": r[0], "name": r[1], "scenario_name": r[2], "pbx": json.loads(r[3] or "{}"), "started_at": r[4], "finished_at": r[5], "summary": json.loads(r[6]) if r[6] else None}
            for r in rows
        ]

    def get_run(self, run_id: int) -> dict | None:
        r = self.db.execute("SELECT id, name, scenario_name, scenario_yaml, pbx, started_at, finished_at, summary, notes FROM runs WHERE id = ?", (run_id,)).fetchone()
        if r is None:
            return None
        samples = [json.loads(s[0]) for s in self.db.execute("SELECT data FROM samples WHERE run_id = ? ORDER BY t", (run_id,))]
        calls = [json.loads(c[0]) for c in self.db.execute("SELECT data FROM calls WHERE run_id = ?", (run_id,))]
        events = [json.loads(e[0]) for e in self.db.execute("SELECT data FROM events WHERE run_id = ? ORDER BY t", (run_id,))]
        return {
            "id": r[0], "name": r[1], "scenario_name": r[2], "scenario_yaml": r[3], "pbx": json.loads(r[4] or "{}"),
            "started_at": r[5], "finished_at": r[6], "summary": json.loads(r[7]) if r[7] else None, "notes": r[8],
            "samples": samples, "calls": calls, "events": events,
        }
