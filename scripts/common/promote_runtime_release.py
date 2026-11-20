#!/usr/bin/env python3
"""Promote an immutable qualified runtime prerelease to a stable release."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil


SOURCE_TAG_RE = re.compile(
    r"^runtime-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-rc(?P<rc>[0-9]+)$"
)
TARGET_TAG_RE = re.compile(r"^runtime-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_name(value: object) -> str:
    if not isinstance(value, str):
        raise SystemExit(f"invalid release asset name: {value!r}")
    name = value
    if (
        not name
        or name != Path(name).name
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or ":" in name
    ):
        raise SystemExit(f"invalid release asset name: {name!r}")
    return name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-tag", required=True)
    parser.add_argument("--target-tag", required=True)
    args = parser.parse_args()

    source_match = SOURCE_TAG_RE.fullmatch(args.source_tag)
    if source_match is None:
        raise SystemExit("source tag must match runtime-vX.Y.Z-rcN")
    target_match = TARGET_TAG_RE.fullmatch(args.target_tag)
    if target_match is None:
        raise SystemExit("target tag must match runtime-vX.Y.Z")
    if source_match.group("version") != target_match.group("version"):
        raise SystemExit("source and target release versions differ")

    source = args.source.resolve()
    output = args.output.resolve()
    if not source.is_dir():
        raise SystemExit(f"source release directory is missing: {source}")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    manifest_path = source / "prerelease-manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise SystemExit("source prerelease-manifest.json is missing or invalid")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get("format_version") != 1:
        raise SystemExit("unsupported source release manifest format")
    if manifest.get("tag") != args.source_tag:
        raise SystemExit("source release manifest tag mismatch")
    version = source_match.group("version")
    if manifest.get("version") != version:
        raise SystemExit("source release manifest version mismatch")
    if manifest.get("producer_repository") != "precsim/python-numpy-fenics":
        raise SystemExit("source release manifest repository mismatch")
    producer_revision = str(manifest.get("producer_revision", "")).lower()
    if GIT_SHA_RE.fullmatch(producer_revision) is None:
        raise SystemExit("source release manifest has invalid producer revision")
    profiles = manifest.get("profiles")
    if not isinstance(profiles, list) or set(profiles) != {"core", "numpy", "fenics"}:
        raise SystemExit("stable promotion requires core, numpy, and fenics profiles")

    source_runs = manifest.get("source_runs")
    if not isinstance(source_runs, list) or not source_runs:
        raise SystemExit("source release manifest has no qualification source runs")
    for record in source_runs:
        if not isinstance(record, dict) or record.get("head_sha") != producer_revision:
            raise SystemExit("source qualification run revision mismatch")

    qualifications = manifest.get("qualifications")
    if not isinstance(qualifications, list) or len(qualifications) != 12:
        raise SystemExit("stable promotion requires the full 12-profile qualification matrix")
    identities = set()
    for record in qualifications:
        if not isinstance(record, dict) or record.get("producer_revision") != producer_revision:
            raise SystemExit("source qualification revision mismatch")
        identities.add((record.get("platform"), record.get("profile")))
    expected_identities = {
        (platform, profile)
        for platform in ("win-x64", "linux-x64", "macos-arm64", "macos-x64")
        for profile in ("core", "numpy", "fenics")
    }
    if identities != expected_identities:
        raise SystemExit("source release qualification matrix is incomplete")

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise SystemExit("source release manifest has no file inventory")
    expected_files: dict[str, str] = {}
    for record in files:
        if not isinstance(record, dict):
            raise SystemExit("invalid source release file record")
        name = asset_name(record.get("name"))
        digest = str(record.get("sha256", "")).lower()
        if SHA256_RE.fullmatch(digest) is None:
            raise SystemExit(f"invalid source release SHA-256 for {name}")
        if name in expected_files:
            raise SystemExit(f"duplicate source release asset: {name}")
        expected_files[name] = digest

    actual_entries = list(source.iterdir())
    for entry in actual_entries:
        if entry.is_symlink() or not entry.is_file():
            raise SystemExit(f"source release contains unsupported entry: {entry.name}")
    actual_names = {entry.name for entry in actual_entries}
    expected_names = set(expected_files) | {"prerelease-manifest.json"}
    if actual_names != expected_names:
        raise SystemExit(
            "source release asset set mismatch: "
            f"missing={sorted(expected_names - actual_names)!r} "
            f"extra={sorted(actual_names - expected_names)!r}"
        )

    for name, expected_sha in sorted(expected_files.items()):
        source_path = source / name
        actual_sha = sha256_file(source_path)
        if actual_sha != expected_sha:
            raise SystemExit(
                f"source release asset SHA-256 mismatch: {name} "
                f"expected={expected_sha} actual={actual_sha}"
            )
        shutil.copy2(source_path, output / name)

    promoted = dict(manifest)
    promoted["tag"] = args.target_tag
    promoted["promotion"] = {
        "source_tag": args.source_tag,
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    (output / "prerelease-manifest.json").write_text(
        json.dumps(promoted, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"producer_release_source_tag={args.source_tag}")
    print(f"producer_release_target_tag={args.target_tag}")
    print(f"producer_release_revision={producer_revision}")
    print(f"producer_release_files={len(expected_files) + 1}")
    print("producer_release_promotion=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
