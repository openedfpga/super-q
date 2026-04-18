"""Backend interface.

A backend's job is simple: given a `TaskSpec`, return a `TaskOutcome`.
Backends are allowed to block for a long time (local compiles take
minutes; AWS cold-starts take even longer). Concurrency control is the
scheduler's problem, not the backend's.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from super_q.project import PocketCore
from super_q.timing import TimingReport


class BackendError(Exception):
    pass


@dataclass
class TaskSpec:
    core: PocketCore
    seed: int
    job_id: str
    task_id: str
    work_dir: Path
    mode: str = "full"             # 'full' | 'split-fit'
    qdb_checkpoint: Path | None = None
    threads: int = 2
    timeout_s: int = 60 * 60
    extra_assignments: dict[str, str] | None = None
    # Shared flag the scheduler sets to abort in-flight work on early-exit.
    cancel_event: threading.Event | None = None


@dataclass
class TaskOutcome:
    ok: bool
    seed: int
    rbf_path: Path | None
    rbf_r_path: Path | None
    timing: TimingReport | None
    log_path: Path | None
    error: str | None
    duration_s: float


class Backend(Protocol):
    """Abstract compute target."""

    name: str

    def available_slots(self) -> int:
        """How many tasks this backend can run concurrently right now."""

    def describe(self) -> dict:
        """Agent-facing description: kind, capacity, host info."""

    def run(self, spec: TaskSpec) -> TaskOutcome:
        """Execute a single task to completion. Must be thread-safe."""
