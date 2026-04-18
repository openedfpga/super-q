"""Direct Quartus runner.

This module is the one place that actually shells out to Quartus. It
supports two build modes:

  * `full` — traditional `quartus_sh --flow compile`, but driven through
    our TCL wrapper so we control the seed and capture timing JSON.

  * `split` — run `quartus_syn` once to produce a post-synth checkpoint
    (`.qdb`), then run `quartus_fit` + `quartus_sta` per seed from that
    checkpoint. Much faster for seed sweeps because synthesis is reused.

Outputs per run:
  * `<project>.sof` / `.rbf` in `output_files/`
  * `<project>.sta.rpt` timing report
  * `timing.json` structured timing (emitted by our TCL wrapper)
  * `superq.log` full Quartus stdout/stderr

Sandboxing: for parallel seeds we never share a Quartus work dir.
The caller passes in a fresh `work_dir` that we rsync the project into.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from super_q.config import quartus_install
from super_q.project import PocketCore
from super_q.timing import TimingReport, merge_reports, parse_sta_report, parse_timing_json

# Our TCL lives alongside the Python package so pip-installed users get it too.
_TCL_DIR = Path(__file__).resolve().parent.parent.parent / "tcl"
if not (_TCL_DIR / "build_seed.tcl").exists():
    # installed layout (see pyproject.toml shared-data)
    alt = Path(__file__).resolve().parent / "tcl"
    if (alt / "build_seed.tcl").exists():
        _TCL_DIR = alt


@dataclass
class BuildRequest:
    core: PocketCore
    seed: int
    work_dir: Path
    mode: str = "full"            # 'full' | 'split-fit'
    qdb_checkpoint: Path | None = None
    parallel_threads: int = 2     # fitter threads per run
    timeout_s: int = 60 * 60       # hard ceiling (1h)
    extra_assignments: dict[str, str] | None = None
    # Scheduler sets this when early-exit fires; `_run` polls it and
    # kills the Quartus process group instead of waiting for timeout.
    cancel_event: threading.Event | None = None


@dataclass
class BuildResult:
    ok: bool
    seed: int
    work_dir: Path
    rbf_path: Path | None
    rbf_r_path: Path | None
    sof_path: Path | None
    log_path: Path
    timing: TimingReport | None
    error: str | None = None
    duration_s: float = 0.0


class QuartusError(Exception):
    pass


def ensure_quartus() -> None:
    """Raise a friendly error if Quartus isn't installed on this host."""
    q = quartus_install()
    if not q.is_installed:
        raise QuartusError(
            "Quartus Lite was not found. Set $QUARTUS_ROOTDIR, add quartus_sh "
            "to PATH, or run `superq install-quartus --help` to bootstrap a "
            "24.1 install locally."
        )


def prepare_work_dir(core: PocketCore, work_dir: Path) -> Path:
    """Copy the project into an isolated sandbox for seed-parallel builds.

    We copy the Quartus dir (where the .qpf lives) plus anything it
    references via relative paths in the .qsf. For safety we copy the
    whole repo root by default — FPGA projects are tiny compared to
    synthesis output.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    sandbox = work_dir / core.root.name
    if sandbox.exists():
        shutil.rmtree(sandbox)
    shutil.copytree(
        core.root,
        sandbox,
        ignore=shutil.ignore_patterns(
            ".git", ".superq", "output_files", "db", "incremental_db",
            "qdb", "__pycache__", "*.pyc", "simulation", "tmp-clearbox",
        ),
        symlinks=True,
    )
    return sandbox


def run_full_compile(req: BuildRequest) -> BuildResult:
    """Run `quartus_sh --flow compile` with our seed TCL preamble."""
    ensure_quartus()
    start = time.time()

    sandbox = prepare_work_dir(req.core, req.work_dir)
    qdir_rel = req.core.quartus_dir.relative_to(req.core.root)
    qdir = sandbox / qdir_rel
    log_path = req.work_dir / "superq.log"

    args = _quartus_sh_cmd(
        qdir=qdir,
        project=req.core.project_name,
        tcl="build_seed.tcl",
        env_overrides=_env(req),
    )

    rc = _run(args, cwd=qdir, log_path=log_path, timeout_s=req.timeout_s,
              threads=req.parallel_threads, cancel_event=req.cancel_event)

    timing = _read_timing(qdir, req.core.project_name)
    rbf = qdir / "output_files" / f"{req.core.project_name}.rbf"
    sof = qdir / "output_files" / f"{req.core.project_name}.sof"

    ok = rc == 0 and (timing.passed if timing else False)
    rbf_path = rbf if rbf.exists() else None
    sof_path = sof if sof.exists() else None

    error: str | None = None
    if rc != 0:
        error = f"quartus_sh exit code {rc}"
    elif rbf_path is None:
        error = "compile succeeded but no .rbf produced — check QSF bitstream settings"
    elif timing and not timing.passed:
        error = f"timing not met: {timing.summary}"

    return BuildResult(
        ok=ok,
        seed=req.seed,
        work_dir=sandbox,
        rbf_path=rbf_path,
        rbf_r_path=None,  # filled in by artifacts.py later
        sof_path=sof_path,
        log_path=log_path,
        timing=timing,
        error=error,
        duration_s=time.time() - start,
    )


def run_split_fit(req: BuildRequest) -> BuildResult:
    """Fitter-only build from a pre-synthesized `.qdb` checkpoint.

    Caller must have produced the checkpoint by running `run_synth_once`
    and should pass the resulting file via `req.qdb_checkpoint`.
    """
    ensure_quartus()
    if req.qdb_checkpoint is None or not req.qdb_checkpoint.exists():
        raise QuartusError("split-fit mode requires a valid qdb_checkpoint")

    start = time.time()
    sandbox = prepare_work_dir(req.core, req.work_dir)
    qdir = sandbox / req.core.quartus_dir.relative_to(req.core.root)
    log_path = req.work_dir / "superq.log"

    # Copy the shared checkpoint into the sandbox so fitter can consume it.
    qdb_target = qdir / "qdb"
    qdb_target.mkdir(exist_ok=True)
    shutil.copy2(req.qdb_checkpoint, qdb_target / req.qdb_checkpoint.name)

    args = _quartus_sh_cmd(
        qdir=qdir,
        project=req.core.project_name,
        tcl="fit_from_qdb.tcl",
        env_overrides={
            **_env(req),
            "SUPER_Q_QDB": str(qdb_target / req.qdb_checkpoint.name),
        },
    )

    rc = _run(args, cwd=qdir, log_path=log_path, timeout_s=req.timeout_s,
              threads=req.parallel_threads, cancel_event=req.cancel_event)

    timing = _read_timing(qdir, req.core.project_name)
    rbf = qdir / "output_files" / f"{req.core.project_name}.rbf"

    ok = rc == 0 and (timing.passed if timing else False)
    return BuildResult(
        ok=ok,
        seed=req.seed,
        work_dir=sandbox,
        rbf_path=rbf if rbf.exists() else None,
        rbf_r_path=None,
        sof_path=None,
        log_path=log_path,
        timing=timing,
        error=None if ok else (f"fitter rc={rc}" if rc else "timing not met"),
        duration_s=time.time() - start,
    )


def run_synth_once(core: PocketCore, work_dir: Path, *, threads: int = 4,
                   timeout_s: int = 60 * 60) -> Path:
    """Run synthesis once, return the path to the saved `.qdb` checkpoint."""
    ensure_quartus()
    sandbox = prepare_work_dir(core, work_dir)
    qdir = sandbox / core.quartus_dir.relative_to(core.root)
    log_path = work_dir / "superq-synth.log"

    args = _quartus_sh_cmd(
        qdir=qdir,
        project=core.project_name,
        tcl="synth_only.tcl",
        env_overrides={},
    )
    rc = _run(args, cwd=qdir, log_path=log_path, timeout_s=timeout_s,
              threads=threads)
    if rc != 0:
        raise QuartusError(f"Synthesis failed (rc={rc}). See {log_path}")

    qdb = qdir / "qdb" / f"{core.project_name}.qdb"
    if not qdb.exists():
        # older Quartus wrote it with a different name — try glob
        matches = list((qdir / "qdb").glob("*.qdb"))
        if not matches:
            raise QuartusError(f"synth ran but no qdb produced in {qdir / 'qdb'}")
        qdb = matches[0]
    return qdb


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _quartus_sh_cmd(*, qdir: Path, project: str, tcl: str,
                    env_overrides: dict[str, str]) -> list[str]:
    q = quartus_install()
    assert q.sh is not None
    tcl_path = _TCL_DIR / tcl
    if not tcl_path.exists():
        raise QuartusError(f"missing TCL wrapper: {tcl_path}")
    return [
        str(q.sh),
        "-t",
        str(tcl_path),
        project,
    ]


def _env(req: BuildRequest) -> dict[str, str]:
    e: dict[str, str] = {
        "SUPER_Q_SEED": str(req.seed),
        "SUPER_Q_DEVICE": req.core.device,
        "SUPER_Q_PROJECT": req.core.project_name,
    }
    if req.extra_assignments:
        # Serialized as key=value;key2=value2 for the TCL side to parse.
        e["SUPER_Q_EXTRA"] = ";".join(f"{k}={v}" for k, v in req.extra_assignments.items())
    return e


def _run(args: list[str], *, cwd: Path, log_path: Path, timeout_s: int,
         threads: int, env_extra: dict[str, str] | None = None,
         cancel_event: "threading.Event | None" = None) -> int:
    """Run `quartus_*` with timeout + cooperative cancellation.

    `cancel_event` (when provided) lets the scheduler tell this runner
    to stop — we poll it while the subprocess is alive and kill the
    process group on set. Without this, in-flight Quartus compiles
    would block for `timeout_s` seconds (default 3600) even after the
    sweep has early-exited with a passing seed elsewhere.
    """
    import threading  # noqa: PLC0415 — avoid a module-level cost on import

    env = os.environ.copy()
    # Cap threads inside Quartus; we scale parallelism by running more seeds.
    env["QUARTUS_NUM_PARALLEL_PROCESSORS"] = str(max(1, threads))
    if env_extra:
        env.update(env_extra)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Start our own process group so we can SIGTERM the whole Quartus
    # invocation tree, not just the top-level quartus_sh shim. On POSIX
    # `os.setsid` creates a new session; on Windows the corresponding
    # flag is CREATE_NEW_PROCESS_GROUP. We run Quartus only on POSIX
    # in CI, so the POSIX path is load-bearing; the Windows fallback
    # is best-effort.
    preexec = os.setsid if sys.platform != "win32" else None
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    with open(log_path, "w", buffering=1) as log:
        log.write(f"# cmd: {' '.join(args)}\n# cwd: {cwd}\n\n")
        log.flush()
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            preexec_fn=preexec,
            creationflags=creationflags,
        )
        start = time.time()
        try:
            while True:
                try:
                    return proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
                if cancel_event is not None and cancel_event.is_set():
                    log.write("\n# cancelled — terminating process group\n")
                    log.flush()
                    _terminate_group(proc)
                    return 130            # 128 + SIGINT(2)
                if time.time() - start > timeout_s:
                    log.write(f"\n# TIMEOUT after {timeout_s}s — terminating\n")
                    log.flush()
                    _terminate_group(proc)
                    return 124
        except KeyboardInterrupt:
            _terminate_group(proc)
            raise


def _terminate_group(proc: subprocess.Popen) -> None:
    """Best-effort SIGTERM the subprocess group, then SIGKILL if it lingers."""
    try:
        if sys.platform == "win32":
            proc.send_signal(getattr(subprocess.signal, "CTRL_BREAK_EVENT", 2))
        else:
            os.killpg(os.getpgid(proc.pid), 15)  # SIGTERM
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            if sys.platform != "win32":
                os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _read_timing(qdir: Path, project: str) -> TimingReport | None:
    out = qdir / "output_files"
    sta_rpt = out / f"{project}.sta.rpt"
    json_path = out / "timing.json"
    primary = parse_sta_report(sta_rpt)
    fallback = parse_timing_json(json_path)
    return merge_reports(primary, fallback)


def verify_toolchain() -> list[str]:
    """Cheap sanity checks agents can run before scheduling jobs."""
    issues: list[str] = []
    q = quartus_install()
    if not q.is_installed:
        issues.append("Quartus not installed or not on PATH")
        return issues
    required = ("sh", "fit", "syn", "sta", "cpf")
    for name in required:
        p = getattr(q, name)
        if p is None or not p.exists():
            issues.append(f"missing Quartus executable: quartus_{name}")
    tcl_wrappers = ("build_seed.tcl", "fit_from_qdb.tcl", "synth_only.tcl")
    for t in tcl_wrappers:
        if not (_TCL_DIR / t).exists():
            issues.append(f"missing TCL wrapper: {t}")
    return issues


def tcl_dir() -> Path:
    return _TCL_DIR


def iter_work_dirs(base: Path) -> Iterable[Path]:
    for child in base.iterdir():
        if child.is_dir():
            yield child
