"""SQLite state store.

The DB is the source of truth for every job, seed task, worker heartbeat
and timing result. Kept simple on purpose — readers and writers poll; there
are no RPC servers to keep alive between sessions.

Schema notes:
  - `jobs` is one row per high-level user request (e.g. a seed sweep).
  - `tasks` is one row per seed attempt. The scheduler inserts these up-front
    so `status` can show a live plan.
  - `workers` tracks per-machine capacity with a heartbeat; the scheduler
    considers a worker dead if its heartbeat is stale.
  - `events` is an append-only log for watch/tail features.

WAL mode lets watchers read while the scheduler is writing.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    core_path TEXT NOT NULL,
    core_name TEXT NOT NULL,
    kind TEXT NOT NULL,            -- 'build' | 'sweep' | 'explore'
    status TEXT NOT NULL,          -- 'queued' | 'running' | 'passed' | 'failed' | 'cancelled'
    created_at REAL NOT NULL,
    started_at REAL,
    ended_at REAL,
    spec_json TEXT NOT NULL,
    best_seed INTEGER,
    best_slack_ns REAL,
    best_fmax_mhz REAL,
    artifact_path TEXT,
    message TEXT
);

CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS ix_jobs_core ON jobs(core_path);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seed INTEGER NOT NULL,
    status TEXT NOT NULL,          -- 'queued' | 'claimed' | 'running' | 'passed' | 'failed' | 'cancelled'
    worker_id TEXT,
    backend TEXT NOT NULL,         -- 'local' | 'docker' | 'aws'
    claimed_at REAL,
    started_at REAL,
    ended_at REAL,
    slack_ns REAL,
    fmax_mhz REAL,
    timing_json TEXT,
    rbf_path TEXT,
    log_path TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS ix_tasks_job ON tasks(job_id);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    host TEXT NOT NULL,
    backend TEXT NOT NULL,
    slots INTEGER NOT NULL,
    started_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL,
    info_json TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    job_id TEXT,
    task_id TEXT,
    kind TEXT NOT NULL,
    payload TEXT
);

CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_job ON events(job_id);
"""


class Store:
    """Thread-safe SQLite handle. One store per process is fine."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            path,
            isolation_level=None,  # autocommit; we wrap writes in txns explicitly
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            # WAL lets concurrent readers (status, watch) coexist with the scheduler writer.
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.executescript(SCHEMA)

    # ----- txn helpers ---------------------------------------------------

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            yield self._conn
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise

    def close(self) -> None:
        self._conn.close()

    # ----- jobs ----------------------------------------------------------

    def create_job(
        self,
        *,
        core_path: str,
        core_name: str,
        kind: str,
        spec: dict[str, Any],
    ) -> str:
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO jobs(id,core_path,core_name,kind,status,created_at,spec_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (jid, core_path, core_name, kind, "queued", now, json.dumps(spec)),
            )
        self.record_event(jid, None, "job.created", spec)
        return jid

    def start_job(self, job_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                (time.time(), job_id),
            )
        self.record_event(job_id, None, "job.started", None)

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        best_seed: int | None = None,
        best_slack_ns: float | None = None,
        best_fmax_mhz: float | None = None,
        artifact_path: str | None = None,
        message: str | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE jobs SET status=?, ended_at=?, best_seed=?, best_slack_ns=?, "
                "best_fmax_mhz=?, artifact_path=?, message=? WHERE id=?",
                (
                    status,
                    time.time(),
                    best_seed,
                    best_slack_ns,
                    best_fmax_mhz,
                    artifact_path,
                    message,
                    job_id,
                ),
            )
        self.record_event(
            job_id,
            None,
            "job.finished",
            {
                "status": status,
                "best_seed": best_seed,
                "best_slack_ns": best_slack_ns,
                "best_fmax_mhz": best_fmax_mhz,
            },
        )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ----- tasks ---------------------------------------------------------

    def create_task(self, *, job_id: str, seed: int, backend: str) -> str:
        tid = uuid.uuid4().hex[:12]
        with self.tx() as c:
            c.execute(
                "INSERT INTO tasks(id,job_id,seed,status,backend) VALUES(?,?,?,?,?)",
                (tid, job_id, seed, "queued", backend),
            )
        return tid

    def claim_task(self, task_id: str, worker_id: str) -> bool:
        """Atomically move queued→claimed. Returns True if we got it."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE tasks SET status='claimed', worker_id=?, claimed_at=? "
                "WHERE id=? AND status='queued'",
                (worker_id, time.time(), task_id),
            )
            return cur.rowcount == 1

    def start_task(self, task_id: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET status='running', started_at=? WHERE id=?",
                (time.time(), task_id),
            )

    def finish_task(
        self,
        task_id: str,
        *,
        status: str,
        slack_ns: float | None = None,
        fmax_mhz: float | None = None,
        timing: dict[str, Any] | None = None,
        rbf_path: str | None = None,
        log_path: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET status=?, ended_at=?, slack_ns=?, fmax_mhz=?, "
                "timing_json=?, rbf_path=?, log_path=?, error=? WHERE id=?",
                (
                    status,
                    time.time(),
                    slack_ns,
                    fmax_mhz,
                    json.dumps(timing) if timing else None,
                    rbf_path,
                    log_path,
                    error,
                    task_id,
                ),
            )

    def list_tasks(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE job_id=? ORDER BY seed ASC", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def next_queued_task(self, backend: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE status='queued' AND backend=? "
            "ORDER BY claimed_at NULLS FIRST, id ASC LIMIT 1",
            (backend,),
        ).fetchone()
        return dict(row) if row else None

    def cancel_job(self, job_id: str) -> int:
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET status='cancelled' "
                "WHERE job_id=? AND status IN ('queued','claimed','running')",
                (job_id,),
            )
            cur = c.execute(
                "UPDATE jobs SET status='cancelled', ended_at=? "
                "WHERE id=? AND status IN ('queued','running')",
                (time.time(), job_id),
            )
            return cur.rowcount

    # ----- workers -------------------------------------------------------

    def register_worker(
        self, worker_id: str, *, host: str, backend: str, slots: int, info: dict[str, Any]
    ) -> None:
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO workers(id,host,backend,slots,started_at,heartbeat_at,info_json) "
                "VALUES(?,?,?,?,?,?,?)",
                (worker_id, host, backend, slots, now, now, json.dumps(info)),
            )

    def heartbeat(self, worker_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE workers SET heartbeat_at=? WHERE id=?", (time.time(), worker_id))

    def live_workers(self, *, max_stale_s: float = 30.0) -> list[dict[str, Any]]:
        cutoff = time.time() - max_stale_s
        rows = self._conn.execute(
            "SELECT * FROM workers WHERE heartbeat_at > ? ORDER BY started_at", (cutoff,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ----- events --------------------------------------------------------

    def record_event(
        self,
        job_id: str | None,
        task_id: str | None,
        kind: str,
        payload: Any,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events(ts,job_id,task_id,kind,payload) VALUES(?,?,?,?,?)",
                (time.time(), job_id, task_id, kind, json.dumps(payload) if payload else None),
            )

    def tail_events(self, *, since: float = 0.0, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE ts > ? ORDER BY ts ASC LIMIT ?", (since, limit)
        ).fetchall()
        return [dict(r) for r in rows]
