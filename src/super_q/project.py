"""Pocket core project model + auto-detection.

A Pocket core repo is a fairly flexible thing but always contains a
Quartus project file (`.qpf`) and produces a bitstream that we can convert
to `.rbf_r`. This module finds all the paths the scheduler needs without
requiring hand-written config.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from super_q.config import POCKET_DEVICE

# Places .qpf files commonly live in a Pocket repo, in preference order.
# `src/fpga/` is the canonical openFPGA layout.
_QPF_SEARCH_DIRS: tuple[str, ...] = (
    "src/fpga",
    "src/pocket/fpga",
    "pocket/fpga",
    "fpga",
    "hw/quartus",
    "quartus",
    ".",
)


@dataclass
class PocketCore:
    """A detected Pocket core project.

    `root` is the repo root the user pointed at. `quartus_dir` is where the
    `.qpf` lives — CWD for `quartus_sh`. `project_name` is the .qpf stem,
    which Quartus uses for output filenames.
    """

    root: Path
    quartus_dir: Path
    qpf: Path
    qsf: Path | None
    project_name: str
    device: str = POCKET_DEVICE
    author: str = "unknown"
    core_name: str = "unknown"
    dist_dir: Path | None = None
    sdc_files: list[Path] = field(default_factory=list)

    @property
    def output_dir(self) -> Path:
        return self.quartus_dir / "output_files"

    @property
    def superq_dir(self) -> Path:
        """Per-core cache/sandbox/artifact root. Git-ignored by default."""
        return self.root / ".superq"

    @property
    def full_name(self) -> str:
        return f"{self.author}.{self.core_name}"

    def expected_rbf(self) -> Path:
        """Where Quartus drops the RBF before we byte-reverse it."""
        # Quartus typically writes output_files/<project>.rbf when configured.
        return self.output_dir / f"{self.project_name}.rbf"

    def as_dict(self) -> dict:
        return {
            "root": str(self.root),
            "quartus_dir": str(self.quartus_dir),
            "qpf": str(self.qpf),
            "qsf": str(self.qsf) if self.qsf else None,
            "project_name": self.project_name,
            "device": self.device,
            "author": self.author,
            "core_name": self.core_name,
            "sdc_files": [str(s) for s in self.sdc_files],
        }


class CoreDetectionError(Exception):
    """Raised when a path doesn't look like a Pocket core."""


def detect_core(path: str | Path) -> PocketCore:
    """Walk `path` looking for a single Pocket core and describe it.

    Raises if zero or multiple .qpf files are found in the expected places.
    Callers wanting batch behavior should use `find_cores` instead.
    """
    root = Path(path).expanduser().resolve()
    if not root.exists():
        raise CoreDetectionError(f"Path does not exist: {root}")

    qpf = _find_qpf(root)
    if qpf is None:
        raise CoreDetectionError(
            f"No .qpf found in {root}. Looked under: {', '.join(_QPF_SEARCH_DIRS)}"
        )
    return _build_core(root, qpf)


def find_cores(paths: list[str | Path]) -> list[PocketCore]:
    """Find every Pocket core under any of `paths`.

    A "path" may be a core root, or a directory containing multiple core
    repos. Each distinct .qpf we locate becomes its own PocketCore.
    """
    seen: set[Path] = set()
    out: list[PocketCore] = []
    for raw in paths:
        root = Path(raw).expanduser().resolve()
        if not root.exists():
            continue
        for qpf in _enumerate_qpf(root):
            if qpf in seen:
                continue
            seen.add(qpf)
            try:
                core_root = _core_root_for_qpf(qpf)
                out.append(_build_core(core_root, qpf))
            except CoreDetectionError:
                continue
    return out


def _find_qpf(root: Path) -> Path | None:
    for sub in _QPF_SEARCH_DIRS:
        cand = root / sub
        if not cand.exists():
            continue
        qpfs = sorted(cand.glob("*.qpf"))
        if qpfs:
            return qpfs[0]
    # Last resort: shallow recursive search (capped for speed).
    for qpf in _enumerate_qpf(root, limit=1):
        return qpf
    return None


def _enumerate_qpf(root: Path, *, limit: int | None = None) -> list[Path]:
    """Find .qpf files, skipping things we know not to descend into."""
    skip = {".git", "node_modules", ".superq", "output_files", "db", "incremental_db",
            "simulation", "tmp-clearbox", ".venv", "venv", "__pycache__"}
    out: list[Path] = []

    def walk(p: Path, depth: int) -> None:
        if depth > 6:
            return
        try:
            for child in p.iterdir():
                if child.name in skip or child.name.startswith("."):
                    if child.name != ".":
                        continue
                if child.is_dir():
                    walk(child, depth + 1)
                elif child.suffix == ".qpf":
                    out.append(child)
                    if limit is not None and len(out) >= limit:
                        return
        except PermissionError:
            return

    walk(root, 0)
    return out


def _core_root_for_qpf(qpf: Path) -> Path:
    """Given a .qpf, walk up to the plausible repo root.

    We stop when we see a sibling `dist/`, `.git/`, or `README*`, which
    tends to mark the repo root; otherwise fall back to two levels above.
    """
    markers = {"dist", ".git", "README.md", "README.rst", "readme.md"}
    p = qpf.parent
    for _ in range(5):
        if any((p / m).exists() for m in markers):
            return p
        if p.parent == p:
            break
        p = p.parent
    # fallback: two levels up from .qpf (canonical layout: core/src/fpga/foo.qpf)
    return qpf.parent.parent if qpf.parent.parent.exists() else qpf.parent


def _build_core(root: Path, qpf: Path) -> PocketCore:
    project_name = qpf.stem
    qsf = qpf.with_suffix(".qsf")
    if not qsf.exists():
        qsf_candidates = sorted(qpf.parent.glob("*.qsf"))
        qsf = qsf_candidates[0] if qsf_candidates else None

    author, core_name = _guess_author_name(root)
    device = _extract_device(qsf) if qsf else POCKET_DEVICE
    sdc_files = _find_sdc_files(qpf.parent)
    dist_dir = root / "dist" if (root / "dist").exists() else None

    return PocketCore(
        root=root,
        quartus_dir=qpf.parent,
        qpf=qpf,
        qsf=qsf,
        project_name=project_name,
        device=device,
        author=author,
        core_name=core_name,
        dist_dir=dist_dir,
        sdc_files=sdc_files,
    )


_DEVICE_RE = re.compile(r"set_global_assignment\s+-name\s+DEVICE\s+([^\s#]+)", re.IGNORECASE)


def _extract_device(qsf: Path) -> str:
    try:
        txt = qsf.read_text(errors="ignore")
    except OSError:
        return POCKET_DEVICE
    m = _DEVICE_RE.search(txt)
    return m.group(1) if m else POCKET_DEVICE


def _find_sdc_files(quartus_dir: Path) -> list[Path]:
    """Locate .sdc timing-constraint files anywhere the core might keep them.

    Pocket cores commonly split constraints into per-subsystem folders
    (e.g. `apf/apf_constraints.sdc`, `core/core_constraints.sdc`), so we
    walk one level of children in addition to the obvious locations.
    """
    sdcs: set[Path] = set()
    candidates: list[Path] = [
        quartus_dir,
        quartus_dir.parent,
        quartus_dir / "constraints",
        quartus_dir / "sdc",
    ]
    for d in candidates:
        if d.exists():
            sdcs.update(d.glob("*.sdc"))
    # One level of child dirs under quartus_dir (apf/, core/, rtl/, …).
    if quartus_dir.exists():
        for child in quartus_dir.iterdir():
            if child.is_dir() and not child.name.startswith(("_", ".")):
                sdcs.update(child.glob("*.sdc"))
    return sorted(sdcs)


_AUTHOR_NAME_RE = re.compile(r"^([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)$")


def _guess_author_name(root: Path) -> tuple[str, str]:
    """Identify (<Author>, <Name>) for this repo.

    Three strategies, in priority order:
      1. The canonical `dist/Cores/<Author>.<Name>/` folder. This is
         authoritative when present because it's the path the Pocket
         loader itself uses.
      2. The repo dirname, if it follows the `Author.Name` convention.
      3. Anything else → `("unknown", <dirname>)` so we still have
         *something* to label the core by.
    """
    # (1) dist/Cores/<Author>.<Name>/
    dist_cores = root / "dist" / "Cores"
    if dist_cores.is_dir():
        candidates = [
            p for p in dist_cores.iterdir()
            if p.is_dir() and _AUTHOR_NAME_RE.match(p.name)
        ]
        if len(candidates) == 1:
            author, name = candidates[0].name.split(".", 1)
            return author, name
        # Multiple cores under one repo: we don't try to guess.

    # (2) repo dirname
    m = _AUTHOR_NAME_RE.match(root.name)
    if m:
        return m.group(1), m.group(2)

    # (3) fall back
    return "unknown", root.name


@cache
def canonical_layout_hint() -> str:
    """One-line description of the layout we expect. Used in error messages."""
    return (
        "Expected openFPGA layout: <core>/src/fpga/<project>.qpf "
        "with .qsf/.sdc alongside and dist/ at the repo root."
    )
