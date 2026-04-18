"""Human-readable progress UI built on rich.

Exists purely so CLI output looks good; nothing else in the system
depends on it. The scheduler emits plain dict events — we turn those
into a live table for humans and a flat JSON stream for agents.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text


@dataclass
class SeedState:
    seed: int
    status: str = "queued"
    slack_ns: float | None = None
    fmax_mhz: float | None = None
    duration_s: float = 0.0
    error: str | None = None


class RichProgress:
    """Live table of seed statuses. Safe to call from scheduler threads."""

    def __init__(self, total: int, *, title: str = "seed sweep") -> None:
        self.console = Console()
        self.title = title
        self.total = total
        self._states: dict[int, SeedState] = {}
        self._lock = threading.Lock()
        self._live: Live | None = None
        self._start_ts = time.time()

    def __enter__(self) -> "RichProgress":
        self._live = Live(self._render(), console=self.console, refresh_per_second=6)
        self._live.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._live:
            self._live.update(self._render())
            self._live.__exit__(exc_type, exc, tb)

    def handle_event(self, kind: str, payload: dict[str, Any]) -> None:
        seed = payload.get("seed")
        if seed is None:
            return
        with self._lock:
            st = self._states.setdefault(seed, SeedState(seed=seed))
            if kind == "seed.started":
                st.status = "running"
            elif kind == "seed.finished":
                st.status = "passed" if payload.get("ok") else "failed"
                st.slack_ns = payload.get("slack")
                st.fmax_mhz = payload.get("fmax")
                st.duration_s = payload.get("duration_s", 0.0)
                st.error = payload.get("error")
        if self._live:
            self._live.update(self._render())

    def _render(self) -> Table:
        t = Table(title=f"{self.title} — elapsed {self._fmt_dur(time.time()-self._start_ts)}",
                  show_lines=False, expand=True)
        t.add_column("seed", justify="right", style="cyan", width=8)
        t.add_column("status", width=9)
        t.add_column("slack (ns)", justify="right", width=14)
        t.add_column("Fmax (MHz)", justify="right", width=12)
        t.add_column("time", justify="right", width=10)
        t.add_column("detail", overflow="fold")

        with self._lock:
            states = sorted(self._states.values(), key=lambda s: s.seed)
        if not states:
            t.add_row("—", "pending", "—", "—", "—", "waiting for dispatch")
            return t
        for st in states:
            color = {
                "queued":  "white",
                "running": "yellow",
                "passed":  "bold green",
                "failed":  "red",
                "cancelled": "dim",
            }.get(st.status, "white")
            t.add_row(
                str(st.seed),
                Text(st.status, style=color),
                f"{st.slack_ns:+.3f}" if st.slack_ns is not None else "—",
                f"{st.fmax_mhz:.2f}" if st.fmax_mhz is not None else "—",
                self._fmt_dur(st.duration_s) if st.duration_s else "—",
                st.error or "",
            )

        # Summary row
        passed = sum(1 for s in states if s.status == "passed")
        running = sum(1 for s in states if s.status == "running")
        failed = sum(1 for s in states if s.status == "failed")
        t.caption = f"{passed} passed · {failed} failed · {running} running · {self.total} total"
        return t

    @staticmethod
    def _fmt_dur(s: float) -> str:
        if s < 60:
            return f"{s:.0f}s"
        m, sec = divmod(int(s), 60)
        if m < 60:
            return f"{m}m{sec:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"


class JsonProgress:
    """Newline-delimited JSON event stream. Written to `out` (default stdout)."""

    def __init__(self, *, out=None) -> None:
        self._out = out or sys.stdout
        self._lock = threading.Lock()

    def __enter__(self) -> "JsonProgress":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass

    def handle_event(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._out.write(json.dumps({"kind": kind, **payload}, default=str) + "\n")
            self._out.flush()


def make_progress(total: int, *, json_mode: bool, title: str) -> tuple[Any, Callable]:
    """Return (context-manager, event-handler) tuple for the scheduler.

    Agents passing --json get newline-delimited events; humans get a live
    rich table. Either way the handler is the same shape.
    """
    if json_mode:
        p = JsonProgress()
        return p, p.handle_event
    p = RichProgress(total=total, title=title)
    return p, p.handle_event
