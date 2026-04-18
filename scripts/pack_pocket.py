#!/usr/bin/env python3
"""Thin CLI shim around super_q.pack — produces a release zip.

Use this if you want to invoke the packager without installing the full
super-q CLI (e.g. inside a minimal container). If super-q is installed,
`superq release pack` is exactly equivalent.

    python3 scripts/pack_pocket.py \\
        --core-path ./my-core \\
        --rbf-r ./my-core/.superq/artifacts/latest/bitstream.rbf_r \\
        --version 0.3.1 \\
        --out-dir ./release
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    # We import lazily so `--help` works even if super_q isn't on sys.path yet.
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--core-path", type=Path, required=True)
    ap.add_argument("--rbf-r", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, default=Path("release"))
    ap.add_argument("--version", default=None)
    ap.add_argument("--name", dest="name_override", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    # Make `src/super_q` importable when running from a checkout.
    src_dir = Path(__file__).resolve().parent.parent / "src"
    if src_dir.exists() and str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    try:
        from super_q.pack import PackError, pack
    except ImportError as e:
        print(f"cannot import super_q.pack — install super-q first: {e}", file=sys.stderr)
        return 2

    try:
        result = pack(
            args.core_path, args.rbf_r,
            out_dir=args.out_dir,
            version=args.version,
            name_override=args.name_override,
        )
    except PackError as e:
        print(f"pack failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(f"packed {result.full_name} v{result.version}")
        print(f"  {result.zip_path}  ({result.bytes/1024/1024:.2f} MB)")
        print(f"  sha256 {result.sha256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
