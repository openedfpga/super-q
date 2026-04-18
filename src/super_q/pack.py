"""Pocket-format release packager.

Given a core folder and a winning `.rbf_r`, produce a zip that unpacks
straight onto a Pocket SD card. Layout inside the zip:

    <Author>.<CoreName>/
        Cores/<Author>.<CoreName>/
            bitstream.rbf_r
            audio.json core.json data.json input.json interact.json
            video.json variants.json
            icon.bin
        Platforms/
            <platform>.json
            _images/<platform>.bin
        Assets/
            <platform>/<Author>.<CoreName>/…

The `dist/` folder inside the caller's repo supplies everything except
the bitstream; we drop the built `.rbf_r` in at the right place, rewrite
`core.json` with the caller's version string, and zip it up.

Same logic used by the CLI (`superq release pack`) and by
`scripts/pack_pocket.py`.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class PackError(Exception):
    pass


@dataclass
class PackResult:
    zip_path: Path
    full_name: str              # <Author>.<CoreName>
    version: str
    sha256: str
    bytes: int

    def as_dict(self) -> dict:
        return {
            "zip_path": str(self.zip_path),
            "full_name": self.full_name,
            "version": self.version,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


def detect_full_name(core_path: Path, override: str | None = None) -> str:
    if override:
        if not re.match(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", override):
            raise PackError(f"name override must be <Author>.<CoreName>, got {override!r}")
        return override

    cores_dir = core_path / "dist" / "Cores"
    if cores_dir.exists():
        subs = [p for p in cores_dir.iterdir() if p.is_dir()]
        if len(subs) == 1:
            return subs[0].name
        if len(subs) > 1:
            names = ", ".join(p.name for p in subs)
            raise PackError(
                f"multiple cores in {cores_dir}: {names}. Pass name_override to disambiguate."
            )

    # Fall back to the repo dirname if it looks like Author.Name.
    name = core_path.resolve().name
    if re.match(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$", name):
        return name
    raise PackError(
        f"can't infer <Author>.<CoreName> from {core_path}; pass name_override explicitly."
    )


def infer_version(core_path: Path) -> str:
    """Best-effort: git tag → core.json → 'dev'."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(core_path), "describe", "--tags", "--always"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        if out:
            return out.lstrip("v")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    for cj in (core_path / "dist").rglob("core.json"):
        try:
            data = json.loads(cj.read_text())
            v = data.get("core", {}).get("metadata", {}).get("version")
            if v:
                return str(v)
        except (OSError, json.JSONDecodeError):
            continue
    return "dev"


def pack(
    core_path: Path,
    rbf_r: Path,
    *,
    out_dir: Path,
    version: str | None = None,
    name_override: str | None = None,
    stamp_date: bool = True,
) -> PackResult:
    """Produce `<out_dir>/<Author>.<Name>_<version>.zip`."""
    if not core_path.exists():
        raise PackError(f"no such core path: {core_path}")
    if not rbf_r.exists():
        raise PackError(f"no such rbf_r: {rbf_r}")

    dist = core_path / "dist"
    if not dist.exists():
        raise PackError(f"no dist/ folder at {core_path}")

    full_name = detect_full_name(core_path, name_override)
    ver = (version or infer_version(core_path)).lstrip("v")

    # Drop the bitstream into Cores/<full_name>/.
    target_dir = dist / "Cores" / full_name
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rbf_r, target_dir / "bitstream.rbf_r")

    _update_core_json(target_dir / "core.json", ver, stamp_date=stamp_date)

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{full_name}_{ver}.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(dist.rglob("*")):
            if path.is_dir() or _is_ignored(path):
                continue
            rel = path.relative_to(dist)
            zf.write(path, f"{full_name}/{rel}")

    return PackResult(
        zip_path=zip_path,
        full_name=full_name,
        version=ver,
        sha256=_sha256(zip_path),
        bytes=zip_path.stat().st_size,
    )


def _update_core_json(path: Path, version: str, *, stamp_date: bool) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    meta = data.setdefault("core", {}).setdefault("metadata", {})
    meta["version"] = version
    if stamp_date and "date_release" in meta:
        meta["date_release"] = datetime.now(UTC).strftime("%Y-%m-%d")
    path.write_text(json.dumps(data, indent=2) + "\n")


def _is_ignored(path: Path) -> bool:
    name = path.name
    if name in {".DS_Store", "Thumbs.db"}:
        return True
    if name.endswith(".bak") or name.endswith("~"):
        return True
    return False


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()
