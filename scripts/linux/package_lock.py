#!/usr/bin/env python3
"""Render and verify the immutable Linux FEniCS conda package lock.

The producer lock is deliberately separate from an environment solver.  The
``explicit`` subcommand turns the checked-in records into a CEP-23-style
``@EXPLICIT`` transaction containing exact URLs and SHA-256 digests.  The
``verify-prefix`` subcommand proves that a prefix created from that transaction
contains exactly the locked package identities before overlay packaging starts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = (
    "name",
    "version",
    "build",
    "build_number",
    "subdir",
    "channel",
    "url",
    "sha256",
    "md5",
)

EXPECTED_RECORD_COUNT = 128

SOURCE_EXPLICIT_SHA256 = {
    "x86_64": "945ad865b5f9404354a5c87bf4db9c93e97da54b77d734780ad3524296277a27",
}

REQUIRED_VERSIONS = {
    "python": "3.12.16",
    "numpy": "2.5.2",
    "fenics-dolfinx": "0.11.0",
    "fenics-libdolfinx": "0.11.0",
    "petsc": "3.25.6",
    "petsc4py": "3.25.6",
    "mpi4py": "4.1.2",
    "mpich": "5.0.1",
    "hdf5": "1.14.6",
    "fenics-basix": "0.11.0",
    "fenics-ffcx": "0.11.0",
    "fenics-ufl": "2026.1.0",
    "libopenblas": "0.3.34",
}

REQUIRED_BUILDS = {
    "libopenblas": "openmp_h156ec52_2",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_canonical_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig")
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_lock(path: Path, architecture: str | None) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format_version") != 1:
        raise SystemExit(f"unsupported package-lock format: {payload.get('format_version')!r}")
    locked_arch = payload.get("architecture")
    if locked_arch != "x86_64":
        raise SystemExit(f"invalid package-lock architecture: {locked_arch!r}")
    if architecture is not None and locked_arch != architecture:
        raise SystemExit(f"package-lock architecture mismatch: {locked_arch} != {architecture}")
    target_subdir = "linux-64"
    packages = payload.get("packages")
    if not isinstance(packages, list) or not packages:
        raise SystemExit("package lock has no packages")
    if payload.get("record_count") != len(packages):
        raise SystemExit(
            f"package-lock record_count mismatch: {payload.get('record_count')!r} != {len(packages)}"
        )
    if len(packages) != EXPECTED_RECORD_COUNT:
        raise SystemExit(
            f"package-lock record count mismatch: {len(packages)} != {EXPECTED_RECORD_COUNT}"
        )

    seen: set[tuple[str, str, str, str]] = set()
    by_name: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(packages):
        if not isinstance(record, dict):
            raise SystemExit(f"package-lock record {index} is not an object")
        missing = [field for field in REQUIRED_FIELDS if record.get(field) in (None, "")]
        if missing:
            raise SystemExit(f"package-lock record {index} is missing fields: {missing!r}")
        sha = str(record["sha256"]).lower()
        md5 = str(record["md5"]).lower()
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise SystemExit(f"invalid SHA-256 in package-lock record {index}: {sha!r}")
        if len(md5) != 32 or any(ch not in "0123456789abcdef" for ch in md5):
            raise SystemExit(f"invalid MD5 in package-lock record {index}: {md5!r}")
        url = str(record["url"])
        if record["channel"] != "conda-forge":
            raise SystemExit(
                f"package-lock record {index} has unexpected channel: {record['channel']!r}"
            )
        expected_url_prefix = f"https://conda.anaconda.org/conda-forge/{record['subdir']}/"
        if not url.startswith(expected_url_prefix):
            raise SystemExit(f"package-lock record {index} has unexpected URL: {url!r}")
        if record["subdir"] not in {target_subdir, "noarch"}:
            raise SystemExit(
                f"package-lock record {index} has cross-target subdir: "
                f"{record['subdir']!r} not in {{{target_subdir!r}, 'noarch'}}"
            )
        key = (str(record["name"]), str(record["version"]), str(record["build"]), url)
        if key in seen:
            raise SystemExit(f"duplicate package-lock identity: {key!r}")
        seen.add(key)
        name = str(record["name"])
        if name == "_x86_64-microarch-level":
            raise SystemExit("package lock must target generic x86_64, not an x86_64_vN microarchitecture")
        if name in by_name:
            raise SystemExit(f"duplicate package-lock name: {name!r}")
        by_name[name] = record

    for name, expected_version in REQUIRED_VERSIONS.items():
        record = by_name.get(name)
        if record is None:
            raise SystemExit(f"package lock is missing required package: {name}")
        actual_version = str(record["version"])
        if actual_version != expected_version:
            raise SystemExit(
                f"package lock version mismatch for {name}: "
                f"{actual_version!r} != {expected_version!r}"
            )
    for name, expected_build in REQUIRED_BUILDS.items():
        record = by_name.get(name)
        if record is None:
            raise SystemExit(f"package lock is missing required package: {name}")
        actual_build = str(record["build"])
        if actual_build != expected_build:
            raise SystemExit(
                f"package lock build mismatch for {name}: "
                f"{actual_build!r} != {expected_build!r}"
            )
    return payload


def locked_url_order(lock: dict[str, Any], source_explicit: Path) -> list[str]:
    actual_sha = sha256_canonical_text(source_explicit)
    expected_sha = SOURCE_EXPLICIT_SHA256[lock["architecture"]]
    if actual_sha != expected_sha:
        raise SystemExit(
            f"source explicit transaction digest mismatch: {actual_sha} != {expected_sha}"
        )
    urls = [
        line.strip()
        for line in source_explicit.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("https://")
    ]
    locked_urls = [str(record["url"]) for record in lock["packages"]]
    if len(urls) != len(set(urls)):
        raise SystemExit("source explicit transaction contains duplicate package URLs")
    if len(locked_urls) != len(set(locked_urls)):
        raise SystemExit("checked-in package lock contains duplicate package URLs")
    if len(urls) != len(locked_urls) or set(urls) != set(locked_urls):
        raise SystemExit("source explicit package URL set differs from the checked-in package lock")
    return urls


def render_explicit(lock: dict[str, Any], source_explicit: Path) -> str:
    platform = "linux-64"
    records_by_url = {str(record["url"]): record for record in lock["packages"]}
    urls = locked_url_order(lock, source_explicit)
    lines = [
        "# Generated from the checked-in micromamba dry-run URL order plus checked-in SHA-256 locks.",
        "# Dependency solving is forbidden when replaying this transaction.",
        f"# source-explicit-sha256: {sha256_canonical_text(source_explicit)}",
        f"# platform: {platform}",
        "@EXPLICIT",
    ]
    for url in urls:
        record = records_by_url[url]
        lines.append(f"{record['url']}#sha256:{str(record['sha256']).lower()}")
    return "\n".join(lines) + "\n"


def normalize_installed_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": record.get("name"),
        "version": record.get("version"),
        "build": record.get("build"),
        "build_number": record.get("build_number"),
        "subdir": record.get("subdir"),
        "channel": record.get("channel"),
        "url": record.get("url"),
        "sha256": record.get("sha256"),
        "md5": record.get("md5"),
    }


def verify_prefix(
    lock: dict[str, Any],
    lock_path: Path,
    prefix: Path,
    package_cache: Path,
    evidence: Path,
) -> None:
    conda_meta = prefix / "conda-meta"
    if not conda_meta.is_dir():
        raise SystemExit(f"locked prefix has no conda metadata: {conda_meta}")

    installed: list[dict[str, Any]] = []
    for path in sorted(conda_meta.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        installed.append(normalize_installed_record(record))

    expected = [normalize_installed_record(record) for record in lock["packages"]]
    expected_by_name = {str(record["name"]): record for record in expected}
    installed_by_name = {str(record["name"]): record for record in installed}
    if len(expected_by_name) != len(expected):
        raise SystemExit("package lock contains duplicate package names; prefix verification is ambiguous")
    if len(installed_by_name) != len(installed):
        raise SystemExit("installed prefix contains duplicate package names; prefix verification is ambiguous")

    missing = sorted(set(expected_by_name) - set(installed_by_name))
    extra = sorted(set(installed_by_name) - set(expected_by_name))
    if missing or extra:
        raise SystemExit(f"locked-prefix package-set mismatch: missing={missing!r} extra={extra!r}")

    mismatches: list[dict[str, Any]] = []
    for name in sorted(expected_by_name):
        exp = expected_by_name[name]
        got = installed_by_name[name]
        differing: dict[str, Any] = {}
        for field in REQUIRED_FIELDS:
            exp_value = exp.get(field)
            got_value = got.get(field)
            if field in {"sha256", "md5"}:
                exp_value = str(exp_value or "").lower()
                got_value = str(got_value or "").lower()
            if exp_value != got_value:
                differing[field] = {"expected": exp_value, "actual": got_value}
        if differing:
            mismatches.append({"name": name, "fields": differing})
    if mismatches:
        raise SystemExit("locked-prefix identity mismatch:\n" + json.dumps(mismatches, indent=2))

    evidence.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format_version": 1,
        "architecture": lock["architecture"],
        "lock_record_count": len(expected),
        "installed_record_count": len(installed),
        "lock_sha256": sha256_file(lock_path),
        "package_hash_verification": "micromamba-explicit-sha256-plus-installed-record-match",
        "solver_used": False,
        "packages": [installed_by_name[name] for name in sorted(installed_by_name)],
    }
    evidence.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


parser = argparse.ArgumentParser()
subparsers = parser.add_subparsers(dest="command", required=True)

explicit_parser = subparsers.add_parser("explicit", help="render a solver-free @EXPLICIT transaction")
explicit_parser.add_argument("--lock", required=True)
explicit_parser.add_argument("--source-explicit", required=True)
explicit_parser.add_argument("--output", required=True)
explicit_parser.add_argument("--arch", choices=("x86_64",))

verify_parser = subparsers.add_parser("verify-prefix", help="verify an installed prefix against the lock")
verify_parser.add_argument("--lock", required=True)
verify_parser.add_argument("--prefix", required=True)
verify_parser.add_argument("--package-cache", required=True)
verify_parser.add_argument("--evidence", required=True)
verify_parser.add_argument("--arch", choices=("x86_64",))

args = parser.parse_args()
lock_path = Path(args.lock).resolve()
lock = load_lock(lock_path, args.arch)

if args.command == "explicit":
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    source_explicit = Path(args.source_explicit).resolve()
    output.write_text(render_explicit(lock, source_explicit), encoding="utf-8")
    print(f"package_lock_sha256={sha256_file(lock_path)}")
    print(f"source_explicit_sha256={sha256_canonical_text(source_explicit)}")
    print(f"explicit_records={len(lock['packages'])}")
    print("dependency_solver=disabled")
elif args.command == "verify-prefix":
    verify_prefix(
        lock,
        lock_path,
        Path(args.prefix).resolve(),
        Path(args.package_cache).resolve(),
        Path(args.evidence).resolve(),
    )
    print(f"package_lock_sha256={sha256_file(lock_path)}")
    print(f"verified_records={len(lock['packages'])}")
    print("package_hash_verification=micromamba-explicit-sha256-plus-installed-record-match")
else:  # pragma: no cover
    raise AssertionError(args.command)
