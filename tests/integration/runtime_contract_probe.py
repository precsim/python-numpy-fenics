#!/usr/bin/env python3
"""Small public consumer mock for the runtime activation contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("core", "numpy", "fenics"), required=True)
    parser.add_argument("--runtime-root", required=True)
    args = parser.parse_args()

    root = Path(args.runtime_root).resolve()
    if Path(sys.prefix).resolve() != root:
        raise SystemExit(f"probe must run from relocated runtime: {sys.prefix} != {root}")

    result: dict[str, object] = {
        "profile": args.profile,
        "runtime_root": str(root),
        "python": sys.version.split()[0],
    }

    if args.profile in {"numpy", "fenics"}:
        import numpy

        result["numpy"] = numpy.__version__
        result["numpy_file"] = str(Path(numpy.__file__).resolve())

    if args.profile == "fenics":
        import fenics_embed_runtime

        activated = Path(fenics_embed_runtime.activate()).resolve()
        if activated != root:
            raise SystemExit(f"bootstrap activated unexpected root: {activated} != {root}")
        import dolfinx
        import petsc4py

        result["dolfinx"] = dolfinx.__version__
        result["petsc4py"] = petsc4py.__version__

    print(json.dumps(result, sort_keys=True))
    print("runtime_contract_probe=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
