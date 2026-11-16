#!/usr/bin/env python3
"""Write hash-bound reference bridge qualification evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--profile", choices=("core", "numpy", "fenics"), required=True)
    parser.add_argument("--mex", type=Path, required=True)
    parser.add_argument("--build-provenance", type=Path, required=True)
    parser.add_argument("--runtime-receipt", type=Path, required=True)
    parser.add_argument("--native-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="append", default=[])
    args = parser.parse_args()
    checks: dict[str, str] = {}
    for raw in args.check:
        if "=" not in raw:
            raise SystemExit(f"invalid check: {raw}")
        name, value = raw.split("=", 1)
        if not name or value not in {"pass", "not-applicable"} or name in checks:
            raise SystemExit(f"invalid or duplicate check: {raw}")
        checks[name] = value
    if not checks:
        raise SystemExit("at least one qualification check is required")

    mex_sha = sha256(args.mex)
    build = json.loads(args.build_provenance.read_text(encoding="utf-8"))
    receipt = json.loads(args.runtime_receipt.read_text(encoding="utf-8"))
    audit = json.loads(args.native_audit.read_text(encoding="utf-8"))
    if build.get("mex_sha256") != mex_sha:
        raise SystemExit("build provenance MEX SHA-256 differs from candidate")
    if audit.get("target") != args.platform:
        raise SystemExit("native audit target differs from qualification platform")
    if receipt.get("platform") != args.platform or receipt.get("profile") != args.profile:
        raise SystemExit("runtime receipt identity differs from qualification")

    record = {
        "format_version": 1,
        "producer_repository": "precsim/python-numpy-fenics",
        "source_revision": os.environ.get("GITHUB_SHA", "local"),
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "local"),
        "platform": args.platform,
        "profile": args.profile,
        "python_abi": "cp314",
        "mex_sha256": mex_sha,
        "build_provenance_sha256": sha256(args.build_provenance),
        "runtime_receipt_sha256": sha256(args.runtime_receipt),
        "native_audit_sha256": sha256(args.native_audit),
        "checks": checks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"reference_bridge_qualification={args.output}")
    print(f"reference_bridge_mex_sha256={mex_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
