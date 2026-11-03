#!/usr/bin/env python3
"""Reject legacy CPython ABI packages before Linux FEniCS qualification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


LEGACY_PY_LABEL = re.compile(r"py3(?:1[0-3]|1[5-9])", re.I)
LEGACY_CP_ABI = re.compile(r"cp3(?:1[0-3]|1[5-9])", re.I)
PYTHON_PACKAGES = ("python", "libpython", "cpython", "python-gil")
CP314_PYTHON_PACKAGES = ("numpy", "mpi4py", "cffi")

# Current conda-forge FEniCS 0.11 Python packages target the limited Python
# API (_python_abi3_support + cpython >=3.12), although their build strings
# retain the original py312/py311 floor. The other listed packages contain
# native code/data without a Python extension. These labels are candidates
# for runtime qualification, not proof of a successful cp314 native closure.
REVIEWED_LEGACY_LABELS = {
    "fenics-basix": "0.11.0",
    "fenics-dolfinx": "0.11.0",
    "petsc4py": "3.25.6",
    "slepc4py": "3.25.2",
    "fenics-basix-nanobind-abi": "2.19",
    "fenics-libbasix": "0.11.0",
    "fenics-libdolfinx": "0.11.0",
    "kahip": "3.25",
    "icu": "78.3",
}


def check(lock: dict) -> list[str]:
    errors: list[str] = []
    by_name = {record["name"]: record for record in lock["packages"]}
    for name in PYTHON_PACKAGES:
        record = by_name.get(name)
        if record is not None and record["version"] != "3.14.10":
            errors.append(f"{name}: requires CPython 3.14.10, found {record['version']}")
    for name, version in (("python_abi", "3.14"), ("numpy", "2.5.2")):
        record = by_name.get(name)
        if record is None or record["version"] != version:
            errors.append(f"{name}: required version {version} is missing")
    abi = by_name.get("python_abi", {})
    if "cp314" not in abi.get("build", ""):
        errors.append("python_abi: requires cp314 build")
    for name in CP314_PYTHON_PACKAGES:
        record = by_name.get(name)
        if record is None:
            errors.append(f"missing required native Python package: {name}")
        elif not re.search(r"(?:py|cp)314", record["build"], re.I):
            errors.append(f"{name}: requires cp314/py314 build, found {record['build']}")
    for record in lock["packages"]:
        name = record["name"]
        if LEGACY_CP_ABI.search(record["build"]) or LEGACY_CP_ABI.search(record["url"]):
            errors.append(f"{name}: legacy CPython ABI in immutable package identity")
        if LEGACY_PY_LABEL.search(record["build"]):
            if REVIEWED_LEGACY_LABELS.get(name) != record["version"]:
                errors.append(f"{name}: unreviewed legacy Python build label {record['build']}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    args = parser.parse_args()
    errors = check(json.loads(args.lock.read_text(encoding="utf-8")))
    if errors:
        for message in errors:
            print(f"cp314_lock_blocker={message}")
        print(f"cp314_lock_status=blocked ({len(errors)} problems)")
        return 1
    print("cp314_lock_status=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
