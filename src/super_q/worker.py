"""Worker process.

Two operating modes:

  * `daemon` — long-running. Registers with the DB, heartbeats, and pulls
    queued tasks until asked to quit. Used on beefy local workstations
    and on persistent cloud VMs.

  * `one-shot` — run exactly one seed for a specific job. Used by AWS
    spot instances and ad-hoc CI jobs. Exits 0 on success, nonzero on
    failure; writes a JSON result envelope so the parent scheduler can
    pick it up (locally or via S3).

In both modes the worker uses the `LocalBackend` internally — the
distinction is who's orchestrating. `super-q-worker daemon` turns any
machine into extra capacity for an already-running sweep.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import signal
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import typer

from super_q.backends import TaskOutcome, TaskSpec
from super_q.backends.local import LocalBackend
from super_q.config import banner, paths
from super_q.db import Store
from super_q.project import detect_core

app = typer.Typer(no_args_is_help=True, add_completion=False, help=banner())
log = logging.getLogger("superq.worker")


@app.callback()
def _root(verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


@app.command("daemon")
def daemon_cmd(
    slots: int = typer.Option(4, "--slots", help="Concurrent tasks to run"),
    threads: int = typer.Option(2, "--threads", help="Quartus threads per task"),
    idle_quit_s: int = typer.Option(
        0, "--idle-quit", help="Seconds idle before exit (0 = never)"
    ),
) -> None:
    """Run as a long-lived worker attached to the local SQLite state.

    Tasks are pulled from the `tasks` table where backend='local'. This lets
    you fire up extra workers on a second machine mounting the same state
    directory over NFS/sshfs, or on the same machine to add parallelism
    without restarting the scheduler.
    """
    store = Store(paths().db_path)
    backend = LocalBackend(max_parallel=slots, threads_per_task=threads)

    worker_id = f"daemon-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
    store.register_worker(
        worker_id,
        host=socket.gethostname(),
        backend="local",
        slots=slots,
        info={"platform": platform.platform(), "pid": os.getpid()},
    )
    log.info("worker %s up; slots=%d threads=%d", worker_id, slots, threads)

    _installed_signal_quit = {"quit": False}

    def _quit(signum, frame):  # noqa: ARG001
        log.info("signal %s: shutting down", signum)
        _installed_signal_quit["quit"] = True

    signal.signal(signal.SIGINT, _quit)
    signal.signal(signal.SIGTERM, _quit)

    last_task_ts = time.time()
    while not _installed_signal_quit["quit"]:
        store.heartbeat(worker_id)

        task = store.next_queued_task("local")
        if task is None:
            if idle_quit_s and time.time() - last_task_ts > idle_quit_s:
                log.info("idle %ds; exiting", idle_quit_s)
                return
            time.sleep(1.0)
            continue

        if not store.claim_task(task["id"], worker_id):
            continue
        last_task_ts = time.time()

        # Materialize the core + spec from the queued task's sibling job.
        job = store.get_job(task["job_id"])
        if job is None:
            store.finish_task(task["id"], status="failed", error="job missing")
            continue
        spec_json = json.loads(job["spec_json"])
        try:
            core = detect_core(spec_json["core"]["root"])
        except Exception as e:
            store.finish_task(task["id"], status="failed", error=f"bad core: {e}")
            continue

        work_dir = paths().cache_dir / "work" / task["job_id"] / f"seed-{task['seed']:04d}"
        spec = TaskSpec(
            core=core,
            seed=task["seed"],
            job_id=task["job_id"],
            task_id=task["id"],
            work_dir=work_dir,
        )
        try:
            outcome = backend.run(spec)
        except Exception as e:
            log.exception("task failed")
            outcome = TaskOutcome(
                ok=False, seed=task["seed"], rbf_path=None, rbf_r_path=None,
                timing=None, log_path=None, error=str(e), duration_s=0.0,
            )
        store.finish_task(
            task["id"],
            status="passed" if outcome.ok else "failed",
            slack_ns=outcome.timing.worst_setup_slack_ns if outcome.timing else None,
            fmax_mhz=outcome.timing.worst_fmax_mhz if outcome.timing else None,
            timing=outcome.timing.as_dict() if outcome.timing else None,
            rbf_path=str(outcome.rbf_r_path) if outcome.rbf_r_path else None,
            log_path=str(outcome.log_path) if outcome.log_path else None,
            error=outcome.error,
        )


@app.command("one-shot")
def one_shot_cmd(
    project: str = typer.Option(..., "--project", help="Quartus project name (without .qpf)"),
    quartus_dir: Path = typer.Option(..., "--quartus-dir", help="Directory containing the .qpf, relative to CWD"),
    seed: int = typer.Option(1, "--seed", envvar="SUPER_Q_SEED"),
    output_json: Path = typer.Option(Path("result.json"), "--output"),
    bucket: str = typer.Option("", "--bucket", help="S3 bucket for uploads (AWS mode)"),
    output_key: str = typer.Option("", "--output-key", help="S3 key prefix for uploads"),
) -> None:
    """Run a single seed build in the current directory. Used by cloud.

    This mode doesn't touch the DB — it takes everything from CLI args
    and emits a JSON envelope the parent process parses. Good for AWS
    spot, serverless runners, or a plain `bash` script that wants the
    bitstream and timing without installing the full super-q toolkit.
    """
    from super_q import quartus as qmod
    from super_q.artifacts import reverse_rbf

    root = Path.cwd()
    core_dir = root  # the worker is invoked inside the uploaded sandbox
    qdir = core_dir / quartus_dir

    # Build a minimal PocketCore manually — we trust the caller.
    from super_q.project import PocketCore

    qpf_candidates = list(qdir.glob("*.qpf"))
    qpf = qdir / f"{project}.qpf" if not qpf_candidates else qpf_candidates[0]
    core = PocketCore(
        root=core_dir,
        quartus_dir=qdir,
        qpf=qpf,
        qsf=qdir / f"{project}.qsf" if (qdir / f"{project}.qsf").exists() else None,
        project_name=project,
    )

    req = qmod.BuildRequest(core=core, seed=seed, work_dir=root / "out")
    result = qmod.run_full_compile(req)

    rbf_r_path: Path | None = None
    if result.rbf_path and result.rbf_path.exists():
        rbf_r_path = result.rbf_path.with_suffix(".rbf_r")
        reverse_rbf(result.rbf_path, rbf_r_path)

    envelope: dict[str, Any] = {
        "ok": result.ok,
        "seed": seed,
        "duration_s": result.duration_s,
        "error": result.error,
        "timing": result.timing.as_dict() if result.timing else None,
        "rbf_path": str(result.rbf_path) if result.rbf_path else None,
        "rbf_r_path": str(rbf_r_path) if rbf_r_path else None,
    }
    output_json.write_text(json.dumps(envelope, indent=2))
    log.info("wrote %s (ok=%s)", output_json, result.ok)

    if bucket and output_key and rbf_r_path:
        try:
            import boto3
            s3 = boto3.client("s3")
            s3.upload_file(str(rbf_r_path), bucket, f"{output_key}/bitstream.rbf_r")
            s3.upload_file(str(output_json), bucket, f"{output_key}/result.json")
            if result.log_path and result.log_path.exists():
                s3.upload_file(str(result.log_path), bucket, f"{output_key}/build.log")
        except ImportError:
            log.warning("boto3 missing; skipping S3 upload")

    sys.exit(0 if result.ok else 1)


def main() -> None:  # entry point for `super-q-worker` script
    app()


if __name__ == "__main__":
    main()
