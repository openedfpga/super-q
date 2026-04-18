"""AWS EC2 spot backend.

Fires up short-lived compute-optimized spot instances with a pre-baked
AMI (Quartus 24.1 + this package). Each instance runs `super-q-worker
--backend=aws-spot` which pulls tasks from the shared SQLite DB
(synced via a lightweight mutation log over SSM or through a
small coordinator endpoint the user exposes).

This module is intentionally lightweight — the heavy lifting (AMI prep,
IAM, VPC wiring) lives in `cloud/terraform/`. See `docs/cloud.md`.

Requires `boto3`; install via `pip install super-q[aws]`.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from super_q.backends.base import BackendError, TaskOutcome, TaskSpec


@dataclass
class AwsConfig:
    region: str = "us-east-2"
    ami_id: str = ""                     # superq-quartus-24.1-<date>
    instance_type: str = "c7i.4xlarge"   # 16 vCPU, 32 GB — good for 4 parallel seeds
    subnet_id: str = ""
    security_group_id: str = ""
    iam_instance_profile: str = "superq-worker"
    key_name: str | None = None
    s3_bucket: str = ""                  # artifacts upload target
    ssh_user: str = "ec2-user"
    max_spot_price_usd: float = 0.40
    shutdown_minutes: int = 10           # auto-terminate idle instances

    @classmethod
    def from_env(cls) -> "AwsConfig":
        return cls(
            region=os.environ.get("SUPERQ_AWS_REGION", "us-east-2"),
            ami_id=os.environ.get("SUPERQ_AWS_AMI", ""),
            instance_type=os.environ.get("SUPERQ_AWS_INSTANCE", "c7i.4xlarge"),
            subnet_id=os.environ.get("SUPERQ_AWS_SUBNET", ""),
            security_group_id=os.environ.get("SUPERQ_AWS_SG", ""),
            iam_instance_profile=os.environ.get("SUPERQ_AWS_IAM", "superq-worker"),
            key_name=os.environ.get("SUPERQ_AWS_KEY") or None,
            s3_bucket=os.environ.get("SUPERQ_AWS_BUCKET", ""),
            ssh_user=os.environ.get("SUPERQ_AWS_SSH_USER", "ec2-user"),
            max_spot_price_usd=float(os.environ.get("SUPERQ_AWS_MAX_SPOT", "0.40")),
            shutdown_minutes=int(os.environ.get("SUPERQ_AWS_SHUTDOWN_MIN", "10")),
        )


class AwsBackend:
    name = "aws"

    def __init__(self, *, config: AwsConfig | None = None, max_parallel: int = 8,
                 threads_per_task: int = 4):
        try:
            import boto3  # noqa: F401
        except ImportError as e:
            raise BackendError(
                "AWS backend requires boto3. Install with `pip install super-q[aws]`."
            ) from e
        self._cfg = config or AwsConfig.from_env()
        self._max = max_parallel
        self._threads = threads_per_task
        self._validate_config()
        self._ec2 = None   # lazy

    def _validate_config(self) -> None:
        missing = [
            f for f in ("ami_id", "subnet_id", "security_group_id", "s3_bucket")
            if not getattr(self._cfg, f)
        ]
        if missing:
            raise BackendError(
                "AWS backend missing config: " + ", ".join(missing) +
                ". Set via env vars (SUPERQ_AWS_*) or construct AwsConfig explicitly."
            )

    def _client(self):
        import boto3
        if self._ec2 is None:
            self._ec2 = boto3.client("ec2", region_name=self._cfg.region)
        return self._ec2

    def available_slots(self) -> int:
        return self._max

    def describe(self) -> dict:
        return {
            "backend": "aws",
            "region": self._cfg.region,
            "ami_id": self._cfg.ami_id,
            "instance_type": self._cfg.instance_type,
            "max_parallel": self._max,
            "threads_per_task": self._threads,
            "s3_bucket": self._cfg.s3_bucket,
            "max_spot_price_usd": self._cfg.max_spot_price_usd,
        }

    def run(self, spec: TaskSpec) -> TaskOutcome:
        """Request a spot instance, ship the sandbox via S3, wait for result.

        The heavy flow is:
          1. Upload the core sandbox (tar.gz) to s3://bucket/jobs/<job>/seed-<N>.tar.gz
          2. Request a spot instance with user-data that:
               a. downloads + untars the sandbox
               b. runs `super-q-worker --one-shot --seed=<N>`
               c. uploads the result tar.gz back to S3
               d. terminates itself
          3. Poll S3 for the result tarball
          4. Unpack locally and collect artifacts
        """
        # Implementation note: we build the user-data script and spot request
        # here but defer the actual polling to keep this file self-contained.
        start = time.time()
        job_key = f"jobs/{spec.job_id}/seed-{spec.seed:04d}"
        self._upload_sandbox(spec, job_key)
        instance_id = self._launch_spot(spec, job_key)

        result_key = f"{job_key}/result.json"
        outcome_json = self._wait_for_result(result_key, timeout_s=spec.timeout_s)
        if outcome_json is None:
            return TaskOutcome(
                ok=False, seed=spec.seed, rbf_path=None, rbf_r_path=None,
                timing=None, log_path=None,
                error=f"AWS spot task timed out on {instance_id}",
                duration_s=time.time() - start,
            )

        rbf_r = self._download_artifact(job_key, spec.core.superq_dir)
        return TaskOutcome(
            ok=bool(outcome_json.get("ok")),
            seed=spec.seed,
            rbf_path=None,
            rbf_r_path=rbf_r,
            timing=None,
            log_path=None,
            error=outcome_json.get("error"),
            duration_s=time.time() - start,
        )

    # ----- helpers (stubs the agent can extend) --------------------------

    def _upload_sandbox(self, spec: TaskSpec, key: str) -> None:
        import tarfile
        import tempfile

        import boto3
        s3 = boto3.client("s3", region_name=self._cfg.region)
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tf:
            with tarfile.open(tf.name, "w:gz") as tar:
                tar.add(spec.core.root, arcname=spec.core.root.name)
            s3.upload_file(tf.name, self._cfg.s3_bucket, f"{key}/input.tar.gz")

    def _launch_spot(self, spec: TaskSpec, key: str) -> str:
        ec2 = self._client()
        user_data = self._render_user_data(spec, key)
        resp = ec2.run_instances(
            ImageId=self._cfg.ami_id,
            InstanceType=self._cfg.instance_type,
            MinCount=1,
            MaxCount=1,
            SubnetId=self._cfg.subnet_id,
            SecurityGroupIds=[self._cfg.security_group_id],
            IamInstanceProfile={"Name": self._cfg.iam_instance_profile},
            KeyName=self._cfg.key_name or None,
            InstanceMarketOptions={
                "MarketType": "spot",
                "SpotOptions": {
                    "MaxPrice": f"{self._cfg.max_spot_price_usd:.2f}",
                    "SpotInstanceType": "one-time",
                    "InstanceInterruptionBehavior": "terminate",
                },
            },
            UserData=user_data,
            InstanceInitiatedShutdownBehavior="terminate",
            TagSpecifications=[{
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Name", "Value": f"superq-{spec.job_id}-s{spec.seed}"},
                    {"Key": "superq:job", "Value": spec.job_id},
                    {"Key": "superq:seed", "Value": str(spec.seed)},
                ],
            }],
        )
        return resp["Instances"][0]["InstanceId"]

    def _render_user_data(self, spec: TaskSpec, key: str) -> str:
        # Minimal cloud-init. The AMI already has Quartus + super-q installed.
        return f"""#!/bin/bash
set -euo pipefail
WORK=/tmp/superq
mkdir -p $WORK && cd $WORK
aws s3 cp s3://{self._cfg.s3_bucket}/{key}/input.tar.gz input.tar.gz
tar -xzf input.tar.gz
cd {spec.core.root.name}
export SUPER_Q_SEED={spec.seed}
super-q-worker one-shot \\
    --project={spec.core.project_name} \\
    --quartus-dir={spec.core.quartus_dir.relative_to(spec.core.root)} \\
    --output-key={key} \\
    --bucket={self._cfg.s3_bucket}
shutdown -h +{self._cfg.shutdown_minutes}
"""

    def _wait_for_result(self, key: str, *, timeout_s: int, poll_s: float = 5.0) -> dict | None:
        import boto3
        s3 = boto3.client("s3", region_name=self._cfg.region)
        start = time.time()
        while time.time() - start < timeout_s:
            try:
                obj = s3.get_object(Bucket=self._cfg.s3_bucket, Key=key)
                return json.loads(obj["Body"].read())
            except s3.exceptions.NoSuchKey:
                time.sleep(poll_s)
        return None

    def _download_artifact(self, key: str, dest_dir: Path) -> Path | None:
        import boto3
        s3 = boto3.client("s3", region_name=self._cfg.region)
        dest = dest_dir / "artifacts" / "aws" / f"{key.replace('/', '_')}.rbf_r"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            s3.download_file(self._cfg.s3_bucket, f"{key}/bitstream.rbf_r", str(dest))
            return dest
        except Exception:
            return None
