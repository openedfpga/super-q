"""Bitstream post-processing + artifact layout.

Analogue Pocket's APF loader expects a byte-reversed raw binary
(`.rbf_r`), not the `.rbf` Quartus emits. The algorithm is literally:
for each byte, mirror bits[7:0] → bits[0:7]. That's done here.

We also copy the resulting artifacts into a per-job, per-seed folder
under `<core>/.superq/artifacts/` so agents can find them by convention
without tailing logs.
"""
from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from super_q.project import PocketCore

# Lookup table: index = original byte, value = bit-reversed byte.
# Generated once at import time so conversions stay in C speed via bytes.translate.
_REVERSE_TABLE = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def reverse_rbf(src: Path, dst: Path, *, chunk: int = 1 << 20) -> int:
    """Byte-reverse every byte of `src` into `dst`. Returns bytes written.

    Streams in 1 MiB chunks so even a 30 MB Cyclone V bitstream stays in
    cache. The output filename convention is `<stem>.rbf_r`.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            buf = fi.read(chunk)
            if not buf:
                break
            fo.write(buf.translate(_REVERSE_TABLE))
            total += len(buf)
    return total


@dataclass
class ArtifactSet:
    rbf: Path | None
    rbf_r: Path | None
    sof: Path | None
    timing_json: Path | None
    sta_rpt: Path | None
    log: Path | None
    sha256: str | None


def collect(core: PocketCore, job_id: str, seed: int, work_dir: Path,
            *, rbf: Path | None, sof: Path | None,
            log: Path | None) -> ArtifactSet:
    """Move build outputs into the canonical per-seed artifact dir.

    Layout:
        <core>/.superq/artifacts/<job>/seed-<N>/
            bitstream.rbf
            bitstream.rbf_r   <- agents want this one
            bitstream.sha256
            timing.json
            <project>.sta.rpt
            build.log
    """
    out_dir = core.superq_dir / "artifacts" / job_id / f"seed-{seed:04d}"
    out_dir.mkdir(parents=True, exist_ok=True)

    rbf_dst = None
    rbf_r_dst = None
    sha = None
    if rbf and rbf.exists():
        rbf_dst = out_dir / "bitstream.rbf"
        shutil.copy2(rbf, rbf_dst)
        rbf_r_dst = out_dir / "bitstream.rbf_r"
        reverse_rbf(rbf_dst, rbf_r_dst)
        sha = _sha256(rbf_r_dst)
        (out_dir / "bitstream.sha256").write_text(f"{sha}  bitstream.rbf_r\n")

    sof_dst = None
    if sof and sof.exists():
        sof_dst = out_dir / f"{core.project_name}.sof"
        shutil.copy2(sof, sof_dst)

    timing_src = work_dir / core.quartus_dir.relative_to(core.root) / "output_files" / "timing.json"
    timing_dst = None
    if timing_src.exists():
        timing_dst = out_dir / "timing.json"
        shutil.copy2(timing_src, timing_dst)

    sta_src = work_dir / core.quartus_dir.relative_to(core.root) / "output_files" / f"{core.project_name}.sta.rpt"
    sta_dst = None
    if sta_src.exists():
        sta_dst = out_dir / f"{core.project_name}.sta.rpt"
        shutil.copy2(sta_src, sta_dst)

    log_dst = None
    if log and log.exists():
        log_dst = out_dir / "build.log"
        shutil.copy2(log, log_dst)

    # A "latest" symlink makes it trivial for agents to find the current best
    # bitstream without querying the DB.
    _write_latest_pointer(core, job_id, seed, out_dir)

    return ArtifactSet(
        rbf=rbf_dst,
        rbf_r=rbf_r_dst,
        sof=sof_dst,
        timing_json=timing_dst,
        sta_rpt=sta_dst,
        log=log_dst,
        sha256=sha,
    )


def promote_to_dist(core: PocketCore, rbf_r: Path) -> Path | None:
    """If the repo has a canonical Pocket `Cores/<Author>.<Name>/` dir,
    drop the rbf_r there as `bitstream.rbf_r`. No-op if the layout isn't
    present — we don't want to create random directories.
    """
    if core.dist_dir is None:
        return None
    candidates = [
        core.dist_dir / "Cores" / core.full_name / "bitstream.rbf_r",
        core.root / "dist" / "Cores" / core.full_name / "bitstream.rbf_r",
    ]
    for cand in candidates:
        if cand.parent.exists():
            shutil.copy2(rbf_r, cand)
            return cand
    return None


def _write_latest_pointer(core: PocketCore, job_id: str, seed: int, out_dir: Path) -> None:
    latest = core.superq_dir / "artifacts" / "latest"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(out_dir.resolve(), target_is_directory=True)
    except OSError:
        # Some filesystems (and Windows without admin) reject symlinks.
        # Write a plain text pointer instead.
        latest = core.superq_dir / "artifacts" / "LATEST"
        latest.write_text(f"{out_dir}\njob={job_id} seed={seed} ts={int(time.time())}\n")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()
