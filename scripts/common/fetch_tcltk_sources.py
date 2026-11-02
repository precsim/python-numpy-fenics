#!/usr/bin/env python3
"""Fetch the producer-owned Tcl/Tk sources from the immutable core lock."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import urllib.request


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", default="locks/core/tcltk-8.6.18.json")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    lock_path = Path(args.lock).resolve()
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or payload.get("version") != "8.6.18":
        raise SystemExit("Tcl/Tk lock identity is invalid")
    sources = payload.get("sources")
    if not isinstance(sources, dict) or set(sources) != {"tcl", "tk"}:
        raise SystemExit("Tcl/Tk lock source set is invalid")

    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for component in ("tcl", "tk"):
        record = sources[component]
        filename = record.get("filename")
        url = record.get("url")
        expected = record.get("sha256")
        if not all(isinstance(value, str) and value for value in (filename, url, expected)):
            raise SystemExit(f"invalid {component} lock record")
        target = output / filename
        request = urllib.request.Request(url, headers={"User-Agent": "python-numpy-fenics-ci"})
        with urllib.request.urlopen(request) as response, target.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        actual = sha256(target)
        if actual != expected:
            target.unlink(missing_ok=True)
            raise SystemExit(f"{component} source SHA-256 mismatch: {actual} != {expected}")
        print(f"{component}_source={target}")
        print(f"{component}_sha256={actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
