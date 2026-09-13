"""Durable job state in SQLite (WAL). Shared by the API and the worker container through the /data volume."""
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'queued',
  progress REAL NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  request_json TEXT NOT NULL,
  settings_json TEXT NOT NULL DEFAULT '{}',
  warnings_json TEXT NOT NULL DEFAULT '[]',
  timings_json TEXT NOT NULL DEFAULT '{}',
  error_json TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  attempt INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  ts REAL NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    return con


class JobStore:
    def __init__(self, path: Path | None = None):
        self.con = connect(path)

    @contextmanager
    def tx(self):
        with _LOCK:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                yield self.con
                self.con.execute("COMMIT")
            except Exception:
                self.con.execute("ROLLBACK")
                raise

    # ---- creation / lookup -------------------------------------------------
    def create(self, request: dict, settings: dict) -> str:
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO jobs(id,status,stage,created_at,updated_at,request_json,settings_json) VALUES(?,?,?,?,?,?,?)",
                (job_id, "queued", "queued", now, now, json.dumps(request), json.dumps(settings)))
        self.event(job_id, "info", "job queued")
        return job_id

    def get(self, job_id: str) -> dict | None:
        row = self.con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(row) if row else None

    def list_jobs(self, limit: int = 50, status: str | None = None) -> "list[dict]":
        if status:
            rows = self.con.execute("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = self.con.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        for k in ("request_json", "settings_json", "warnings_json", "timings_json", "error_json"):
            v = d.pop(k)
            d[k[:-5]] = json.loads(v) if v else ({} if k != "warnings_json" else [])
        d["cancel_requested"] = bool(d["cancel_requested"])
        return d

    # ---- worker side -------------------------------------------------------
    def claim_next(self, worker_id: str) -> dict | None:
        with self.tx() as c:
            row = c.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return None
            now = time.time()
            c.execute("UPDATE jobs SET status='running', started_at=COALESCE(started_at,?), updated_at=?, worker_id=?, "
                      "attempt=attempt+1 WHERE id=?", (now, now, worker_id, row["id"]))
        return self.get(row["id"])

    def requeue_orphans(self, worker_id: str) -> list[str]:
        """On worker start: jobs left 'running' by a crashed/restarted worker go back to the queue."""
        with self.tx() as c:
            rows = c.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
            ids = [r["id"] for r in rows]
            for i in ids:
                c.execute("UPDATE jobs SET status='queued', updated_at=? WHERE id=?", (time.time(), i))
        for i in ids:
            self.event(i, "warn", f"requeued after worker restart ({worker_id}); completed stages will be reused")
        return ids

    def update(self, job_id: str, **fields):
        cols, vals = [], []
        for k, v in fields.items():
            if k in ("settings", "warnings", "timings", "error"):
                k = k + "_json"
                v = json.dumps(v)
            cols.append(f"{k}=?")
            vals.append(v)
        cols.append("updated_at=?")
        vals.append(time.time())
        vals.append(job_id)
        with self.tx() as c:
            c.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", vals)

    def request_cancel(self, job_id: str) -> str | None:
        with self.tx() as c:
            row = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            if row["status"] == "queued":
                c.execute("UPDATE jobs SET status='cancelled', stage='cancelled', finished_at=?, updated_at=?, "
                          "cancel_requested=1 WHERE id=?", (time.time(), time.time(), job_id))
                return "cancelled"
            if row["status"] == "running":
                c.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?", (time.time(), job_id))
                return "cancelling"
            return row["status"]

    def retry(self, job_id: str, allow_completed: bool = False, settings: dict | None = None) -> str | None:
        """Requeue a failed/cancelled job (or a completed one when reprocessing from a stage). Completed stages
        (stages/<name>/result.json) are reused by the pipeline unless the caller removed them."""
        with self.tx() as c:
            row = c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            allowed = ("failed", "cancelled") + (("completed",) if allow_completed else ())
            if row["status"] not in allowed:
                return row["status"]
            if settings is not None:
                c.execute("UPDATE jobs SET settings_json=? WHERE id=?", (json.dumps(settings), job_id))
            c.execute("UPDATE jobs SET status='queued', stage='queued', cancel_requested=0, error_json=NULL, finished_at=NULL, "
                      "updated_at=? WHERE id=?", (time.time(), job_id))
        self.event(job_id, "info", "retry requested; completed stages will be reused")
        return "queued"

    def is_cancel_requested(self, job_id: str) -> bool:
        row = self.con.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    # ---- events ------------------------------------------------------------
    def event(self, job_id: str, level: str, message: str):
        with self.tx() as c:
            c.execute("INSERT INTO events(job_id,ts,level,message) VALUES(?,?,?,?)", (job_id, time.time(), level, message))

    def events(self, job_id: str, limit: int = 200) -> list[dict]:
        rows = self.con.execute("SELECT ts,level,message FROM events WHERE job_id=? ORDER BY id DESC LIMIT ?",
                                (job_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]
