#!/usr/bin/env python3
"""Static repository-policy validation for the public runtime producer."""

from __future__ import annotations

import json
from pathlib import Path
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


def main() -> int:
    errors: list[str] = []

    if (ROOT / ".circleci").exists():
        errors.append("CircleCI configuration is forbidden; use GitHub Actions")

    for path in (ROOT / "src", ROOT / "private"):
        if path.exists():
            errors.append(
                f"private-consumer source root must not exist: {path.relative_to(ROOT)}"
            )

    required = [
        ROOT / ".github" / "workflows" / "validate.yml",
        ROOT / ".github" / "workflows" / "qualify-windows-fenics.yml",
        ROOT / ".github" / "workflows" / "qualify-windows-core-numpy.yml",
        ROOT / ".github" / "workflows" / "qualify-macos-fenics.yml",
        ROOT / ".github" / "workflows" / "qualify-macos-core-numpy.yml",
        ROOT / ".github" / "workflows" / "qualify-linux-core-numpy.yml",
        ROOT / ".github" / "workflows" / "prerelease.yml",
        ROOT / "contracts" / "runtime-manifest-v1.schema.json",
        ROOT / "contracts" / "qualification-v1.schema.json",
        ROOT / "config" / "runtime-matrix.json",
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

    workflow_root = ROOT / ".github" / "workflows"
    if workflow_root.is_dir():
        for path in sorted(workflow_root.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            if "circleci" in text.lower():
                errors.append(
                    f"workflow contains CircleCI reference: {path.relative_to(ROOT)}"
                )
            if "python-matlab-octave-bridge" in text:
                errors.append(
                    f"workflow references a private consumer repository: {path.relative_to(ROOT)}"
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
