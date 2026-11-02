#!/usr/bin/env python3
"""Write machine-readable producer qualification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--check", action="append", default=[])
    args = parser.parse_args()

    artifact = Path(args.artifact).resolve()
    if not artifact.is_file():
        raise SystemExit(f"artifact does not exist: {artifact}")

    checks: dict[str, str] = {}
    for raw in args.check:
        if "=" not in raw:
            raise SystemExit(f"invalid check assignment: {raw!r}")
        name, status = raw.split("=", 1)
        if status not in {"pass", "fail", "not-applicable"}:
            raise SystemExit(f"invalid check status: {status!r}")
        checks[name] = status

    payload = {
        "format_version": 1,
        "producer_repository": "precsim/python-numpy-fenics",
        "producer_revision": os.environ.get(
            "PRODUCER_REVISION",
            os.environ.get("GITHUB_SHA", "local"),
        ),
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
        "platform": args.platform,
        "profile": args.profile,
        "artifact": artifact.name,
        "artifact_sha256": sha256_file(artifact),
        "checks": checks,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"qualification={output}")
    print(f"artifact_sha256={payload['artifact_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
