#!/usr/bin/env python3
"""Explicitly acquire, verify, and transactionally stage a producer runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_lib import (  # noqa: E402
    ConsumerRuntimeError,
    PLATFORMS,
    PROFILES,
    prepare_runtime_inputs,
    stage_runtime,
    write_evidence_record,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--platform", choices=sorted(PLATFORMS), required=True)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--record",
        type=Path,
        help="Write deterministic staged-runtime evidence JSON.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Require every pinned producer asset to exist in the local immutable cache.",
    )
    args = parser.parse_args()
    try:
        inputs = prepare_runtime_inputs(
            args.receipt,
            platform=args.platform,
            profile=args.profile,
            cache=args.cache.resolve(),
            offline=args.offline,
        )
        result = stage_runtime(inputs, args.output)
        if args.record is not None:
            write_evidence_record(args.record, result)
    except ConsumerRuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
