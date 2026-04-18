"""SSH pool backend — bring your own Linux boxes.

The simplest remote path. Agents just list a handful of SSH-accessible
Linux hosts (home lab, dedicated server at Hetzner, a Mac mini colo'd
somewhere) and super-q fans seeds out to them over SSH.

For each task we:
    1. rsync the core sandbox to `host:/tmp/superq-<job>-<seed>/`
    2. ssh in and run `super-q-worker one-shot …`
    3. rsync the result back
    4. delete the remote sandbox

Requires just `ssh` and `rsync` on PATH locally. Hosts need:
    * OpenSSH server
    * Quartus Lite on PATH (or `quartus_root` set in config)
    * `super-q-worker` installed (`pip install super-q`)

Concurrency: each host can run `slots_per_host` seeds at once. With 3
hosts × 4 slots, 12 seeds run in parallel. No queue — we pick any host
with a free slot.
"""
from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from super_q.artifacts import reverse_rbf
from super_q.backends.base import BackendError, TaskOutcome, TaskSpec
from super_q.pool_config import PoolSpec
from super_q.timing import merge_reports, parse_sta_report, parse_timing_json

log = logging.getLogger("superq.ssh")


@dataclass
class SshHost:
    host: str
    user: str = ""
    port: int = 22
    slots: int = 4
    quartus_root: str | None = None
    sem: threading.Semaphore | None = None

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host


class SshBackend:
    name = "ssh"

    def __init__(self, *, pool: PoolSpec | None = None, **kw) -> None:
        if shutil.which("ssh") is None or shutil.which("rsync") is None:
            raise BackendError("ssh backend needs ssh + rsync on PATH")

        raw = dict(pool.raw) if pool else {}
        raw.update(kw)
        hosts = raw.get("hosts") or []
        if not hosts:
            raise BackendError("ssh backend needs 'hosts' list")

        user = raw.get("user", "")
        port = int(raw.get("port", 22))
        slots = int(raw.get("slots_per_host", 4))
        q_root = raw.get("quartus_root")

        self._hosts: list[SshHost] = []
        for h in hosts:
            if isinstance(h, str):
                self._hosts.append(SshHost(
                    host=h, user=user, port=port, slots=slots, quartus_root=q_root,
                    sem=threading.Semaphore(slots),
                ))
            else:  # dict with host-specific overrides
                self._hosts.append(SshHost(
                    host=h["host"],
                    user=h.get("user", user),
                    port=int(h.get("port", port)),
                    slots=int(h.get("slots", slots)),
                    quartus_root=h.get("quartus_root", q_root),
                    sem=threading.Semaphore(int(h.get("slots", slots))),
                ))

        self._total_slots = sum(h.slots for h in self._hosts)
        self._pick_lock = threading.Lock()
        self._round_robin = 0

    def available_slots(self) -> int:
        return self._total_slots

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "ssh",
            "total_slots": self._total_slots,
            "hosts": [
                {"host": h.host, "user": h.user, "slots": h.slots,
                 "quartus_root": h.quartus_root}
                for h in self._hosts
            ],
        }

    def run(self, spec: TaskSpec) -> TaskOutcome:
        host = self._pick_host()
        assert host.sem is not None
        with host.sem:
            return self._run_on(host, spec)

    # ------------------------------------------------------------------ #

    def _pick_host(self) -> SshHost:
        """Round-robin pick among hosts with at least one idle slot."""
        with self._pick_lock:
            n = len(self._hosts)
            for i in range(n):
                h = self._hosts[(self._round_robin + i) % n]
                # _value is a CPython implementation detail but good enough as a hint
                if h.sem is not None and h.sem._value > 0:  # type: ignore[attr-defined]
                    self._round_robin = (self._round_robin + i + 1) % n
                    return h
            # All busy — fall back to the next in rotation (will block on sem).
            h = self._hosts[self._round_robin % n]
            self._round_robin += 1
            return h

    def _run_on(self, host: SshHost, spec: TaskSpec) -> TaskOutcome:
        start = time.time()
        remote_dir = f"/tmp/superq-{spec.job_id}-s{spec.seed:04d}"
        qdir_rel = str(spec.core.quartus_dir.relative_to(spec.core.root))
        project = spec.core.project_name

        log_path = spec.work_dir / "superq.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._rsync_to(host, spec.core.root, remote_dir)
            rc = self._ssh_run(
                host,
                self._remote_cmd(host, remote_dir, spec, qdir_rel, project),
                log_path=log_path,
                timeout_s=spec.timeout_s,
            )
            # Pull artifacts back even if the build failed (useful for debugging).
            self._rsync_back(host, remote_dir, spec.work_dir)
        finally:
            # Best-effort cleanup of the remote sandbox.
            self._ssh_run(host, f"rm -rf {shlex.quote(remote_dir)}", log_path=log_path,
                          timeout_s=60, check=False)

        local_qdir = spec.work_dir / Path(spec.core.root.name) / qdir_rel
        rbf = local_qdir / "output_files" / f"{project}.rbf"
        sta = local_qdir / "output_files" / f"{project}.sta.rpt"
        timing_json = local_qdir / "output_files" / "timing.json"
        timing = merge_reports(parse_sta_report(sta), parse_timing_json(timing_json))

        rbf_r_path: Path | None = None
        if rbf.exists():
            out_dir = spec.core.superq_dir / "artifacts" / spec.job_id / f"seed-{spec.seed:04d}"
            out_dir.mkdir(parents=True, exist_ok=True)
            rbf_dst = out_dir / "bitstream.rbf"
            shutil.copy2(rbf, rbf_dst)
            rbf_r_path = out_dir / "bitstream.rbf_r"
            reverse_rbf(rbf_dst, rbf_r_path)

        ok = rc == 0 and rbf_r_path is not None and (timing.passed if timing else False)
        return TaskOutcome(
            ok=ok,
            seed=spec.seed,
            rbf_path=rbf if rbf.exists() else None,
            rbf_r_path=rbf_r_path,
            timing=timing,
            log_path=log_path,
            error=None if ok else f"ssh host {host.host}: rc={rc}",
            duration_s=time.time() - start,
        )

    # ------------------------------------------------------------------ #

    def _rsync_to(self, host: SshHost, src: Path, remote_dir: str) -> None:
        self._rsync([
            "rsync", "-az", "--delete",
            "--exclude=.git", "--exclude=.superq", "--exclude=output_files",
            "--exclude=db", "--exclude=incremental_db",
            "-e", self._ssh_cmd(host),
            f"{src}/", f"{host.target}:{remote_dir}/",
        ])

    def _rsync_back(self, host: SshHost, remote_dir: str, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        self._rsync([
            "rsync", "-az",
            "--include=**/output_files/**", "--include=*/", "--exclude=*",
            "-e", self._ssh_cmd(host),
            f"{host.target}:{remote_dir}/", f"{dst}/{Path(remote_dir).name}/",
        ], check=False)
        # pull the whole output_files tree (simpler than nested includes)
        self._rsync([
            "rsync", "-az",
            "-e", self._ssh_cmd(host),
            f"{host.target}:{remote_dir}/", f"{dst}/",
        ], check=False)

    def _rsync(self, args: list[str], *, check: bool = True) -> int:
        log.debug("rsync: %s", " ".join(args))
        r = subprocess.run(args, check=False)
        if check and r.returncode != 0:
            raise BackendError(f"rsync failed (rc={r.returncode}): {' '.join(args)}")
        return r.returncode

    def _ssh_cmd(self, host: SshHost) -> str:
        base = ["ssh", "-p", str(host.port), "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new"]
        return " ".join(shlex.quote(a) for a in base)

    def _ssh_run(self, host: SshHost, cmd: str, *, log_path: Path,
                 timeout_s: int, check: bool = True) -> int:
        ssh = ["ssh", "-p", str(host.port), "-o", "BatchMode=yes",
               host.target, cmd]
        log.debug("ssh[%s]: %s", host.host, cmd)
        with open(log_path, "a", buffering=1) as lf:
            lf.write(f"\n# ssh {host.host}: {cmd}\n")
            try:
                proc = subprocess.run(
                    ssh, stdout=lf, stderr=subprocess.STDOUT,
                    timeout=timeout_s, check=False,
                )
            except subprocess.TimeoutExpired:
                lf.write(f"# TIMEOUT {timeout_s}s\n")
                return 124
        if check and proc.returncode != 0:
            log.error("ssh %s rc=%s", host.host, proc.returncode)
        return proc.returncode

    def _remote_cmd(self, host: SshHost, remote_dir: str, spec: TaskSpec,
                    qdir_rel: str, project: str) -> str:
        env = []
        if host.quartus_root:
            env.append(f'export QUARTUS_ROOTDIR={shlex.quote(host.quartus_root)}')
            env.append('export PATH="$QUARTUS_ROOTDIR/bin:$PATH"')
        env.append(f"export SUPER_Q_SEED={spec.seed}")

        extra = ""
        if spec.extra_assignments:
            s = ";".join(f"{k}={v}" for k, v in spec.extra_assignments.items())
            env.append(f"export SUPER_Q_EXTRA={shlex.quote(s)}")

        return "; ".join([
            "set -e",
            *env,
            f"cd {shlex.quote(remote_dir)}",
            f"super-q-worker one-shot --project={shlex.quote(project)} "
            f"--quartus-dir={shlex.quote(qdir_rel)} --seed={spec.seed} "
            f"--output result.json",
        ]) + extra
