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
    parser.add_argument("--component-provenance")
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
        "artifact": f"python-runtime-{args.platform}-cp314-{args.profile}",
        "version": args.version,
        "platform": args.platform,
        "python_abi": "cp314",
        "profile": args.profile,
        "producer": {
            "repository": "precsim/python-numpy-fenics",
            "revision": os.environ.get(
                "PRODUCER_REVISION",
                os.environ.get("GITHUB_SHA", "local"),
            ),
        },
        "files": entries,
    }
    if args.component_provenance:
        provenance = Path(args.component_provenance).resolve()
        if not provenance.is_file() or root not in provenance.parents:
            raise SystemExit("component provenance must be an existing file inside the runtime root")
        data = json.loads(provenance.read_text(encoding="utf-8-sig"))
        tcl_tk = data.get("tcl_tk")
        if not isinstance(tcl_tk, dict):
            raise SystemExit("component provenance has no Tcl/Tk record")
        version = tcl_tk.get("version")
        if version is None:
            versions = {tcl_tk.get("tcl_version"), tcl_tk.get("tk_version")}
            if versions == {"8.6.18"}:
                version = "8.6.18"
        if version != "8.6.18" or tcl_tk.get("ownership") != "producer":
            raise SystemExit("component provenance does not identify Tcl/Tk 8.6.18")
        payload["tcl_tk"] = {
            "version": "8.6.18",
            "ownership": "producer",
            "provenance_path": provenance.relative_to(root).as_posix(),
            "provenance_sha256": sha256(provenance),
        }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"runtime_manifest={output}")
    print(f"runtime_manifest_entries={len(entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
