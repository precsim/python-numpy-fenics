#!/usr/bin/env python3
"""Prove that the offline package verifier detects modified executable bytes.

Only use on an already-tested disposable extraction directory. The published
archive and producer runtime are never modified by this negative test.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--verifier", type=Path, default=Path(__file__).with_name("verify-package.py"))
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    candidate = root / "bridge.mex"
    if not candidate.is_file() or candidate.is_symlink():
        raise SystemExit("disposable extracted package has no regular bridge.mex")

    # An append avoids platform-dependent file offset behavior and tests exact
    # inventory/hash verification without tampering with producer-owned bytes.
    with candidate.open("ab") as stream:
        stream.write(b"\nreference bridge tamper probe\n")
    result = subprocess.run(
        [sys.executable, str(args.verifier.resolve(strict=True)), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 or "reference_package_result=fail" not in result.stdout:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        raise SystemExit("offline verifier did not reject altered MEX bytes")
    print("reference_bridge_tamper_detection=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
