"""Modal App definition — deploy once, call from anywhere.

Deploy flow:

    # 1. Authenticate (one time per machine)
    modal token new

    # 2. Accept Intel's EULA and stash it as a Modal Secret so the build
    #    step doesn't prompt:
    modal secret create altera-eula \\
        EULA='I have read and accept the Intel Simplified Software License Agreement.'

    # 3. Deploy the App (super-q source is baked in from your local checkout):
    modal deploy super_q.modal_app

    # 4. One-time: unpack Quartus Lite into the persistent Volume.
    modal run super_q.modal_app::install_quartus \\
        --tarball=./Quartus-lite-24.1std.0.917-linux.tar

    # 5. Verify end-to-end connectivity with a no-Quartus smoke test:
    modal run super_q.modal_app::smoke_test

    # …or via super-q:
    superq modal check
    superq modal smoke
    superq modal bench ./my-core --seed=1

Cost reference (2026-Q2): 8 vCPU / 16 GiB CPU-only Modal runs are ~$0.08/hr.
A 16-seed sweep × 4 min average × 8 parallel ≈ $0.09 total.
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any

try:
    import modal
except ImportError:   # keep this module importable even without the SDK
    modal = None  # type: ignore[assignment]


# ---- build the image + volume, but only when the SDK is present -----------

_SUPER_Q_ROOT = Path(__file__).resolve().parent.parent.parent  # /super-q checkout root
_TCL_SRC = _SUPER_Q_ROOT / "tcl"


def _build_image():
    assert modal is not None
    # Ubuntu 22.04 with Quartus runtime deps + super-q installed from local src.
    # We bake in the TCL wrappers explicitly because they live alongside the
    # Python package but aren't Python modules.
    img = (
        modal.Image.from_registry("ubuntu:22.04", add_python="3.11")
        .apt_install(
            "ca-certificates", "curl", "git", "rsync", "libc6-i386",
            "libncurses5", "libncurses6", "libtinfo5", "libxft2",
            "libxrender1", "libxtst6", "libxi6", "libfreetype6",
            "libpng16-16", "libjpeg62", "unzip", "make", "coreutils",
        )
        .pip_install(
            "typer>=0.12", "rich>=13.7", "pydantic>=2.7", "anyio>=4.4",
            "platformdirs>=4.2", "httpx>=0.27", "pyyaml>=6.0",
        )
        .add_local_dir(str(_SUPER_Q_ROOT), remote_path="/opt/super-q", copy=True)
        .run_commands("pip install /opt/super-q")
        .env({"QUARTUS_ROOTDIR": "/opt/intelFPGA_lite/24.1/quartus",
              "PATH": "/opt/intelFPGA_lite/24.1/quartus/bin:/usr/local/bin:/usr/bin:/bin"})
    )
    return img


def _volume():
    assert modal is not None
    return modal.Volume.from_name("superq-quartus", create_if_missing=True)


if modal is not None:
    app = modal.App("super-q")
    image = _build_image()
    vol = _volume()

    # --------------------------------------------------------------------- #
    # smoke_test — verify Modal connectivity without needing Quartus yet.
    # --------------------------------------------------------------------- #
    @app.function(image=image, cpu=2, memory=1024, timeout=120)
    def smoke_test() -> dict[str, Any]:
        """Prove the image builds + an invocation round-trips before paying
        for the Quartus install. Takes under a minute on a warm container.
        """
        import platform
        import time as _t

        start = _t.time()
        cpu_line = ""
        try:
            with open("/proc/cpuinfo") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        cpu_line = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass

        # Confirm super-q imports inside the container.
        import super_q  # noqa: F401
        from super_q.config import quartus_install

        quartus = quartus_install()
        tcl_files = sorted(Path("/opt/super-q/tcl").glob("*.tcl"))
        return {
            "ok": True,
            "hostname": platform.node(),
            "cpu": cpu_line,
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
            "super_q_version": super_q.__version__,
            "quartus_installed": quartus.is_installed,
            "quartus_version": quartus.version,
            "tcl_wrappers": [p.name for p in tcl_files],
            "boot_s": round(_t.time() - start, 2),
        }

    # --------------------------------------------------------------------- #
    # install_quartus — one-time: unpack the Quartus installer into the Volume.
    # --------------------------------------------------------------------- #
    @app.function(
        image=image,
        volumes={"/opt/intelFPGA_lite/24.1": vol},
        cpu=4, memory=8192, timeout=60 * 60 * 2,
    )
    def install_quartus(
        tarball_bytes: bytes,
        eula_accepted: bool = False,
        version: str = "24.1",
    ) -> dict[str, Any]:
        """Unpack Quartus Lite into the persistent Volume.

        `eula_accepted` must be True — caller is attesting that they've
        read and accepted Intel's license at
        https://www.intel.com/content/www/us/en/legal/end-user-license-agreement.html.
        super-q itself does not ship Intel binaries.
        """
        if not eula_accepted:
            raise RuntimeError(
                "eula_accepted=False: you must read and accept Intel's EULA before installing Quartus."
            )
        work = Path(tempfile.mkdtemp(prefix="quartus-install-"))
        tarfile_path = work / "q.tar"
        tarfile_path.write_bytes(tarball_bytes)
        subprocess.check_call(["tar", "-xf", str(tarfile_path)], cwd=work)
        setup = next(work.rglob("setup.sh"))
        setup.chmod(0o755)
        subprocess.check_call([
            str(setup), "--mode", "unattended", "--accept_eula", "1",
            "--installdir", f"/opt/intelFPGA_lite/{version}",
            "--disable-components", "quartus_help",
        ])
        # Persist changes to the Volume.
        vol.commit()
        bin_dir = Path(f"/opt/intelFPGA_lite/{version}/quartus/bin")
        return {
            "ok": bin_dir.exists(),
            "bin_dir": str(bin_dir),
            "executables": sorted(p.name for p in bin_dir.glob("quartus_*")),
        }

    # --------------------------------------------------------------------- #
    # run_seed — the hot path. One seed → one bitstream + timing.
    # --------------------------------------------------------------------- #
    @app.function(
        image=image,
        volumes={"/opt/intelFPGA_lite/24.1": vol},
        cpu=8, memory=16 * 1024, timeout=60 * 60,
    )
    def run_seed(core_tar: bytes, payload: dict[str, Any]) -> dict[str, Any]:
        import time as _t

        start = _t.time()
        work = Path(tempfile.mkdtemp(prefix="superq-"))
        (work / "in.tar.gz").write_bytes(core_tar)
        with tarfile.open(work / "in.tar.gz", "r:gz") as tar:
            tar.extractall(work)

        core_root = next(p for p in work.iterdir() if p.is_dir())
        qdir = core_root / payload["quartus_dir"]
        project = payload["project"]
        seed = int(payload["seed"])

        env = os.environ.copy()
        env["SUPER_Q_SEED"] = str(seed)
        env["SUPER_Q_PROJECT"] = project
        env["QUARTUS_ROOTDIR"] = "/opt/intelFPGA_lite/24.1/quartus"
        env["PATH"] = f"{env['QUARTUS_ROOTDIR']}/bin:{env.get('PATH', '')}"
        env["QUARTUS_NUM_PARALLEL_PROCESSORS"] = str(payload.get("threads") or 4)
        if payload.get("extra"):
            env["SUPER_Q_EXTRA"] = ";".join(
                f"{k}={v}" for k, v in payload["extra"].items()
            )

        tcl = "build_seed.tcl" if payload.get("mode", "full") == "full" else "fit_from_qdb.tcl"
        tcl_path = Path("/opt/super-q/tcl") / tcl
        log_buf = io.StringIO()
        proc = subprocess.run(
            [f"{env['QUARTUS_ROOTDIR']}/bin/quartus_sh", "-t", str(tcl_path), project],
            cwd=qdir, env=env, capture_output=True, text=True,
        )
        log_buf.write(proc.stdout or "")
        log_buf.write(proc.stderr or "")

        rbf = qdir / "output_files" / f"{project}.rbf"
        timing_json_path = qdir / "output_files" / "timing.json"

        result: dict[str, Any] = {
            "ok": proc.returncode == 0 and rbf.exists(),
            "seed": seed,
            "duration_s": round(_t.time() - start, 2),
            "log": log_buf.getvalue()[-50_000:],
            "error": None if proc.returncode == 0 else f"quartus rc={proc.returncode}",
        }
        if rbf.exists():
            result["rbf"] = base64.b64encode(rbf.read_bytes()).decode()
        if timing_json_path.exists():
            try:
                result["timing"] = json.loads(timing_json_path.read_text())
                result["timing_json"] = timing_json_path.read_text()
            except json.JSONDecodeError:
                pass
        return result
