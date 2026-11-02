#!/usr/bin/env python3
"""Static repository-policy validation for the public runtime producer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
GENERATED_ROOTS = {
    ".git",
    ".migration-source",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "out",
    "tmp",
}

ACTION_USE = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
PINNED_ACTION = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+@[a-fA-F0-9]{40}\Z")


def unpinned_actions(workflow: str) -> list[str]:
    return [
        action
        for action in ACTION_USE.findall(workflow)
        if not action.startswith("./") and not PINNED_ACTION.fullmatch(action)
    ]


def private_source_directories(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory, child_dirs, _ in os.walk(root):
        parent = Path(directory)
        for name in child_dirs:
            if name.casefold() == "private":
                found.append((parent / name).relative_to(root))
        if parent == root:
            child_dirs[:] = [name for name in child_dirs if name not in GENERATED_ROOTS]
    return found


def main() -> int:
    errors: list[str] = []

    if (ROOT / ".circleci").exists():
        errors.append("CircleCI configuration is forbidden; use GitHub Actions")

    for path in (ROOT / "src", ROOT / "private"):
        if path.exists():
            errors.append(
                f"private-consumer source root must not exist: {path.relative_to(ROOT)}"
            )

    for relative in private_source_directories(ROOT):
        errors.append(f"nested private source directory must not exist: {relative}")

    required = [
        ROOT / ".github" / "workflows" / "validate.yml",
        ROOT / ".github" / "workflows" / "qualify-windows-fenics.yml",
        ROOT / ".github" / "workflows" / "qualify-windows-core-numpy.yml",
        ROOT / ".github" / "workflows" / "qualify-macos-fenics.yml",
        ROOT / ".github" / "workflows" / "qualify-macos-core-numpy.yml",
        ROOT / ".github" / "workflows" / "qualify-linux-core-numpy.yml",
        ROOT / ".github" / "workflows" / "qualify-linux-fenics.yml",
        ROOT / ".github" / "workflows" / "prerelease.yml",
        ROOT / "contracts" / "runtime-manifest-v1.schema.json",
        ROOT / "contracts" / "qualification-v1.schema.json",
        ROOT / "config" / "runtime-matrix.json",
        ROOT / "locks" / "core" / "tcltk-8.6.18.json",
        ROOT / "scripts" / "common" / "fetch_tcltk_sources.py",
        ROOT / "locks" / "windows" / "windows-x64-fenics-explicit.txt",
        ROOT / "locks" / "windows" / "windows-x64-fenics-package-lock.json",
        ROOT / "scripts" / "windows" / "package_lock.py",
    ]
    for path in required:
        if not path.is_file():
            errors.append(f"required producer file is missing: {path.relative_to(ROOT)}")

    for path in sorted(ROOT.rglob("*.json")):
        relative = path.relative_to(ROOT)
        if any(part in GENERATED_ROOTS for part in relative.parts):
            continue
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            errors.append(f"invalid JSON {path.relative_to(ROOT)}: {exc}")

    matrix_path = ROOT / "config" / "runtime-matrix.json"
    tcltk_lock_path = ROOT / "locks" / "core" / "tcltk-8.6.18.json"
    if matrix_path.is_file() and tcltk_lock_path.is_file():
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        python = matrix.get("python", {})
        tcl_tk = matrix.get("tcl_tk", {})
        if python != {"abi": "cp314", "consumer_baseline": "3.14.10"}:
            errors.append("runtime matrix must pin CPython 3.14.10 / cp314")
        if tcl_tk != {"version": "8.6.18", "ownership": "producer"}:
            errors.append("runtime matrix must declare producer-owned Tcl/Tk 8.6.18")
        tcltk_lock = json.loads(tcltk_lock_path.read_text(encoding="utf-8"))
        if tcltk_lock.get("version") != "8.6.18":
            errors.append("core Tcl/Tk lock must pin version 8.6.18")
        sources = tcltk_lock.get("sources", {})
        expected_tcltk = {
            "tcl": ("tcl8.6.18-src.tar.gz", "14f9af32b1767ff718477a8f974ad03c34341097e6b43f4ce54644ee974e268e"),
            "tk": ("tk8.6.18-src.tar.gz", "95cd528a80f5e4bdb557af9b14a7197d6860793a3894e25e7c9fad2ed05d4c3c"),
        }
        for component, (filename, digest) in expected_tcltk.items():
            record = sources.get(component, {})
            if record.get("filename") != filename or record.get("sha256") != digest:
                errors.append(f"core Tcl/Tk lock has invalid {component} source identity")

    workflow_root = ROOT / ".github" / "workflows"
    if workflow_root.is_dir():
        for path in sorted(
            child for child in workflow_root.iterdir()
            if child.is_file() and child.suffix.lower() in {".yml", ".yaml"}
        ):
            text = path.read_text(encoding="utf-8")
            if "circleci" in text.lower():
                errors.append(
                    f"workflow contains CircleCI reference: {path.relative_to(ROOT)}"
                )
            if "python-matlab-octave-bridge" in text.lower():
                errors.append(
                    f"workflow references a private consumer repository: {path.relative_to(ROOT)}"
                )
            for action in unpinned_actions(text):
                errors.append(
                    f"workflow action must use an immutable 40-character commit SHA: "
                    f"{path.relative_to(ROOT)}: {action}"
                )

    if errors:
        print("repository validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("repository_validation=pass")
    print("ci_provider=github-actions")
    print("private_consumer_checkout=forbidden")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
