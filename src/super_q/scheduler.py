"""Scheduler — the brains that actually run builds.

Given a core, a seed plan, and a backend, the scheduler:
  1. Creates a job row and N task rows in the DB.
  2. Optionally runs synthesis once for "split-fit" mode.
  3. Fans out tasks to the backend's worker pool.
  4. Early-exits when a passing seed is found (if configured).
  5. Writes per-seed outcomes to the DB + updates the job row.

All dispatch is done via a thread pool — Quartus is CPU-bound and the
GIL is released while it subprocess-calls, so threads are the right
abstraction. For the AWS backend, the thread pool just fans out HTTP
and S3 I/O.

Callers typically use `Scheduler.run_sweep(...)` which blocks until
done, or `Scheduler.submit_sweep(...)` to return a job id and poll.
"""
from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from super_q import quartus
from super_q.backends.base import Backend, TaskOutcome, TaskSpec
from super_q.config import paths
from super_q.db import Store
from super_q.project import PocketCore
from super_q.seeds import SeedPlan, SeedResult, summarize

log = logging.getLogger("superq.scheduler")

ProgressFn = Callable[[str, dict], None]  # event, payload


@dataclass
class SweepOutcome:
    job_id: str
    core: PocketCore
    plan: SeedPlan
    results: list[SeedResult]
    summary: dict
    best: SeedResult | None

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "core": self.core.as_dict(),
            "plan": self.plan.as_dict(),
            "results": [r.as_dict() for r in self.results],
            "summary": self.summary,
            "best": self.best.as_dict() if self.best else None,
        }


class Scheduler:
    def __init__(self, store: Store, backend: Backend, *, on_event: ProgressFn | None = None):
        self.store = store
        self.backend = backend
        self.on_event = on_event or (lambda *_: None)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def run_sweep(
        self,
        core: PocketCore,
        plan: SeedPlan,
        *,
        mode: str = "full",
        threads_per_task: int = 2,
        timeout_s: int = 60 * 60,
        extra_assignments: dict[str, str] | None = None,
    ) -> SweepOutcome:
        """Run a seed sweep end-to-end. Blocks until done.

        `mode`:
          - "full"       — every seed runs full synth+fit. Simple & robust.
          - "split-fit"  — synthesis once, fitter per seed. Fast; relies on
                           Quartus versions that support .qdb checkpoints.
        """
        job_id = self._create_job(core, plan, kind="sweep")

        try:
            self._emit("job.started", {"job_id": job_id, "core": core.full_name})

            # split-fit precomputes a shared synthesis checkpoint.
            qdb: Path | None = None
            if mode == "split-fit":
                qdb = self._run_synth_once(core, job_id)

            results: list[SeedResult] = []
            best: SeedResult | None = None
            cancel_flag = threading.Event()

            futures = self._dispatch_tasks(
                job_id=job_id,
                core=core,
                plan=plan,
                mode=mode,
                qdb=qdb,
                threads_per_task=threads_per_task,
                timeout_s=timeout_s,
                extra_assignments=extra_assignments,
                cancel_flag=cancel_flag,
            )

            early_exit = False
            for fut in as_completed(futures):
                task_id, spec, outcome = fut.result()
                sr = self._record_outcome(job_id, task_id, spec, outcome)
                results.append(sr)

                if sr.passed and (best is None or sr.score > best.score):
                    best = sr

                if sr.passed and plan.stop_on_first_pass:
                    log.info("seed=%s passed; signaling early exit", sr.seed)
                    cancel_flag.set()
                    # Cancel queued futures outright. In-flight ones can't
                    # be cancelled via Future.cancel, but setting
                    # cancel_flag tells the Quartus runner to SIGTERM its
                    # process group — so we return in seconds instead of
                    # waiting out timeout_s per straggler.
                    for f in futures:
                        if not f.done():
                            f.cancel()
                    early_exit = True
                    break

            # Tear down the pool explicitly; cancel_flag + `_run`'s
            # subprocess-group kill handle the in-flight work. Without
            # this, the executor's daemon threads keep ->waiting on
            # Python interpreter shutdown until their Popen's exit.
            if early_exit:
                _shutdown_executor(futures)

            summary = summarize(results, plan=plan)
            status = "passed" if best else "failed"
            self.store.finish_job(
                job_id,
                status=status,
                best_seed=best.seed if best else None,
                best_slack_ns=best.slack_ns if best else None,
                best_fmax_mhz=best.fmax_mhz if best else None,
                artifact_path=best.rbf_r_path if best else None,
                message=summary.get("summary"),
            )

            self._emit(
                "job.finished",
                {"job_id": job_id, "status": status, "summary": summary},
            )
            return SweepOutcome(
                job_id=job_id,
                core=core,
                plan=plan,
                results=results,
                summary=summary,
                best=best,
            )

        except Exception as e:
            log.exception("sweep failed")
            self.store.finish_job(job_id, status="failed", message=str(e))
            raise

    def cancel(self, job_id: str) -> int:
        return self.store.cancel_job(job_id)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _create_job(self, core: PocketCore, plan: SeedPlan, *, kind: str) -> str:
        job_id = self.store.create_job(
            core_path=str(core.root),
            core_name=core.full_name,
            kind=kind,
            spec={
                "plan": plan.as_dict(),
                "core": core.as_dict(),
                "backend": self.backend.describe(),
            },
        )
        self.store.start_job(job_id)
        return job_id

    def _run_synth_once(self, core: PocketCore, job_id: str) -> Path | None:
        """Run synthesis once and publish a shared .qdb checkpoint."""
        self._emit("synth.started", {"job_id": job_id})
        work_dir = paths().cache_dir / "synth" / job_id
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            qdb = quartus.run_synth_once(core, work_dir)
            self._emit("synth.finished", {"job_id": job_id, "qdb": str(qdb)})
            return qdb
        except Exception as e:
            log.warning("split-fit synth failed, falling back to full per seed: %s", e)
            self._emit("synth.failed", {"job_id": job_id, "error": str(e)})
            return None

    def _dispatch_tasks(
        self,
        *,
        job_id: str,
        core: PocketCore,
        plan: SeedPlan,
        mode: str,
        qdb: Path | None,
        threads_per_task: int,
        timeout_s: int,
        extra_assignments: dict[str, str] | None,
        cancel_flag: threading.Event,
    ) -> list[Future]:
        """Submit every seed to the backend's worker pool."""
        effective_mode = "split-fit" if (mode == "split-fit" and qdb) else "full"
        parallel = min(plan.max_parallel, self.backend.available_slots())
        ex = ThreadPoolExecutor(max_workers=max(1, parallel), thread_name_prefix="superq-seed")
        futures: list[Future] = []

        for seed in plan.seeds:
            task_id = self.store.create_task(job_id=job_id, seed=seed, backend=self.backend.name)
            work_dir = paths().cache_dir / "work" / job_id / f"seed-{seed:04d}"
            spec = TaskSpec(
                core=core,
                seed=seed,
                job_id=job_id,
                task_id=task_id,
                work_dir=work_dir,
                mode=effective_mode,
                qdb_checkpoint=qdb,
                threads=threads_per_task,
                timeout_s=timeout_s,
                extra_assignments=extra_assignments,
                # Shared cancellation signal so in-flight Quartus procs die
                # promptly when the first passing seed triggers early exit,
                # instead of burning the full timeout_s.
                cancel_event=cancel_flag,
            )
            fut = ex.submit(self._run_one, task_id, spec, cancel_flag)
            futures.append(fut)
        return futures

    def _run_one(
        self,
        task_id: str,
        spec: TaskSpec,
        cancel_flag: threading.Event,
    ) -> tuple[str, TaskSpec, TaskOutcome]:
        # Respect early-exit: don't start a new task if we've already won.
        if cancel_flag.is_set():
            self.store.finish_task(task_id, status="cancelled")
            return task_id, spec, TaskOutcome(
                ok=False, seed=spec.seed, rbf_path=None, rbf_r_path=None,
                timing=None, log_path=None, error="cancelled", duration_s=0.0,
            )

        worker_id = f"inline-{uuid.uuid4().hex[:6]}"
        if not self.store.claim_task(task_id, worker_id):
            return task_id, spec, TaskOutcome(
                ok=False, seed=spec.seed, rbf_path=None, rbf_r_path=None,
                timing=None, log_path=None, error="already claimed", duration_s=0.0,
            )
        self.store.start_task(task_id)
        self._emit("seed.started", {"seed": spec.seed, "task_id": task_id})
        try:
            outcome = self.backend.run(spec)
        except Exception as e:
            outcome = TaskOutcome(
                ok=False, seed=spec.seed, rbf_path=None, rbf_r_path=None,
                timing=None, log_path=None, error=f"backend error: {e}",
                duration_s=0.0,
            )
        self._emit(
            "seed.finished",
            {
                "seed": spec.seed,
                "task_id": task_id,
                "ok": outcome.ok,
                "slack": outcome.timing.worst_setup_slack_ns if outcome.timing else None,
                "fmax": outcome.timing.worst_fmax_mhz if outcome.timing else None,
                "duration_s": outcome.duration_s,
                "error": outcome.error,
            },
        )
        return task_id, spec, outcome

    def _record_outcome(
        self,
        job_id: str,
        task_id: str,
        spec: TaskSpec,
        outcome: TaskOutcome,
    ) -> SeedResult:
        timing = outcome.timing
        self.store.finish_task(
            task_id,
            status="passed" if outcome.ok else "failed",
            slack_ns=timing.worst_setup_slack_ns if timing else None,
            fmax_mhz=timing.worst_fmax_mhz if timing else None,
            timing=timing.as_dict() if timing else None,
            rbf_path=str(outcome.rbf_r_path) if outcome.rbf_r_path else None,
            log_path=str(outcome.log_path) if outcome.log_path else None,
            error=outcome.error,
        )
        return SeedResult(
            seed=spec.seed,
            passed=outcome.ok,
            slack_ns=timing.worst_setup_slack_ns if timing else None,
            fmax_mhz=timing.worst_fmax_mhz if timing else None,
            duration_s=outcome.duration_s,
            rbf_r_path=str(outcome.rbf_r_path) if outcome.rbf_r_path else None,
            error=outcome.error,
            timing=timing.as_dict() if timing else None,
        )

    def _emit(self, kind: str, payload: dict) -> None:
        self.store.record_event(payload.get("job_id"), payload.get("task_id"), kind, payload)
        try:
            self.on_event(kind, payload)
        except Exception:  # never let UI bugs kill a sweep
            log.exception("progress callback raised")


def _shutdown_executor(futures: list[Future]) -> None:
    """Wait up to a few seconds for in-flight futures, then give up.

    `cancel_event` signals SIGTERM inside `_run`, so futures should
    resolve in <10 s after early-exit. If one ignores the signal
    (buggy Quartus wrapper, NFS stall), we don't want to block the
    whole sweep — just return; the orphaned process group will be
    reaped by the runner host.
    """
    import concurrent.futures as cf
    try:
        cf.wait(futures, timeout=15.0)
    except Exception:  # pragma: no cover — purely defensive
        log.exception("executor shutdown waited too long")


def batch_run(
    store: Store,
    backend: Backend,
    cores: list[PocketCore],
    plan_factory: Callable[[PocketCore], SeedPlan],
    *,
    parallel_cores: int = 2,
    on_event: ProgressFn | None = None,
    mode: str = "full",
) -> list[SweepOutcome]:
    """Sweep many cores concurrently — good for CI and bulk explorations.

    `parallel_cores` is the number of cores to run at once; each still
    gets its own per-seed parallelism from the plan. Keep this small:
    Quartus is RAM-hungry and fitter peaks easily exceed 4 GB.
    """
    outcomes: list[SweepOutcome] = []
    sched = Scheduler(store, backend, on_event=on_event)
    ex = ThreadPoolExecutor(max_workers=parallel_cores, thread_name_prefix="superq-core")
    futs = {ex.submit(sched.run_sweep, c, plan_factory(c), mode=mode): c for c in cores}
    for fut in as_completed(futs):
        try:
            outcomes.append(fut.result())
        except Exception as e:
            core = futs[fut]
            log.error("core %s failed: %s", core.full_name, e)
    return outcomes
