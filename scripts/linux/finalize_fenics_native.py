#!/usr/bin/env python3
"""Finalize a Linux FEniCS native-build candidate after closure audit."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.linux.rebuild_fenics_native import InputError, finalize_candidate_build_handoff


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-build-handoff", type=Path, required=True)
    parser.add_argument("--native-closure-report", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="package handoff path; defaults under the candidate evidence directory",
    )
    args = parser.parse_args(argv)

    try:
        destination, payload = finalize_candidate_build_handoff(
            args.candidate_build_handoff,
            args.native_closure_report,
            args.output,
        )
        print(f"package_handoff={destination}")
        print(f"package_handoff_status={payload['status']}")
        print(f"replacement_count={len(payload['replacements'])}")
        print("source_rebuild_provenance_eligible=false")
        return 0
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
