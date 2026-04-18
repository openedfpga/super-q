"""Local-subprocess backend.

Runs Quartus on the current machine. This is the default for quick seed
sweeps on a dev laptop or beefy workstation. Each task gets its own work
directory so seeds don't collide.
"""
from __future__ import annotations

import os
import platform
import threading
from pathlib import Path

from super_q import quartus
from super_q.artifacts import collect
from super_q.backends.base import BackendError, TaskOutcome, TaskSpec
from super_q.config import host_capacity, quartus_install


class LocalBackend:
    name = "local"

    def __init__(self, *, max_parallel: int | None = None,
                 threads_per_task: int = 1):
        # Quartus Lite (the edition we target) is single-threaded for
        # compile regardless of NUM_PARALLEL_PROCESSORS — it literally
        # logs `Info (20029): Only one processor detected - disabling
        # parallel compilation`. So the honest default is 1 thread/seed
        # and `max_parallel = cpu_count`, giving a 4-vCPU runner 4
        # concurrent compiles instead of 2.
        #
        # The `threads_per_task` knob exists for Pro-edition users where
        # >1 thread/compile is real; it still controls the env var we
        # set, just no longer inflates the slot math.
        cap = host_capacity()
        self._max = max_parallel or max(1, cap.cpu_count)
        self._threads = threads_per_task
        self._sem = threading.Semaphore(self._max)
        self._lock = threading.Lock()

    def available_slots(self) -> int:
        with self._lock:
            return self._max

    def describe(self) -> dict:
        q = quartus_install()
        cap = host_capacity()
        return {
            "backend": "local",
            "host": platform.node(),
            "platform": cap.platform_name,
            "cpus": cap.cpu_count,
            "mem_gb": round(cap.mem_gb, 1),
            "quartus_version": q.version,
            "quartus_installed": q.is_installed,
            "max_parallel": self._max,
            "threads_per_task": self._threads,
        }

    def run(self, spec: TaskSpec) -> TaskOutcome:
        if not quartus_install().is_installed:
            raise BackendError(
                "local backend requires Quartus; install it or switch to a "
                "docker/aws backend"
            )
        with self._sem:
            return self._run_inner(spec)

    def _run_inner(self, spec: TaskSpec) -> TaskOutcome:
        work_dir = spec.work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        req = quartus.BuildRequest(
            core=spec.core,
            seed=spec.seed,
            work_dir=work_dir,
            mode=spec.mode,
            qdb_checkpoint=spec.qdb_checkpoint,
            parallel_threads=spec.threads or self._threads,
            timeout_s=spec.timeout_s,
            extra_assignments=spec.extra_assignments,
            cancel_event=spec.cancel_event,
        )
        result = (quartus.run_split_fit(req)
                  if spec.mode == "split-fit"
                  else quartus.run_full_compile(req))

        # When Quartus errored out (not just timing miss), surface the
        # last chunk of its log to stderr so CI operators and agents
        # don't have to download the workflow artifact to see why.
        if not result.ok and result.log_path and result.log_path.exists():
            _tail_log_to_stderr(result.log_path, spec.seed, result.error)

        artifacts = collect(
            spec.core,
            spec.job_id,
            spec.seed,
            result.work_dir,
            rbf=result.rbf_path,
            sof=result.sof_path,
            log=result.log_path,
        )

        return TaskOutcome(
            ok=result.ok and artifacts.rbf_r is not None,
            seed=spec.seed,
            rbf_path=artifacts.rbf,
            rbf_r_path=artifacts.rbf_r,
            timing=result.timing,
            log_path=artifacts.log or result.log_path,
            error=result.error,
            duration_s=result.duration_s,
        )

    def cleanup(self, work_dir: Path) -> None:
        """Optional post-run cleanup hook. The scheduler calls this for
        failed seeds in best-of-N sweeps to reclaim disk space quickly.
        """
        import shutil
        if work_dir.exists() and os.environ.get("SUPERQ_KEEP_WORK") != "1":
            shutil.rmtree(work_dir, ignore_errors=True)


def _tail_log_to_stderr(log_path: Path, seed: int, error: str | None,
                        *, lines: int = 40) -> None:
    """Print the last N log lines to stderr, fenced with a header.

    Specifically targets the case where Quartus or our TCL wrapper
    errored — those always leave the diagnostic at the tail of the log.
    For a timing-miss-only failure (rc=0 but slack<0) we skip this
    noise since the failure is obvious from the timing summary.
    """
    import sys as _s
    # Timing misses have a specific error prefix from run_full_compile.
    if error and error.startswith("timing not met"):
        return
    try:
        with open(log_path, errors="replace") as fh:
            tail = fh.readlines()[-lines:]
    except OSError:
        return
    print(f"\n--- seed={seed} last {len(tail)} lines of {log_path} ---",
          file=_s.stderr, flush=True)
    _s.stderr.writelines(tail)
    print(f"--- end seed={seed} ---", file=_s.stderr, flush=True)
