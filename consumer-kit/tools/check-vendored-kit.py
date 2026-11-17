#!/usr/bin/env python3
"""Compute or verify a deterministic identity for a vendored consumer kit."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


IGNORED_PARTS = {"__pycache__", ".git"}


def tree_sha256(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in IGNORED_PARTS for part in relative.parts) or path.is_dir():
            continue
        if path.is_symlink():
            identity = path.readlink().as_posix().encode("utf-8")
            kind = b"L"
        elif path.is_file():
            identity = hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii")
            kind = b"F"
        else:
            raise SystemExit(f"unsupported vendored kit entry: {relative.as_posix()}")
        digest.update(kind + b"\0" + relative.as_posix().encode("utf-8") + b"\0" + identity + b"\n")
        count += 1
    return digest.hexdigest(), count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expect")
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        raise SystemExit(f"consumer kit root is missing: {root}")
    digest, count = tree_sha256(root)
    if args.expect and digest != args.expect.lower():
        raise SystemExit(f"consumer kit tree SHA-256 mismatch: expected {args.expect}, got {digest}")
    print(f"consumer_kit_tree_sha256={digest}")
    print(f"consumer_kit_files={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
