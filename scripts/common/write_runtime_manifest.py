#!/usr/bin/env python3
"""Write the public producer runtime-manifest v1 for a staged runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--profile", choices=("core", "numpy", "fenics"), required=True)
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--output")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        raise SystemExit(f"runtime root does not exist: {root}")
    output = Path(args.output).resolve() if args.output else root / "runtime-manifest.json"
    if root != output.parent and root not in output.parents:
        raise SystemExit("runtime manifest output must be inside the runtime root")

    entries = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path == output:
            continue
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append(
                {
                    "path": rel,
                    "kind": "symlink",
                    "target": os.readlink(path),
                    "owners": ["python-numpy-fenics"],
                }
            )
        elif path.is_file():
            entries.append(
                {
                    "path": rel,
                    "kind": "file",
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                    "owners": ["python-numpy-fenics"],
                }
            )

    payload = {
        "format_version": 1,
        "artifact": f"python-runtime-{args.platform}-cp312-{args.profile}",
        "version": args.version,
        "platform": args.platform,
        "python_abi": "cp312",
        "profile": args.profile,
        "producer": {
            "repository": "precsim/python-numpy-fenics",
            "revision": os.environ.get("GITHUB_SHA", "local"),
        },
        "files": entries,
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"runtime_manifest={output}")
    print(f"runtime_manifest_entries={len(entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
