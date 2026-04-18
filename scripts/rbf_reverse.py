#!/usr/bin/env python3
"""Stand-alone Quartus .rbf → Analogue Pocket .rbf_r converter.

super-q uses this internally, but it's usable on its own in case you just
want the byte-reversal step without installing the rest of the package:

    python3 rbf_reverse.py output_files/proj.rbf output_files/proj.rbf_r

The algorithm is the canonical APF one: mirror bits[7:0] to bits[0:7]
for every byte. Streamed in 1 MiB chunks so it's O(file size) memory.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


_TABLE = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def reverse(src: Path, dst: Path, *, chunk: int = 1 << 20) -> int:
    total = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            buf = fi.read(chunk)
            if not buf:
                break
            fo.write(buf.translate(_TABLE))
            total += len(buf)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Byte-reverse a Quartus .rbf into .rbf_r")
    ap.add_argument("src", type=Path, help="input .rbf file")
    ap.add_argument("dst", type=Path, nargs="?", help="output .rbf_r (default: src with .rbf_r suffix)")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    dst = args.dst or args.src.with_suffix(".rbf_r")
    if dst.exists() and not args.overwrite:
        print(f"refusing to overwrite {dst} (pass --overwrite)", file=sys.stderr)
        return 2
    n = reverse(args.src, dst)
    print(f"wrote {dst} ({n} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
