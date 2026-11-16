#!/usr/bin/env python3
"""Populate the non-runtime files for an already staged reference test layout."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil


ALIASES = {
    "win-x64": "bridge.mexw64",
    "linux-x64": "bridge.mexa64",
    "macos-arm64": "bridge.mexmaca64",
    "macos-x64": "bridge.mexmaci64",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_exact(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise SystemExit(f"missing layout input: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if sha256(source) != sha256(destination):
        raise SystemExit(f"layout copy changed bytes: {destination}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--mex", type=Path, required=True)
    parser.add_argument("--platform", choices=sorted(ALIASES), required=True)
    parser.add_argument("--profile", choices=("core", "numpy", "fenics"), required=True)
    parser.add_argument("--runtime-record", type=Path, required=True)
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--host-fixture", type=Path, required=True)
    args = parser.parse_args()

    root = args.package_root.resolve()
    runtime = root / "runtime"
    if not runtime.is_dir():
        raise SystemExit(f"staged runtime is missing from test layout: {runtime}")
    copy_exact(args.mex.resolve(), root / "bridge.mex")
    copy_exact(args.mex.resolve(), root / ALIASES[args.platform])
    copy_exact(args.runtime_record.resolve(), root / "evidence" / "runtime-receipt.json")
    copy_exact(args.backend.resolve(), root / "python" / "mock_backend.py")
    copy_exact(args.host_fixture.resolve(), root / "host" / "mock_smoke.m")
    (root / "profile.txt").write_text(args.profile + "\n", encoding="utf-8", newline="\n")
    print(f"reference_test_layout={root}")
    print(f"reference_test_mex_sha256={sha256(root / 'bridge.mex')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
