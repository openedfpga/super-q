"""GitHub Actions dispatch backend — CI minutes as compute.

For public repos this is essentially free (2000 GHA minutes/month on
free tier + unlimited for public projects). It's also the most natural
choice if your cores live on GitHub anyway: the same workflow that runs
CI is the one that builds your seeds, artifacts come back through the
normal Actions UI, and no extra infrastructure is required.

How it works:
  1. We POST a workflow_dispatch with `{core_archive_url, seed, mode}` inputs.
  2. The workflow runs `super-q-worker one-shot …` on a GitHub-hosted runner.
  3. The job uploads the bitstream as a workflow artifact named
     `superq-<job>-seed-<NNNN>`.
  4. We poll `actions/runs/:id/artifacts` and download it locally.

Requires `$GH_TOKEN` with `workflow` scope and either a public repo
(for anonymous artifact hosting) or write access. The workflow file
`.github/workflows/build-core.yml` is shipped in this repo; adapt
`repo:` and `workflow:` in your pool config to point at your fork.
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

log = logging.getLogger("superq.gha")


@dataclass
class GhaConfig:
    repo: str              # "owner/name"
    workflow: str          # "build-core.yml"
    ref: str = "main"
    max_parallel: int = 20
    timeout_s: int = 60 * 60
    artifact_bucket: str = ""
    artifact_endpoint: str = ""


class GhaBackend:
    name = "gha"

    def __init__(self, *, pool: PoolSpec | None = None, **kw) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError as e:
            raise BackendError(
                "GHA backend requires httpx. Install with `pip install super-q[gha]`."
            ) from e
        raw = dict(pool.raw) if pool else {}
        raw.update(kw)
        if "repo" not in raw or "workflow" not in raw:
            raise BackendError("gha backend needs 'repo' and 'workflow' in config")

        self._cfg = GhaConfig(
            repo=raw["repo"],
            workflow=raw["workflow"],
            ref=raw.get("branch", raw.get("ref", "main")),
            max_parallel=int(raw.get("max_parallel", 20)),
            timeout_s=int(raw.get("timeout_s", 60 * 60)),
            artifact_bucket=raw.get("artifact_bucket", ""),
            artifact_endpoint=raw.get("artifact_endpoint", ""),
        )
        self._token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not self._token:
            raise BackendError("GH_TOKEN not set in environment")
        self._sem = threading.Semaphore(self._cfg.max_parallel)

    def available_slots(self) -> int:
        return self._cfg.max_parallel

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "gha",
            "repo": self._cfg.repo,
            "workflow": self._cfg.workflow,
            "ref": self._cfg.ref,
            "max_parallel": self._cfg.max_parallel,
        }

    def run(self, spec: TaskSpec) -> TaskOutcome:
        with self._sem:
            return self._run_inner(spec)

    # ------------------------------------------------------------------ #

    def _run_inner(self, spec: TaskSpec) -> TaskOutcome:
        start = time.time()

        archive_url = _publish_sandbox(self._cfg, spec)
        run_id = self._dispatch_workflow(spec, archive_url)
        ok, err = self._await_run(run_id, spec.timeout_s)

        rbf_r_path: Path | None = None
        if ok:
            rbf_r_path = self._download_artifact(run_id, spec)
            if rbf_r_path is None:
                ok = False
                err = "workflow finished but no super-q artifact found"

        return TaskOutcome(
            ok=ok,
            seed=spec.seed,
            rbf_path=None,
            rbf_r_path=rbf_r_path,
            timing=None,
            log_path=None,
            error=err,
            duration_s=time.time() - start,
        )

    def _dispatch_workflow(self, spec: TaskSpec, archive_url: str) -> int:
        import httpx
        body = {
            "ref": self._cfg.ref,
            "inputs": {
                "archive_url": archive_url,
                "project": spec.core.project_name,
                "quartus_dir": str(spec.core.quartus_dir.relative_to(spec.core.root)),
                "seed": str(spec.seed),
                "mode": spec.mode,
                "job_id": spec.job_id,
            },
        }
        url = (f"https://api.github.com/repos/{self._cfg.repo}/actions/"
               f"workflows/{self._cfg.workflow}/dispatches")
        h = {"Authorization": f"Bearer {self._token}",
             "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        with httpx.Client(timeout=60) as client:
            r = client.post(url, json=body, headers=h)
            if r.status_code >= 300:
                raise BackendError(f"dispatch failed {r.status_code}: {r.text[:400]}")

            # GitHub doesn't return the run id from dispatches — find it by polling.
            for _ in range(30):
                runs = client.get(
                    f"https://api.github.com/repos/{self._cfg.repo}/actions/runs",
                    params={"event": "workflow_dispatch", "per_page": 20},
                    headers=h,
                ).json().get("workflow_runs", [])
                for run in runs:
                    name = run.get("name") or ""
                    # Match by the job_id we embedded in the run title.
                    if spec.job_id in name or spec.job_id in json.dumps(run.get("head_commit") or {}):
                        return run["id"]
                time.sleep(2)
        raise BackendError("workflow dispatched but run id not discoverable")

    def _await_run(self, run_id: int, timeout_s: int) -> tuple[bool, str | None]:
        import httpx
        h = {"Authorization": f"Bearer {self._token}",
             "Accept": "application/vnd.github+json"}
        start = time.time()
        with httpx.Client(timeout=30) as client:
            while time.time() - start < timeout_s:
                r = client.get(
                    f"https://api.github.com/repos/{self._cfg.repo}/actions/runs/{run_id}",
                    headers=h,
                ).json()
                status = r.get("status")
                if status == "completed":
                    conc = r.get("conclusion")
                    if conc == "success":
                        return True, None
                    return False, f"gha run {run_id} {conc}"
                time.sleep(5)
        return False, f"gha run {run_id} timed out"

    def _download_artifact(self, run_id: int, spec: TaskSpec) -> Path | None:
        import httpx
        h = {"Authorization": f"Bearer {self._token}",
             "Accept": "application/vnd.github+json"}
        with httpx.Client(timeout=60) as client:
            arts = client.get(
                f"https://api.github.com/repos/{self._cfg.repo}/actions/runs/{run_id}/artifacts",
                headers=h,
            ).json().get("artifacts", [])
            target = None
            for a in arts:
                if spec.job_id in a.get("name", "") and str(spec.seed) in a["name"]:
                    target = a
                    break
            if not target:
                return None

            zip_bytes = client.get(target["archive_download_url"],
                                   headers=h, follow_redirects=True).content

        import io
        import zipfile
        out_dir = spec.core.superq_dir / "artifacts" / spec.job_id / f"seed-{spec.seed:04d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            z.extractall(out_dir)
        rbf_candidate = next(out_dir.rglob("*.rbf"), None)
        if rbf_candidate is None:
            return None
        rbf_dst = out_dir / "bitstream.rbf"
        if rbf_candidate != rbf_dst:
            rbf_candidate.rename(rbf_dst)
        rbf_r = out_dir / "bitstream.rbf_r"
        reverse_rbf(rbf_dst, rbf_r)
        return rbf_r


def _publish_sandbox(cfg: GhaConfig, spec: TaskSpec) -> str:
    """Upload the core tar to a URL the runner can GET."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(spec.core.root, arcname=spec.core.root.name)
    payload = buf.getvalue()

    if cfg.artifact_bucket:
        import boto3
        s3 = boto3.client("s3", endpoint_url=cfg.artifact_endpoint or None,
                          region_name=os.environ.get("AWS_REGION", "auto"))
        key = f"jobs/{spec.job_id}/seed-{spec.seed:04d}/input.tar.gz"
        s3.put_object(Bucket=cfg.artifact_bucket, Key=key, Body=payload)
        return s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": cfg.artifact_bucket, "Key": key},
            ExpiresIn=60 * 60 * 4,
        )

    # No bucket configured: fall back to a GitHub Gist (works but slow, capped at 10 MB).
    raise BackendError(
        "GHA backend needs `artifact_bucket` (Tigris/R2/S3) configured in the pool — "
        "the runner pulls the core tar from it. Public-repo cores can alternatively "
        "commit+push a tag instead; see docs/ci.md."
    )
