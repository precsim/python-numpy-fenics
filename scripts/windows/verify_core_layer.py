#!/usr/bin/env python3
"""Verify that a Windows NumPy runtime preserves its staged core byte identity."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


EXCLUDED_CORE_PATHS = {"RUNTIME_PROFILE"}


class CoreLayerError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(raw: object) -> str:
    value = str(raw).replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise CoreLayerError(f"core runtime manifest contains unsafe path: {value!r}")
    return path.as_posix()


def _load_core_manifest(core_root: Path) -> tuple[Path, dict[str, object]]:
    manifest_path = core_root / "runtime-manifest.json"
    if not manifest_path.is_file():
        raise CoreLayerError(f"core runtime manifest is missing: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise CoreLayerError("core runtime manifest is not an object")
    if (
        payload.get("format_version") != 1
        or payload.get("platform") != "win-x64"
        or payload.get("profile") != "core"
        or payload.get("python_abi") != "cp312"
    ):
        raise CoreLayerError("core runtime manifest identity is invalid")
    return manifest_path, payload


def verify_core_layer(core: Path, numpy: Path) -> dict[str, object]:
    core_root = core.resolve()
    numpy_root = numpy.resolve()
    if not core_root.is_dir():
        raise CoreLayerError(f"core runtime does not exist: {core_root}")
    if not numpy_root.is_dir():
        raise CoreLayerError(f"NumPy runtime does not exist: {numpy_root}")

    manifest_path, manifest = _load_core_manifest(core_root)
    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        raise CoreLayerError("core runtime manifest has no file inventory")

    expected: dict[str, tuple[int, str]] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise CoreLayerError("core runtime manifest contains a non-object file entry")
        relative = _safe_relative(raw.get("path"))
        if relative in expected:
            raise CoreLayerError(f"core runtime manifest contains duplicate path: {relative}")
        digest = str(raw.get("sha256", "")).lower()
        size = raw.get("bytes")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise CoreLayerError(f"core runtime manifest has invalid SHA-256: {relative}")
        if not isinstance(size, int) or size < 0:
            raise CoreLayerError(f"core runtime manifest has invalid byte count: {relative}")
        expected[relative] = (size, digest)

    actual_core_files = {
        path.relative_to(core_root).as_posix()
        for path in core_root.rglob("*")
        if path.is_file()
    }
    expected_core_files = set(expected) | {"runtime-manifest.json"}
    if actual_core_files != expected_core_files:
        missing = sorted(expected_core_files - actual_core_files)
        extra = sorted(actual_core_files - expected_core_files)
        raise CoreLayerError(
            f"core runtime file inventory differs from its manifest: missing={missing!r} extra={extra!r}"
        )

    aggregate = hashlib.sha256()
    inherited_count = 0
    for relative in sorted(expected):
        size, digest = expected[relative]
        core_path = core_root.joinpath(*PurePosixPath(relative).parts)
        if core_path.stat().st_size != size or sha256_file(core_path) != digest:
            raise CoreLayerError(f"qualified core runtime differs from its manifest: {relative}")
        if relative in EXCLUDED_CORE_PATHS:
            continue
        numpy_path = numpy_root.joinpath(*PurePosixPath(relative).parts)
        if not numpy_path.is_file():
            raise CoreLayerError(f"NumPy runtime is missing inherited core file: {relative}")
        if numpy_path.stat().st_size != size or sha256_file(numpy_path) != digest:
            raise CoreLayerError(f"NumPy runtime changed inherited core file: {relative}")
        aggregate.update(b"F\0")
        aggregate.update(relative.encode("utf-8") + b"\0")
        aggregate.update(digest.encode("ascii") + b"\n")
        inherited_count += 1

    if inherited_count == 0:
        raise CoreLayerError("core runtime has no inherited files to bind")

    return {
        "format_version": 1,
        "kind": "windows-core-layer-identity",
        "platform": "win-x64",
        "core_manifest_sha256": sha256_file(manifest_path),
        "inherited_tree_sha256": aggregate.hexdigest(),
        "inherited_file_count": inherited_count,
        "excluded_paths": sorted(EXCLUDED_CORE_PATHS),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core", required=True)
    parser.add_argument("--numpy", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    payload = verify_core_layer(Path(args.core), Path(args.numpy))
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"windows_core_manifest_sha256={payload['core_manifest_sha256']}")
    print(f"windows_inherited_core_tree_sha256={payload['inherited_tree_sha256']}")
    print(f"windows_inherited_core_file_count={payload['inherited_file_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
