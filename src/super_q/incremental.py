"""Incremental compile orchestration.

Unlike seed sweeps (which live in isolated sandboxes) incremental builds
re-use the core's real Quartus `db/` and `incremental_db/` directories so
Quartus can skip work for unchanged partitions. That means:

  * only one incremental build per core at a time (serial by design)
  * we never sandbox-copy — we run directly in the core's quartus_dir
  * the warm-shell path is preferred; falling back to a cold
    `quartus_sh -t` run is fine but costs the startup tax each time

Typical wall-clock numbers on a Pocket core:
    cold full rebuild:   4 – 8 min
    cold incremental:    1 – 3 min
    warm incremental:    20 s – 2 min  (startup reuse is the win)
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from super_q import quartus
from super_q.artifacts import collect
from super_q.project import PocketCore
from super_q.timing import TimingReport, merge_reports, parse_sta_report, parse_timing_json
from super_q.warm_shell import ShellPool, WarmShell, WarmShellError

log = logging.getLogger("superq.incremental")


@dataclass
class IncrementalResult:
    ok: bool
    seed: int
    timing: TimingReport | None
    rbf_r_path: Path | None
    duration_s: float
    reused_warm_shell: bool
    error: str | None = None


class IncrementalBuilder:
    """Serializes incremental builds per-core and reuses warm shells.

    One instance per interpreter is plenty; the CLI's watch-build command
    owns one, and the daemon shares one across its clients. Construct a
    fresh builder only if you want a separate pool (e.g. in a sub-process).
    """

    def __init__(self, *, shell_pool: ShellPool | None = None) -> None:
        self._pool = shell_pool or ShellPool(size=1)
        self._locks: dict[Path, threading.Lock] = {}
        self._lock_table = threading.Lock()

    def _core_lock(self, core: PocketCore) -> threading.Lock:
        with self._lock_table:
            return self._locks.setdefault(core.root.resolve(), threading.Lock())

    def run(
        self,
        core: PocketCore,
        *,
        job_id: str,
        seed: int = 1,
        timeout_s: int = 60 * 60,
        use_warm_shell: bool = True,
    ) -> IncrementalResult:
        quartus.ensure_quartus()
        start = time.time()

        with self._core_lock(core):
            reused = False
            try:
                if use_warm_shell:
                    reused = True
                    timing = self._run_via_shell(core, seed, timeout_s)
                else:
                    timing = self._run_cold(core, seed, timeout_s)
            except Exception as e:
                log.exception("incremental build failed")
                return IncrementalResult(
                    ok=False, seed=seed, timing=None, rbf_r_path=None,
                    duration_s=time.time() - start, reused_warm_shell=reused,
                    error=str(e),
                )

            rbf_src = core.quartus_dir / "output_files" / f"{core.project_name}.rbf"
            artifacts = collect(
                core, job_id, seed, core.quartus_dir,
                rbf=rbf_src if rbf_src.exists() else None,
                sof=None,
                log=None,
            )

            ok = artifacts.rbf_r is not None and (timing.passed if timing else False)
            return IncrementalResult(
                ok=ok,
                seed=seed,
                timing=timing,
                rbf_r_path=artifacts.rbf_r,
                duration_s=time.time() - start,
                reused_warm_shell=reused,
                error=None if ok else (
                    timing.summary if timing and not timing.passed
                    else "no .rbf produced — check GENERATE_RBF_FILE in qsf"
                ),
            )

    # -- implementations --

    def _run_via_shell(self, core: PocketCore, seed: int, timeout_s: int) -> TimingReport | None:
        shell: WarmShell | None = None
        try:
            shell = self._pool.acquire(core.quartus_dir)
            # Open the project (no-op if already open from a prior call).
            shell.open_project(core.project_name)
            result = shell.incremental_compile(seed=seed)
            if not result.ok:
                raise WarmShellError(f"incremental compile failed: {result.output[-400:]}")
            return self._read_timing(core)
        finally:
            if shell is not None:
                self._pool.release(shell, core.quartus_dir)

    def _run_cold(self, core: PocketCore, seed: int, timeout_s: int) -> TimingReport | None:
        req = quartus.BuildRequest(
            core=core,
            seed=seed,
            work_dir=core.quartus_dir,
            mode="full",
        )
        # Bypass the sandboxing prepare step: we want to mutate db/ in place.
        args = quartus._quartus_sh_cmd(  # noqa: SLF001 — internal helper by design
            qdir=core.quartus_dir,
            project=core.project_name,
            tcl="incremental_build.tcl",
            env_overrides={"SUPER_Q_SEED": str(seed), "SUPER_Q_PROJECT": core.project_name},
        )
        log_path = core.superq_dir / "incremental.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        quartus._run(  # noqa: SLF001
            args,
            cwd=core.quartus_dir,
            log_path=log_path,
            timeout_s=timeout_s,
            threads=4,
            env_extra={"SUPER_Q_SEED": str(seed)},
        )
        return self._read_timing(core)

    def _read_timing(self, core: PocketCore) -> TimingReport | None:
        out = core.quartus_dir / "output_files"
        primary = parse_sta_report(out / f"{core.project_name}.sta.rpt")
        return merge_reports(primary, parse_timing_json(out / "timing.json"))

    def shutdown(self) -> None:
        self._pool.shutdown()
