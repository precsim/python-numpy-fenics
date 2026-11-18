#!/usr/bin/env python3
"""Derive an exact-key cache identity for Linux NumPy native-build intermediates."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


CACHE_SCHEMA = "linux-numpy-native-build-v2"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def contract_key(
    *,
    image: str,
    core_build_script_sha: str,
    workflow_sha: str,
    manifest_sha: str,
    numpy_build_script_sha: str,
    helper_sha: str,
) -> str:
    digest = hashlib.sha256()
    for name, value in (
        ("schema", CACHE_SCHEMA),
        ("image", image),
        ("core_build_script_sha256", core_build_script_sha),
        ("workflow_sha256", workflow_sha),
        ("sources_manifest_sha256", manifest_sha),
        ("numpy_build_script_sha256", numpy_build_script_sha),
        ("cache_helper_sha256", helper_sha),
    ):
        digest.update(name.encode() + b"\0" + value.encode() + b"\n")
    return f"{CACHE_SCHEMA}-{digest.hexdigest()}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-build-script", type=Path, required=True)
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--sources-manifest", type=Path, required=True)
    parser.add_argument("--build-script", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()

    core_build_script = args.core_build_script.resolve()
    workflow = args.workflow.resolve()
    manifest = args.sources_manifest.resolve()
    numpy_build_script = args.build_script.resolve()
    output = args.github_output.resolve()
    helper = Path(__file__).resolve()

    for label, path in (
        ("core build script", core_build_script),
        ("Linux qualification workflow", workflow),
        ("NumPy source manifest", manifest),
        ("NumPy build script", numpy_build_script),
        ("cache-key helper", helper),
    ):
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"{label} is missing or not a regular file: {path}")

    core_build_script_sha = sha256_file(core_build_script)
    workflow_sha = sha256_file(workflow)
    manifest_sha = sha256_file(manifest)
    numpy_build_script_sha = sha256_file(numpy_build_script)
    helper_sha = sha256_file(helper)
    key = contract_key(
        image=args.image,
        core_build_script_sha=core_build_script_sha,
        workflow_sha=workflow_sha,
        manifest_sha=manifest_sha,
        numpy_build_script_sha=numpy_build_script_sha,
        helper_sha=helper_sha,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"key={key}\n")
        stream.write(f"core_build_script_sha256={core_build_script_sha}\n")
        stream.write(f"workflow_sha256={workflow_sha}\n")
        stream.write(f"sources_manifest_sha256={manifest_sha}\n")
        stream.write(f"numpy_build_script_sha256={numpy_build_script_sha}\n")
        stream.write(f"cache_helper_sha256={helper_sha}\n")

    print(f"numpy_build_cache_key={key}")
    print(f"numpy_build_cache_core_build_script_sha256={core_build_script_sha}")
    print(f"numpy_build_cache_workflow_sha256={workflow_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
