"""Global config — paths, defaults, environment discovery.

Everything the rest of the code needs to know about the host environment
lives here. Values are cheap to compute but cached in a singleton so
multiple callers share the same view.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Literal

from platformdirs import user_cache_dir, user_data_dir, user_state_dir

# Pocket device: Cyclone V 5CEBA4F23C8N
# Reference: https://www.analogue.co/developer/docs/overview
POCKET_DEVICE = "5CEBA4F23C8"
POCKET_FAMILY = "Cyclone V"

# Default Quartus Lite version we target. Kept as a plain string so the
# install script and Docker image can parse it. 24.1 is the latest Lite
# that fully supports Cyclone V.
DEFAULT_QUARTUS_VERSION = "24.1"

# Conservative Fmax for Pocket video clock; real cores define their own SDC.
DEFAULT_TARGET_FMAX_MHZ = 74.25  # video clock


@dataclass(frozen=True)
class Paths:
    """Resolved on-disk locations for state/cache/logs."""
    state_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path
    db_path: Path
    artifacts_dir: Path

    @classmethod
    def resolve(cls) -> "Paths":
        override = os.environ.get("SUPERQ_HOME")
        if override:
            root = Path(override).expanduser().resolve()
            state = root / "state"
            data = root / "data"
            cache = root / "cache"
        else:
            state = Path(user_state_dir("super-q"))
            data = Path(user_data_dir("super-q"))
            cache = Path(user_cache_dir("super-q"))
        log = state / "logs"
        for d in (state, data, cache, log):
            d.mkdir(parents=True, exist_ok=True)
        return cls(
            state_dir=state,
            data_dir=data,
            cache_dir=cache,
            log_dir=log,
            db_path=state / "superq.db",
            artifacts_dir=data / "artifacts",
        )


@dataclass
class QuartusInstall:
    """Describes a located Quartus installation.

    `None` values mean Quartus is not installed locally — which is fine for
    pure scheduling/cloud work, but blocks local builds.
    """
    version: str | None
    root: Path | None
    bin_dir: Path | None
    sh: Path | None
    fit: Path | None
    syn: Path | None
    sta: Path | None
    cpf: Path | None

    @property
    def is_installed(self) -> bool:
        return self.sh is not None


def _find_quartus_bin() -> Path | None:
    """Locate a Quartus install. Honors $QUARTUS_ROOTDIR, PATH, then common dirs."""
    env_root = os.environ.get("QUARTUS_ROOTDIR")
    if env_root:
        p = Path(env_root) / "bin"
        if not p.exists():
            p = Path(env_root) / "quartus" / "bin"
        if (p / _exe("quartus_sh")).exists():
            return p

    which = shutil.which("quartus_sh")
    if which:
        return Path(which).parent

    guesses: list[Path] = []
    if sys.platform == "darwin":
        guesses += [
            Path("/Applications/intelFPGA_lite/24.1/quartus/bin"),
            Path("/opt/intelFPGA_lite/24.1/quartus/bin"),
        ]
    elif sys.platform.startswith("linux"):
        for base in ("/opt", "/usr/local", str(Path.home())):
            for ver in ("24.1", "23.1", "22.1"):
                guesses.append(Path(base) / "intelFPGA_lite" / ver / "quartus" / "bin")
    else:
        guesses += [
            Path("C:/intelFPGA_lite/24.1/quartus/bin64"),
            Path("C:/intelFPGA_lite/23.1/quartus/bin64"),
        ]
    for g in guesses:
        if (g / _exe("quartus_sh")).exists():
            return g
    return None


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


@cache
def quartus_install() -> QuartusInstall:
    bin_dir = _find_quartus_bin()
    if bin_dir is None:
        return QuartusInstall(None, None, None, None, None, None, None, None)
    root = bin_dir.parent
    version = _detect_quartus_version(root)
    return QuartusInstall(
        version=version,
        root=root,
        bin_dir=bin_dir,
        sh=bin_dir / _exe("quartus_sh"),
        fit=bin_dir / _exe("quartus_fit"),
        syn=bin_dir / _exe("quartus_syn"),
        sta=bin_dir / _exe("quartus_sta"),
        cpf=bin_dir / _exe("quartus_cpf"),
    )


def _detect_quartus_version(root: Path) -> str | None:
    # Quartus writes version into several files. Try the cheap ones.
    candidates = [
        root / "common" / "VERSION",
        root.parent / "VERSION",
        root / "VERSION",
    ]
    for c in candidates:
        if c.exists():
            try:
                txt = c.read_text(errors="ignore").strip()
                for line in txt.splitlines():
                    line = line.strip()
                    if line and line[0].isdigit():
                        return line.split()[0]
            except OSError:
                continue
    # Parse from directory name: .../24.1/quartus/
    parts = root.parts
    for p in parts:
        if p and p[0].isdigit() and "." in p:
            return p
    return None


@dataclass
class HostCapacity:
    """What this host can actually run in parallel."""
    cpu_count: int
    mem_gb: float
    platform_name: str
    quartus_parallel: int = field(default=0)

    def __post_init__(self) -> None:
        if self.quartus_parallel == 0:
            # Quartus fitter scales badly past ~8 threads. Leave headroom for
            # running multiple seeds concurrently: each worker can grab 2–4
            # threads, and we run cpu/threads-per-worker seeds at once.
            self.quartus_parallel = max(1, self.cpu_count // 4)


@cache
def host_capacity() -> HostCapacity:
    cpu = os.cpu_count() or 4
    mem_gb = _detect_mem_gb()
    return HostCapacity(cpu_count=cpu, mem_gb=mem_gb, platform_name=platform.platform())


def _detect_mem_gb() -> float:
    try:
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
            return int(out) / (1024**3)
        if sys.platform.startswith("linux"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb / (1024**2)
    except Exception:
        pass
    return 8.0


# ---- Public helpers ------------------------------------------------------

LogLevel = Literal["debug", "info", "warn", "error"]


def paths() -> Paths:
    return Paths.resolve()


def banner() -> str:
    q = quartus_install()
    h = host_capacity()
    qv = f"Quartus {q.version}" if q.is_installed else "Quartus: not found"
    return f"super-q · {qv} · {h.cpu_count} cpus · {h.mem_gb:.0f}GB"
