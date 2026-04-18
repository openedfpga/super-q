"""Filesystem watcher → incremental rebuild loop.

Designed to feel like `cargo watch` for Pocket cores: you save a file,
super-q kicks off an incremental compile, you see timing + a fresh
`.rbf_r` ~60 seconds later.

Design decisions:
  * Uses `watchfiles` (Rust-backed, notify-based). We only watch the
    core's source directories, not the entire repo — anything in
    `.superq/` or `output_files/` would trivially loop.
  * Debounces 500 ms after the last change. Saving several files at once
    (git checkout, formatter run) results in one build.
  * Coalesces: if a change arrives while a build is running, we mark a
    pending rebuild and fire it as soon as the current one finishes.
    We never run two incremental builds in parallel — that would race
    on `db/` and tank cache hits.
  * Auto-falls back from warm-shell to cold if Quartus isn't local —
    remote backends don't benefit from warm shells anyway.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from super_q.config import quartus_install
from super_q.incremental import IncrementalBuilder, IncrementalResult
from super_q.project import PocketCore

log = logging.getLogger("superq.watch")


# Files we want to trigger a rebuild. Verilog/SV/VHDL + Quartus config +
# SDC constraints. Ignore anything that's output of our pipeline.
_WATCHED_SUFFIXES = {
    ".v", ".sv", ".svh", ".vh", ".vhd", ".vhdl",
    ".qsf", ".qpf", ".qip", ".ip",
    ".sdc", ".tcl",
    ".hex", ".mif",
}
_IGNORED_DIRS = {
    ".superq", "output_files", "db", "incremental_db", "qdb",
    ".git", "__pycache__", "simulation", "tmp-clearbox",
}


EventHandler = Callable[[str, dict[str, Any]], None]


class WatchLoop:
    """Runs an incremental build on every debounced filesystem change."""

    def __init__(
        self,
        core: PocketCore,
        *,
        seed: int = 1,
        debounce_ms: int = 500,
        use_warm_shell: bool = True,
        on_event: EventHandler | None = None,
        builder: IncrementalBuilder | None = None,
    ) -> None:
        self._core = core
        self._seed = seed
        self._debounce_s = debounce_ms / 1000.0
        self._warm = use_warm_shell and quartus_install().is_installed
        self._on_event = on_event or (lambda *_: None)
        self._builder = builder or IncrementalBuilder()

        self._pending = threading.Event()
        self._build_lock = threading.Lock()
        self._stopping = threading.Event()
        self._last_build: IncrementalResult | None = None

    def run(self) -> None:
        try:
            from watchfiles import watch
        except ImportError as e:
            raise RuntimeError(
                "watch-build needs the watchfiles package. "
                "Install with `pip install super-q[watch]` or `pip install watchfiles`."
            ) from e

        self._on_event("watch.started", {
            "core": self._core.full_name,
            "watching": [str(self._core.quartus_dir), str(self._core.root)],
            "warm_shell": self._warm,
        })

        # Prime with an initial build so the user sees fresh timing on startup.
        self._pending.set()
        threading.Thread(target=self._build_consumer, daemon=True).start()

        try:
            for changes in watch(
                self._core.root,
                watch_filter=self._accept,
                debounce=int(self._debounce_s * 1000),
                stop_event=self._stopping,
            ):
                if not changes:
                    continue
                files = sorted({str(p) for _, p in changes})[:6]
                self._on_event("watch.changed", {"files": files, "n": len(changes)})
                self._pending.set()
        except KeyboardInterrupt:
            self.stop()

    def stop(self) -> None:
        self._stopping.set()
        self._pending.set()
        self._builder.shutdown()

    # ------------------------------------------------------------------ #

    def _accept(self, change: Any, path: str) -> bool:
        p = Path(path)
        if any(part in _IGNORED_DIRS for part in p.parts):
            return False
        # Accept any file with a watched suffix, or new directories that
        # might contain them.
        return p.suffix.lower() in _WATCHED_SUFFIXES

    def _build_consumer(self) -> None:
        while not self._stopping.is_set():
            self._pending.wait(timeout=0.5)
            if self._stopping.is_set():
                return
            if not self._pending.is_set():
                continue
            # Drain — any new changes during the build will re-set it.
            self._pending.clear()
            with self._build_lock:
                self._run_one_build()

    def _run_one_build(self) -> None:
        job_id = f"watch-{int(time.time())}"
        self._on_event("build.started", {"job_id": job_id, "seed": self._seed})
        try:
            res = self._builder.run(
                self._core,
                job_id=job_id,
                seed=self._seed,
                use_warm_shell=self._warm,
            )
        except Exception as e:
            log.exception("watch build failed")
            self._on_event("build.failed", {"job_id": job_id, "error": str(e)})
            return

        self._last_build = res
        self._on_event("build.finished", {
            "job_id": job_id,
            "ok": res.ok,
            "slack_ns": res.timing.worst_setup_slack_ns if res.timing else None,
            "fmax_mhz": res.timing.worst_fmax_mhz if res.timing else None,
            "duration_s": res.duration_s,
            "rbf_r_path": str(res.rbf_r_path) if res.rbf_r_path else None,
            "reused_warm_shell": res.reused_warm_shell,
            "error": res.error,
        })
