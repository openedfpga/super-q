"""Modal backend — the default remote path.

Why Modal:
  * Python-native. Agents already know the SDK. No cluster to manage.
  * True per-second billing and scale-to-zero.
  * Persistent Volumes hold Quartus's 15 GB install so cold starts are
    ~20 s instead of the 5–10 min it would take to download on boot.
  * No account onboarding friction — `modal token new` and go.

Topology:
  * A single Modal App (configurable, default: `super-q`) exposes a
    `.run_seed(core_tar, project, seed, mode, extra) -> result_tar` function.
  * The function image has Quartus Lite 24.1 baked in via a build step
    that reads the bundle from a Modal Secret or a pre-populated Volume.
  * For each task, we upload the core tar to a small Dict, call the
    function, and fetch the `.rbf_r` + timing from the returned bytes.

The Modal Function is defined in `super_q.modal_app`; this backend just
marshals TaskSpec → Modal call.

Install: `pip install super-q[modal]` (adds the `modal` SDK).
"""
from __future__ import annotations

import io
import json
import logging
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

from super_q.artifacts import reverse_rbf
from super_q.backends.base import BackendError, TaskOutcome, TaskSpec
from super_q.pool_config import PoolSpec
from super_q.timing import TimingReport

log = logging.getLogger("superq.modal")


class ModalBackend:
    name = "modal"

    def __init__(self, *, pool: PoolSpec | None = None,
                 app_name: str = "super-q",
                 max_parallel: int = 32,
                 cpu: int = 8,
                 memory_gb: int = 16,
                 timeout_s: int = 60 * 60,
                 image: str | None = None) -> None:
        try:
            import modal  # noqa: F401
        except ImportError as e:
            raise BackendError(
                "Modal backend requires the modal SDK. "
                "Install with `pip install super-q[modal]`."
            ) from e

        if pool is not None:
            app_name = pool.opt("app", app_name)
            max_parallel = pool.max_parallel or max_parallel
            cpu = int(pool.opt("cpu", cpu))
            memory_gb = int(pool.opt("memory_gb", memory_gb))
            image = pool.opt("image", image)
            timeout_s = int(pool.opt("timeout_s", timeout_s))

        self._app_name = app_name
        self._max = max_parallel
        self._cpu = cpu
        self._mem_gb = memory_gb
        self._image = image
        self._timeout = timeout_s
        self._sem = threading.Semaphore(self._max)
        self._fn = None   # resolved lazily

    def available_slots(self) -> int:
        return self._max

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "modal",
            "app": self._app_name,
            "max_parallel": self._max,
            "cpu": self._cpu,
            "memory_gb": self._mem_gb,
            "image": self._image,
        }

    def run(self, spec: TaskSpec) -> TaskOutcome:
        with self._sem:
            return self._run_inner(spec)

    # ------------------------------------------------------------------ #

    def _resolve_function(self):
        import modal
        if self._fn is not None:
            return self._fn
        # Look up the deployed app+function. Users run `modal deploy …`
        # once per Quartus release; thereafter every client machine can
        # reach it by name.
        try:
            fn = modal.Function.from_name(self._app_name, "run_seed")
        except Exception as e:
            raise BackendError(
                f"could not resolve Modal function {self._app_name}.run_seed — "
                f"have you run `modal deploy super_q.modal_app`? ({e})"
            ) from e
        self._fn = fn
        return fn

    def _run_inner(self, spec: TaskSpec) -> TaskOutcome:
        start = time.time()
        tar_bytes = _pack_core(spec)
        fn = self._resolve_function()

        payload = {
            "project": spec.core.project_name,
            "quartus_dir": str(spec.core.quartus_dir.relative_to(spec.core.root)),
            "seed": spec.seed,
            "mode": spec.mode,
            "extra": spec.extra_assignments or {},
            "threads": spec.threads,
        }

        try:
            result: dict[str, Any] = fn.remote(tar_bytes, payload)
        except Exception as e:
            return TaskOutcome(
                ok=False, seed=spec.seed, rbf_path=None, rbf_r_path=None,
                timing=None, log_path=None, error=f"modal call failed: {e}",
                duration_s=time.time() - start,
            )

        rbf_r_path = _unpack_result(result, spec)
        timing = _parse_timing(result.get("timing"))
        return TaskOutcome(
            ok=bool(result.get("ok")) and rbf_r_path is not None,
            seed=spec.seed,
            rbf_path=None,
            rbf_r_path=rbf_r_path,
            timing=timing,
            log_path=None,
            error=result.get("error"),
            duration_s=time.time() - start,
        )


# ---------------------------------------------------------------------------
# packing / unpacking
# ---------------------------------------------------------------------------


def _pack_core(spec: TaskSpec) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(
            spec.core.root, arcname=spec.core.root.name,
            filter=_scrub_tar,
        )
    return buf.getvalue()


def _scrub_tar(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    # Skip heavy build byproducts; we only need sources + .qpf/.qsf/.sdc.
    skip = {".git", ".superq", "output_files", "db", "incremental_db",
            "qdb", "__pycache__", "simulation"}
    parts = set(Path(info.name).parts)
    if skip & parts:
        return None
    return info


def _unpack_result(result: dict[str, Any], spec: TaskSpec) -> Path | None:
    blob = result.get("rbf")
    if not blob:
        return None
    if isinstance(blob, str):
        import base64
        blob = base64.b64decode(blob)
    out_dir = spec.core.superq_dir / "artifacts" / spec.job_id / f"seed-{spec.seed:04d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rbf = out_dir / "bitstream.rbf"
    rbf.write_bytes(blob)
    rbf_r = out_dir / "bitstream.rbf_r"
    reverse_rbf(rbf, rbf_r)
    log_blob = result.get("log")
    if log_blob:
        (out_dir / "build.log").write_bytes(
            log_blob.encode() if isinstance(log_blob, str) else log_blob
        )
    timing_json = result.get("timing_json")
    if timing_json:
        (out_dir / "timing.json").write_text(
            timing_json if isinstance(timing_json, str) else json.dumps(timing_json)
        )
    return rbf_r


def _parse_timing(payload: Any) -> TimingReport | None:
    if not payload:
        return None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    from super_q.timing import ClockTiming
    clocks = [
        ClockTiming(
            name=c["name"],
            setup_slack_ns=c.get("setup_slack_ns"),
            hold_slack_ns=c.get("hold_slack_ns"),
            fmax_mhz=c.get("fmax_mhz"),
            restricted_fmax_mhz=c.get("restricted_fmax_mhz"),
        )
        for c in payload.get("clocks", [])
    ]
    return TimingReport(
        passed=bool(payload.get("passed")),
        worst_setup_slack_ns=payload.get("worst_setup_slack_ns"),
        worst_hold_slack_ns=payload.get("worst_hold_slack_ns"),
        clocks=clocks,
        summary=payload.get("summary", ""),
        source="modal",
    )
