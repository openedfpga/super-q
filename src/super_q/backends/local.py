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
                 threads_per_task: int = 2):
        cap = host_capacity()
        self._max = max_parallel or max(1, cap.cpu_count // max(1, threads_per_task))
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
