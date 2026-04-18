"""Fly.io Machines backend — ephemeral VMs per task.

Fly's Machines API is a great fit for bursty FPGA builds:
  * create → run → stop → destroy in a single REST round-trip
  * global regions (build close to the user)
  * per-second billing, so a 4-minute build costs single-digit cents
  * pre-baked Docker images start in ~3 s on warm hosts

We don't run a long-lived Fly app. Instead, we boot a fresh Machine per
task with a self-destructing init script that:

  1. downloads the core tar from a URL we sign (Tigris / R2 / any S3-compat)
  2. runs `super-q-worker one-shot`
  3. uploads the result tar back
  4. exits (Machine then stops; `auto_destroy=true` frees it)

Requires `$FLY_API_TOKEN`. Install via `pip install super-q[fly]`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from super_q.artifacts import reverse_rbf
from super_q.backends.base import BackendError, TaskOutcome, TaskSpec
from super_q.pool_config import PoolSpec

log = logging.getLogger("superq.fly")

_FLY_API = "https://api.machines.dev/v1"


@dataclass
class FlyConfig:
    app: str
    image: str
    region: str = "iad"
    size: str = "performance-4x"   # 4 vCPU, 8 GB
    disk_gb: int = 40
    max_parallel: int = 8
    timeout_s: int = 60 * 60
    artifact_bucket: str = ""        # s3-compatible (Tigris, R2)
    artifact_endpoint: str = ""      # e.g. https://fly.storage.tigris.dev


class FlyBackend:
    name = "fly"

    def __init__(self, *, pool: PoolSpec | None = None, **kw) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError as e:
            raise BackendError(
                "Fly backend requires httpx. Install with `pip install super-q[fly]`."
            ) from e

        # Merge pool config over any kwargs the caller passed explicitly.
        raw = dict(pool.raw) if pool else {}
        raw.update(kw)
        if "app" not in raw:
            raise BackendError("Fly backend needs 'app' set in its pool config")
        if "image" not in raw:
            raise BackendError("Fly backend needs 'image' pointing at your Quartus Docker image")

        self._cfg = FlyConfig(
            app=raw["app"],
            image=raw["image"],
            region=raw.get("region", "iad"),
            size=raw.get("size", "performance-4x"),
            disk_gb=int(raw.get("disk_gb", 40)),
            max_parallel=int(raw.get("max_parallel", 8)),
            timeout_s=int(raw.get("timeout_s", 60 * 60)),
            artifact_bucket=raw.get("artifact_bucket", ""),
            artifact_endpoint=raw.get("artifact_endpoint", ""),
        )
        self._token = os.environ.get("FLY_API_TOKEN")
        if not self._token:
            raise BackendError("FLY_API_TOKEN not set in environment")
        self._sem = threading.Semaphore(self._cfg.max_parallel)

    def available_slots(self) -> int:
        return self._cfg.max_parallel

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "fly",
            "app": self._cfg.app,
            "region": self._cfg.region,
            "size": self._cfg.size,
            "image": self._cfg.image,
            "max_parallel": self._cfg.max_parallel,
        }

    def run(self, spec: TaskSpec) -> TaskOutcome:
        with self._sem:
            return self._run_inner(spec)

    # ------------------------------------------------------------------ #

    def _run_inner(self, spec: TaskSpec) -> TaskOutcome:
        import httpx
        start = time.time()

        if not self._cfg.artifact_bucket:
            raise BackendError("Fly backend needs artifact_bucket for input/output")

        # 1. Upload the sandbox tar to object storage.
        input_url = _upload_sandbox(self._cfg, spec)
        output_key = f"jobs/{spec.job_id}/seed-{spec.seed:04d}/result.json"
        rbf_key    = f"jobs/{spec.job_id}/seed-{spec.seed:04d}/bitstream.rbf"

        # 2. Create an auto-destroying Machine that runs the task.
        init = _render_init_script(self._cfg, spec, input_url, output_key, rbf_key)
        body = {
            "config": {
                "image": self._cfg.image,
                "auto_destroy": True,
                "guest": _size_to_guest(self._cfg.size),
                "size": self._cfg.size,
                "env": {
                    "SUPER_Q_SEED": str(spec.seed),
                    "SUPER_Q_PROJECT": spec.core.project_name,
                },
                "processes": [
                    {
                        "name": "build",
                        "entrypoint": ["/bin/bash", "-lc"],
                        "cmd": [init],
                    }
                ],
                "restart": {"policy": "no"},
            },
            "region": self._cfg.region,
        }
        headers = {"Authorization": f"Bearer {self._token}"}
        with httpx.Client(timeout=60) as client:
            r = client.post(f"{_FLY_API}/apps/{self._cfg.app}/machines",
                            json=body, headers=headers)
            if r.status_code >= 300:
                return self._err(spec, f"fly create failed ({r.status_code}): {r.text[:400]}", start)
            machine_id = r.json()["id"]

        # 3. Wait for the result file.
        result = _poll_object(self._cfg, output_key, timeout_s=spec.timeout_s)
        if result is None:
            return self._err(spec, f"fly machine {machine_id} timed out", start)

        # 4. Pull the bitstream + reverse it locally.
        rbf_bytes = _fetch_object(self._cfg, rbf_key)
        rbf_r_path: Path | None = None
        if rbf_bytes:
            out_dir = spec.core.superq_dir / "artifacts" / spec.job_id / f"seed-{spec.seed:04d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "bitstream.rbf").write_bytes(rbf_bytes)
            rbf_r_path = out_dir / "bitstream.rbf_r"
            reverse_rbf(out_dir / "bitstream.rbf", rbf_r_path)

        ok = bool(result.get("ok")) and rbf_r_path is not None
        return TaskOutcome(
            ok=ok,
            seed=spec.seed,
            rbf_path=None,
            rbf_r_path=rbf_r_path,
            timing=None,
            log_path=None,
            error=result.get("error"),
            duration_s=time.time() - start,
        )

    def _err(self, spec: TaskSpec, msg: str, start: float) -> TaskOutcome:
        log.error(msg)
        return TaskOutcome(
            ok=False, seed=spec.seed, rbf_path=None, rbf_r_path=None,
            timing=None, log_path=None, error=msg,
            duration_s=time.time() - start,
        )


# ---------------------------------------------------------------------------
# object storage glue (Tigris/R2 use S3-compatible API)
# ---------------------------------------------------------------------------


def _upload_sandbox(cfg: FlyConfig, spec: TaskSpec) -> str:
    s3 = _s3(cfg)
    key = f"jobs/{spec.job_id}/seed-{spec.seed:04d}/input.tar.gz"
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(spec.core.root, arcname=spec.core.root.name)
    s3.put_object(Bucket=cfg.artifact_bucket, Key=key, Body=buf.getvalue())

    # Signed GET URL so the Machine can pull without AWS creds.
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": cfg.artifact_bucket, "Key": key},
        ExpiresIn=60 * 60 * 4,
    )


def _poll_object(cfg: FlyConfig, key: str, *, timeout_s: int, poll_s: float = 5.0):
    s3 = _s3(cfg)
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            obj = s3.get_object(Bucket=cfg.artifact_bucket, Key=key)
            return json.loads(obj["Body"].read())
        except s3.exceptions.NoSuchKey:
            time.sleep(poll_s)
        except Exception as e:  # NoSuchKey shape varies by backend
            if "NoSuchKey" in type(e).__name__ or "404" in str(e):
                time.sleep(poll_s)
                continue
            raise
    return None


def _fetch_object(cfg: FlyConfig, key: str) -> bytes | None:
    s3 = _s3(cfg)
    try:
        return s3.get_object(Bucket=cfg.artifact_bucket, Key=key)["Body"].read()
    except Exception:
        return None


def _s3(cfg: FlyConfig):
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=cfg.artifact_endpoint or None,
        region_name=os.environ.get("AWS_REGION", "auto"),
    )


# ---------------------------------------------------------------------------
# init script + sizing
# ---------------------------------------------------------------------------


def _render_init_script(cfg: FlyConfig, spec: TaskSpec, input_url: str,
                        output_key: str, rbf_key: str) -> str:
    quartus_dir = str(spec.core.quartus_dir.relative_to(spec.core.root))
    return f"""set -euxo pipefail
cd /tmp
curl -fL "{input_url}" -o input.tar.gz
tar -xzf input.tar.gz
cd {spec.core.root.name}
export SUPER_Q_SEED={spec.seed}
super-q-worker one-shot --project={spec.core.project_name} --quartus-dir={quartus_dir} --output result.json || true

python3 -c '
import json, os, sys, base64, subprocess
res = json.load(open("result.json"))
subprocess.check_call(["curl", "-fL", "-X", "PUT", os.environ["SUPERQ_OUTPUT_URL"],
                       "-H", "Content-Type: application/json",
                       "--data-binary", json.dumps(res)])
if os.path.exists(res.get("rbf_path", "") or ""):
    subprocess.check_call(["curl", "-fL", "-X", "PUT", os.environ["SUPERQ_RBF_URL"],
                           "--data-binary", "@" + res["rbf_path"]])
'
"""


def _size_to_guest(size: str) -> dict[str, Any]:
    # Map Fly size presets onto the {cpu_kind, cpus, memory_mb} the API expects.
    table = {
        "performance-2x":  {"cpu_kind": "performance", "cpus": 2,  "memory_mb": 4_096},
        "performance-4x":  {"cpu_kind": "performance", "cpus": 4,  "memory_mb": 8_192},
        "performance-8x":  {"cpu_kind": "performance", "cpus": 8,  "memory_mb": 16_384},
        "performance-16x": {"cpu_kind": "performance", "cpus": 16, "memory_mb": 32_768},
    }
    return table.get(size, table["performance-4x"])
