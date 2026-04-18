"""Docker backend — runs Quartus inside a container.

Useful when agents are on macOS (where Quartus doesn't run natively) or
want reproducible builds across a fleet of cheap Linux VMs. The image is
built from `docker/Dockerfile` which contains a licensed-EULA-accepted
copy of Quartus Lite 24.1.

We support two container topologies:
  * `local` (default): spawn a new container per task against the local
    Docker daemon. Simplest; good for a handful of seeds.
  * `swarm`: submit to a Docker Swarm service scaled to N workers. The
    workers pull from a shared Redis-backed queue. See docs/cloud.md.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from super_q.artifacts import collect
from super_q.backends.base import BackendError, TaskOutcome, TaskSpec
from super_q.timing import merge_reports, parse_sta_report, parse_timing_json

DEFAULT_IMAGE = os.environ.get("SUPERQ_DOCKER_IMAGE", "superq/quartus:24.1")


class DockerBackend:
    name = "docker"

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        max_parallel: int = 4,
        threads_per_task: int = 2,
        extra_args: list[str] | None = None,
    ):
        self._image = image
        self._max = max_parallel
        self._threads = threads_per_task
        self._extra_args = extra_args or []
        self._sem = threading.Semaphore(self._max)
        if shutil.which("docker") is None:
            raise BackendError("docker CLI not found on PATH")

    def available_slots(self) -> int:
        return self._max

    def describe(self) -> dict:
        return {
            "backend": "docker",
            "image": self._image,
            "max_parallel": self._max,
            "threads_per_task": self._threads,
        }

    def run(self, spec: TaskSpec) -> TaskOutcome:
        with self._sem:
            return self._run_inner(spec)

    def _run_inner(self, spec: TaskSpec) -> TaskOutcome:
        start = time.time()
        # Mount the core root so the container can see sources, and the
        # work dir so we can collect outputs from outside.
        core_root = str(spec.core.root)
        work_dir = spec.work_dir
        work_dir.mkdir(parents=True, exist_ok=True)

        qdir_rel = str(spec.core.quartus_dir.relative_to(spec.core.root))
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{core_root}:/work/core:rw",
            "-v", f"{work_dir}:/work/out:rw",
            "-e", f"SUPER_Q_SEED={spec.seed}",
            "-e", f"SUPER_Q_PROJECT={spec.core.project_name}",
            "-e", f"QUARTUS_NUM_PARALLEL_PROCESSORS={spec.threads or self._threads}",
            *self._extra_args,
            self._image,
            "/entrypoint.sh",
            spec.mode,
            qdir_rel,
            spec.core.project_name,
        ]
        log_path = work_dir / "superq.log"
        with open(log_path, "w", buffering=1) as log:
            log.write(f"# docker run: seed={spec.seed} image={self._image}\n")
            rc = subprocess.run(
                cmd, stdout=log, stderr=subprocess.STDOUT,
                timeout=spec.timeout_s, check=False,
            ).returncode

        # Outputs land in /work/out (bound to work_dir) then we pull them
        # into the canonical artifact layout on the host side.
        qdir = work_dir / "core" / qdir_rel  # mirror of the sandbox inside the container
        rbf = qdir / "output_files" / f"{spec.core.project_name}.rbf"
        sta_rpt = qdir / "output_files" / f"{spec.core.project_name}.sta.rpt"
        timing_json = qdir / "output_files" / "timing.json"
        timing = merge_reports(parse_sta_report(sta_rpt), parse_timing_json(timing_json))

        artifacts = collect(
            spec.core, spec.job_id, spec.seed, work_dir,
            rbf=rbf if rbf.exists() else None,
            sof=None,
            log=log_path,
        )

        ok = rc == 0 and artifacts.rbf_r is not None and (timing.passed if timing else False)
        err = None if ok else f"docker rc={rc}"
        return TaskOutcome(
            ok=ok,
            seed=spec.seed,
            rbf_path=artifacts.rbf,
            rbf_r_path=artifacts.rbf_r,
            timing=timing,
            log_path=log_path,
            error=err,
            duration_s=time.time() - start,
        )


def image_exists(image: str) -> bool:
    try:
        subprocess.check_output(["docker", "image", "inspect", image], stderr=subprocess.DEVNULL)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def build_image(tag: str, dockerfile_dir: Path, *, no_cache: bool = False) -> int:
    args = ["docker", "build", "-t", tag, str(dockerfile_dir)]
    if no_cache:
        args.insert(2, "--no-cache")
    return subprocess.call(args)
