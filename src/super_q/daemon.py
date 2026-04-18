"""Local dispatcher daemon.

Purpose: keep the Python + Quartus startup taxes paid *once* per session,
so agents can fire requests in under a second. The daemon doesn't do
compilation itself — it dispatches to whatever pool the user configured
(remote Modal/Fly/SSH/GHA, or local). For the `local` pool it also
maintains a warm-shell pool so incremental rebuilds skip startup.

Transport: a Unix domain socket at `~/.superq/superq.sock` speaks
newline-delimited JSON. Every request has an `op` string; the responses
include a matching `reply_to`. TCP is avoided on purpose — the daemon is
meant to be personal, not network-accessible.

Requests:
    { "op": "ping" }
    { "op": "info" }
    { "op": "sweep",   "path": ..., "min_seed": 1, "max_seed": 16, ... }
    { "op": "explore", "path": ..., "budget_s": 1800, ... }
    { "op": "build",   "path": ..., "seed": 1, "incremental": true }
    { "op": "cancel",  "job_id": ... }
    { "op": "status",  "job_id": ... }
    { "op": "shutdown" }

The CLI's `superq daemon start/ping/status/stop` is the user-facing side;
everything programmatic goes through a single helper client below.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import socketserver
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from super_q.backends.base import Backend
from super_q.config import paths
from super_q.db import Store
from super_q.explore import explore
from super_q.incremental import IncrementalBuilder
from super_q.pool_config import resolve_backend
from super_q.project import detect_core
from super_q.scheduler import Scheduler
from super_q.seeds import SeedPlan

log = logging.getLogger("superq.daemon")


def socket_path() -> Path:
    return paths().state_dir / "superq.sock"


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------


class DaemonState:
    """Shared across request handlers."""

    def __init__(self, *, pool_name: str | None, parallel: int = 4) -> None:
        self.store = Store(paths().db_path)
        self.pool_name = pool_name
        self.backend: Backend = resolve_backend(pool_name)
        self.incremental = IncrementalBuilder()
        self.pool = ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="superq-daemon")
        self.shutdown_event = threading.Event()
        self.start_ts = time.time()


class _Handler(socketserver.BaseRequestHandler):
    state: DaemonState  # injected by the server below

    def handle(self) -> None:
        sock: socket.socket = self.request
        f = sock.makefile("rwb", buffering=0)
        try:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    req = json.loads(raw)
                except json.JSONDecodeError as e:
                    self._reply(f, {"ok": False, "error": f"bad json: {e}"})
                    continue
                self._dispatch(f, req)
        except (BrokenPipeError, ConnectionResetError):
            return

    # ------------------------------------------------------------------ #

    def _dispatch(self, f, req: dict) -> None:
        op = req.get("op")
        rid = req.get("id")

        try:
            if op == "ping":
                self._reply(f, {"ok": True, "pong": True, "id": rid})
            elif op == "info":
                self._reply(f, {
                    "ok": True, "id": rid,
                    "backend": self.state.backend.describe(),
                    "pool": self.state.pool_name,
                    "uptime_s": time.time() - self.state.start_ts,
                    "db": str(self.state.store.path),
                })
            elif op == "sweep":
                self._reply(f, self._sweep(req))
            elif op == "explore":
                self._reply(f, self._explore(req))
            elif op == "build":
                self._reply(f, self._build(req))
            elif op == "status":
                self._reply(f, self._status(req))
            elif op == "cancel":
                self._reply(f, {
                    "ok": True, "id": rid,
                    "cancelled": self.state.store.cancel_job(req["job_id"]),
                })
            elif op == "shutdown":
                self._reply(f, {"ok": True, "bye": True, "id": rid})
                self.state.shutdown_event.set()
            else:
                self._reply(f, {"ok": False, "error": f"unknown op: {op}", "id": rid})
        except Exception as e:
            log.exception("op %s failed", op)
            self._reply(f, {"ok": False, "error": str(e), "id": rid})

    def _reply(self, f, payload: dict) -> None:
        f.write((json.dumps(payload, default=str) + "\n").encode())
        f.flush()

    # ------ op implementations ---------------------------------------

    def _sweep(self, req: dict) -> dict:
        core = detect_core(req["path"])
        plan = SeedPlan.range(
            start=int(req.get("min_seed", 1)),
            end=int(req.get("max_seed", 16)),
            max_parallel=int(req.get("parallel", 4)),
            stop_on_first_pass=bool(req.get("stop_on_pass", True)),
        )
        sched = Scheduler(self.state.store, self.state.backend)
        outcome = sched.run_sweep(core, plan,
                                   mode=req.get("mode", "full"),
                                   threads_per_task=int(req.get("threads", 2)))
        return {"ok": True, "id": req.get("id"), "outcome": outcome.as_dict()}

    def _explore(self, req: dict) -> dict:
        core = detect_core(req["path"])
        outcome = explore(
            self.state.store, self.state.backend, core,
            budget_s=int(req.get("budget_s", 30 * 60)),
            parallel=int(req.get("parallel", 4)),
        )
        return {"ok": True, "id": req.get("id"), "outcome": outcome.as_dict()}

    def _build(self, req: dict) -> dict:
        core = detect_core(req["path"])
        seed = int(req.get("seed", 1))
        if req.get("incremental"):
            result = self.state.incremental.run(
                core, job_id=f"daemon-{int(time.time())}", seed=seed,
                use_warm_shell=bool(req.get("use_warm_shell", True)),
            )
            return {"ok": result.ok, "id": req.get("id"), "result": {
                "seed": seed,
                "ok": result.ok,
                "duration_s": result.duration_s,
                "reused_warm_shell": result.reused_warm_shell,
                "slack_ns": result.timing.worst_setup_slack_ns if result.timing else None,
                "fmax_mhz": result.timing.worst_fmax_mhz if result.timing else None,
                "rbf_r_path": str(result.rbf_r_path) if result.rbf_r_path else None,
                "error": result.error,
            }}
        # Non-incremental single seed → just call sweep with one seed.
        plan = SeedPlan(seeds=[seed], stop_on_first_pass=True, max_parallel=1)
        sched = Scheduler(self.state.store, self.state.backend)
        out = sched.run_sweep(core, plan)
        return {"ok": out.best is not None, "id": req.get("id"),
                "outcome": out.as_dict()}

    def _status(self, req: dict) -> dict:
        jid = req["job_id"]
        job = self.state.store.get_job(jid)
        tasks = self.state.store.list_tasks(jid) if job else []
        return {"ok": job is not None, "id": req.get("id"), "job": job, "tasks": tasks}


class _ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(*, pool_name: str | None = None, parallel: int = 4) -> None:
    path = socket_path()
    if path.exists():
        # Stale socket? Try a ping; if nothing answers, clobber.
        try:
            with Client(path) as c:
                if c.ping():
                    raise RuntimeError(f"daemon already running at {path}")
        except Exception:
            pass
        path.unlink(missing_ok=True)

    state = DaemonState(pool_name=pool_name, parallel=parallel)

    def _handler_factory(*a, **kw):
        h = _Handler(*a, **kw)
        return h
    _Handler.state = state  # type: ignore[attr-defined]

    server = _ThreadedUnixServer(str(path), _handler_factory)
    os.chmod(path, 0o600)
    log.info("daemon listening on %s (pool=%s)", path, pool_name or "default")

    def _stop(signum, frame):  # noqa: ARG001
        log.info("signal %s: shutting down", signum)
        state.shutdown_event.set()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        state.shutdown_event.wait()
    finally:
        server.shutdown()
        server.server_close()
        path.unlink(missing_ok=True)
        state.incremental.shutdown()
        state.pool.shutdown(wait=False)


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------


class Client:
    """Blocking RPC client — one request/response per call."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or socket_path()
        self._sock: socket.socket | None = None

    def __enter__(self) -> "Client":
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(str(self._path))
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._sock = None

    def call(self, op: str, **kw) -> dict[str, Any]:
        assert self._sock is not None, "use as context manager"
        payload = {"op": op, **kw}
        self._sock.sendall((json.dumps(payload) + "\n").encode())
        f = self._sock.makefile("rb")
        line = f.readline()
        if not line:
            raise RuntimeError("daemon closed connection")
        return json.loads(line)

    def ping(self) -> bool:
        try:
            r = self.call("ping")
            return bool(r.get("ok"))
        except Exception:
            return False


def is_running() -> bool:
    path = socket_path()
    if not path.exists():
        return False
    try:
        with Client(path) as c:
            return c.ping()
    except Exception:
        return False
