"""`~/.superq/config.toml` — named remote worker pools.

This file gives users (and agents) a consistent way to address remote
compute without stuffing env vars into every command. A typical layout:

    [pool.modal]
    kind = "modal"
    app = "super-q"
    max_parallel = 32
    image = "registry.modal.com/you/super-q:24.1"
    cpu = 8
    memory_gb = 16

    [pool.fly]
    kind = "fly"
    app = "super-q-build"
    size = "performance-4x"
    region = "iad"
    image = "registry.fly.io/you/super-q:24.1"
    max_parallel = 8

    [pool.homelab]
    kind = "ssh"
    hosts = ["build1.local", "build2.local"]
    user = "superq"
    slots_per_host = 4
    quartus_root = "/opt/intelFPGA_lite/24.1/quartus"

    [pool.gha]
    kind = "gha"
    repo = "you/pocket-ci"
    workflow = "build-core.yml"
    branch = "main"

    [default]
    pool = "modal"

Pools never contain secrets — tokens come from the environment
(`MODAL_TOKEN_ID`, `FLY_API_TOKEN`, `GH_TOKEN`, `SSH_AUTH_SOCK`).
"""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from super_q.config import paths


class PoolConfigError(Exception):
    pass


@dataclass
class PoolSpec:
    name: str
    kind: str                       # 'local' | 'modal' | 'fly' | 'ssh' | 'gha' | 'docker' | 'aws'
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def max_parallel(self) -> int:
        return int(self.raw.get("max_parallel", 4))

    @property
    def threads_per_task(self) -> int:
        return int(self.raw.get("threads", 2))

    def opt(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)


def config_path() -> Path:
    override = os.environ.get("SUPERQ_CONFIG")
    if override:
        return Path(override).expanduser()
    return paths().state_dir / "config.toml"


def load() -> dict[str, PoolSpec]:
    """Parse `config.toml`, returning `name → PoolSpec`. Empty dict if absent."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise PoolConfigError(f"{path}: {e}") from e

    pools: dict[str, PoolSpec] = {}
    for name, body in (data.get("pool") or {}).items():
        kind = body.get("kind")
        if not kind:
            raise PoolConfigError(f"pool '{name}' missing kind=")
        pools[name] = PoolSpec(name=name, kind=kind, raw=body)
    return pools


def default_pool_name() -> str | None:
    """Named pool to use when `--pool` is omitted. Env beats config."""
    env = os.environ.get("SUPERQ_POOL")
    if env:
        return env
    path = config_path()
    if not path.exists():
        return None
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError:
        return None
    return (data.get("default") or {}).get("pool")


def resolve_backend(pool_name: str | None):
    """Turn a pool name (or None for default) into a ready `Backend`.

    Agents can bypass this and pass `--backend=local` directly; the pool
    mechanism is for named remote setups.
    """
    from super_q.backends import get_backend

    pools = load()
    name = pool_name or default_pool_name()

    if name is None:
        return get_backend("local")

    spec = pools.get(name)
    if spec is None:
        # Allow bare backend names as shortcuts: `--pool=local`, `--pool=modal`.
        if name in {"local", "docker", "modal", "fly", "ssh", "gha", "aws"}:
            return get_backend(name)
        raise PoolConfigError(f"no pool named '{name}' in {config_path()}")

    return get_backend(spec.kind, pool=spec)


# ---- helpers for the CLI `remote` subcommand --------------------------------

_EXAMPLE = b"""\
# ~/.superq/config.toml - named remote worker pools.
# Edit and set a `default.pool` to make `superq sweep .` remote by default.

[pool.modal]
kind = "modal"
app = "super-q"
max_parallel = 32
cpu = 8
memory_gb = 16

[pool.fly]
kind = "fly"
app = "super-q-build"
size = "performance-4x"
region = "iad"
max_parallel = 8

[pool.homelab]
kind = "ssh"
hosts = ["build1.local", "build2.local"]
user = "superq"
slots_per_host = 4

[pool.gha]
kind = "gha"
repo = "you/pocket-ci"
workflow = "build-core.yml"
branch = "main"

[default]
# pool = "modal"   # uncomment to default to a remote pool
"""


def write_example(force: bool = False) -> Path:
    path = config_path()
    if path.exists() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_EXAMPLE)
    return path


def describe(pools: dict[str, PoolSpec] | None = None) -> dict[str, Any]:
    pools = pools or load()
    return {
        "config_path": str(config_path()),
        "default_pool": default_pool_name(),
        "pools": [
            {"name": p.name, "kind": p.kind, "max_parallel": p.max_parallel, **p.raw}
            for p in pools.values()
        ],
    }


# Typer shouldn't complain if tomllib is missing on <3.11; we require 3.11+.
assert sys.version_info >= (3, 11), "super-q requires Python 3.11+"
