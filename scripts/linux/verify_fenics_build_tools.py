#!/usr/bin/env python3
"""Verify the standalone Linux FEniCS C++ build-tool transaction.

This verifier is intentionally independent of the immutable 128-record FEniCS
runtime package lock.  It proves that the checked-in build-only @EXPLICIT file
is exactly the transitive dependency closure of the preserved GCC14/CMake/Ninja
source-build roots, that every archive byte is present and SHA-256 verified,
and that cached package metadata agrees with the preserved installed records.

It does not solve dependencies and it does not make any source_rebuilds claim.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse


EXPECTED_EXPLICIT_SHA256 = "763466504eb4af7a1ac9948269d8b228b6df683df5e5d0f30c434249e8529690"
EXPECTED_PACKAGE_COUNT = 41
EXPECTED_ROOTS = {
    "gcc_linux-64": ("14.4.0", "hc469d41_2"),
    "gxx_linux-64": ("14.4.0", "h0047a55_2"),
    "sysroot_linux-64": ("2.17", "h0157908_18"),
    "cmake": ("4.4.2", "hff7ac6b_2"),
    "ninja": ("1.13.2", "h171cf75_1"),
    "pkg-config": ("0.29.2", "h1114479_1013"),
    "make": ("4.4.1", "h1114479_4"),
}
ALLOWED_SUBDIRS = {"linux-64", "noarch"}
CONDA_FORGE_PREFIX = "https://conda.anaconda.org/conda-forge/"
LOCK_LINE_RE = re.compile(r"^(https://[^#]+)#sha256:([0-9a-f]{64})$")


class BuildToolLockError(ValueError):
    """Raised when build-tool evidence does not satisfy the fixed contract."""


@dataclass(frozen=True)
class ExplicitEntry:
    url: str
    sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_text_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig")
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_explicit(path: Path) -> list[ExplicitEntry]:
    if not path.is_file():
        raise BuildToolLockError(f"explicit lock is missing: {path}")
    actual_sha = canonical_text_sha256(path)
    if actual_sha != EXPECTED_EXPLICIT_SHA256:
        raise BuildToolLockError(
            f"explicit lock digest mismatch: {actual_sha} != {EXPECTED_EXPLICIT_SHA256}"
        )
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines or lines[0] != "@EXPLICIT":
        raise BuildToolLockError("build-tool lock is not an @EXPLICIT transaction")
    entries: list[ExplicitEntry] = []
    seen_urls: set[str] = set()
    for line in lines[1:]:
        match = LOCK_LINE_RE.fullmatch(line)
        if not match:
            raise BuildToolLockError(f"invalid explicit package line: {line!r}")
        url, sha = match.groups()
        if not url.startswith(CONDA_FORGE_PREFIX):
            raise BuildToolLockError(f"unexpected package channel URL: {url!r}")
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "conda-forge" or parts[1] not in ALLOWED_SUBDIRS:
            raise BuildToolLockError(f"unexpected package subdir URL: {url!r}")
        if not (parts[-1].endswith(".conda") or parts[-1].endswith(".tar.bz2")):
            raise BuildToolLockError(f"unsupported conda archive extension: {url!r}")
        if url in seen_urls:
            raise BuildToolLockError(f"duplicate explicit package URL: {url}")
        seen_urls.add(url)
        entries.append(ExplicitEntry(url, sha))
    if len(entries) != EXPECTED_PACKAGE_COUNT:
        raise BuildToolLockError(
            f"explicit package count mismatch: {len(entries)} != {EXPECTED_PACKAGE_COUNT}"
        )
    return entries


def load_prefix_records(prefix: Path) -> dict[str, dict[str, Any]]:
    conda_meta = prefix / "conda-meta"
    if not conda_meta.is_dir():
        raise BuildToolLockError(f"prefix has no conda-meta directory: {conda_meta}")
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(conda_meta.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise BuildToolLockError(f"invalid conda-meta package name in {path}")
        if name in records:
            raise BuildToolLockError(f"duplicate installed package name: {name}")
        for field in ("version", "build", "subdir", "url", "sha256", "depends"):
            if field not in record:
                raise BuildToolLockError(f"conda-meta record {name} is missing {field}")
        if record["subdir"] not in ALLOWED_SUBDIRS:
            raise BuildToolLockError(
                f"conda-meta record {name} has unexpected subdir: {record['subdir']!r}"
            )
        if not str(record["url"]).startswith(CONDA_FORGE_PREFIX):
            raise BuildToolLockError(f"conda-meta record {name} is not from conda-forge")
        sha = str(record["sha256"]).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise BuildToolLockError(f"conda-meta record {name} has invalid SHA-256")
        if not isinstance(record["depends"], list):
            raise BuildToolLockError(f"conda-meta record {name} has non-list dependencies")
        records[name] = record
    return records


def dependency_name(spec: str) -> str:
    if not isinstance(spec, str) or not spec.strip():
        raise BuildToolLockError(f"invalid dependency spec: {spec!r}")
    name = spec.split()[0]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise BuildToolLockError(f"unsupported dependency name in spec: {spec!r}")
    return name


def dependency_closure(
    records: dict[str, dict[str, Any]],
) -> tuple[set[str], set[str]]:
    for name, (version, build) in EXPECTED_ROOTS.items():
        record = records.get(name)
        if record is None:
            raise BuildToolLockError(f"source prefix is missing build-tool root: {name}")
        if (str(record["version"]), str(record["build"])) != (version, build):
            raise BuildToolLockError(
                f"build-tool root identity mismatch for {name}: "
                f"{record['version']} {record['build']} != {version} {build}"
            )

    closure: set[str] = set()
    virtual_specs: set[str] = set()
    pending = list(EXPECTED_ROOTS)
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        record = records.get(name)
        if record is None:
            raise BuildToolLockError(f"dependency closure is missing installed package: {name}")
        closure.add(name)
        for raw_spec in record["depends"]:
            spec = str(raw_spec)
            dep_name = dependency_name(spec)
            if dep_name.startswith("__"):
                virtual_specs.add(spec)
                continue
            if dep_name not in records:
                raise BuildToolLockError(
                    f"dependency closure for {name} has no installed provider for {spec!r}"
                )
            if dep_name not in closure:
                pending.append(dep_name)
    if len(closure) != EXPECTED_PACKAGE_COUNT:
        raise BuildToolLockError(
            f"derived dependency closure count mismatch: {len(closure)} != {EXPECTED_PACKAGE_COUNT}"
        )
    return closure, virtual_specs


def verify_lock_against_records(
    entries: list[ExplicitEntry],
    records: dict[str, dict[str, Any]],
    closure: set[str],
) -> dict[str, dict[str, Any]]:
    records_by_url = {str(record["url"]): record for record in records.values()}
    locked: dict[str, dict[str, Any]] = {}
    for entry in entries:
        record = records_by_url.get(entry.url)
        if record is None:
            raise BuildToolLockError(
                f"explicit package URL is absent from preserved conda-meta: {entry.url}"
            )
        name = str(record["name"])
        if name in locked:
            raise BuildToolLockError(f"explicit lock contains duplicate package name: {name}")
        record_sha = str(record["sha256"]).lower()
        if entry.sha256 != record_sha:
            raise BuildToolLockError(
                f"explicit SHA-256 differs from preserved conda-meta for {name}: "
                f"{entry.sha256} != {record_sha}"
            )
        locked[name] = record
    locked_names = set(locked)
    missing = sorted(closure - locked_names)
    extra = sorted(locked_names - closure)
    if missing or extra:
        raise BuildToolLockError(
            f"explicit lock differs from derived build-tool closure: missing={missing} extra={extra}"
        )
    return locked


def archive_path(package_cache: Path, url: str) -> Path:
    parsed = urlparse(url)
    return package_cache / parsed.scheme / parsed.netloc / parsed.path.lstrip("/")


def locate_archive(package_cache: Path, url: str) -> Path:
    """Locate one locked archive in either preserved or micromamba cache layout."""

    mirrored = archive_path(package_cache, url)
    if mirrored.is_file():
        return mirrored
    filename = Path(urlparse(url).path).name
    if not filename:
        raise BuildToolLockError(f"locked package URL has no archive filename: {url!r}")
    matches = sorted(
        path for path in package_cache.rglob(filename) if path.is_file() and not path.is_symlink()
    )
    if len(matches) != 1:
        raise BuildToolLockError(
            f"expected exactly one cached archive for {filename!r}, found {len(matches)}"
        )
    return matches[0]


def extracted_package_path(archive: Path) -> Path:
    name = archive.name
    if name.endswith(".conda"):
        return archive.with_name(name[: -len(".conda")])
    if name.endswith(".tar.bz2"):
        return archive.with_name(name[: -len(".tar.bz2")])
    raise BuildToolLockError(f"unsupported archive path: {archive}")


def verify_archives(
    package_cache: Path,
    entries: list[ExplicitEntry],
    locked: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not package_cache.is_dir():
        raise BuildToolLockError(f"package cache is missing: {package_cache}")
    by_url = {str(record["url"]): record for record in locked.values()}
    verified: list[dict[str, Any]] = []
    for entry in entries:
        record = by_url[entry.url]
        name = str(record["name"])
        archive = locate_archive(package_cache, entry.url)
        actual_sha = sha256_file(archive)
        if actual_sha != entry.sha256:
            raise BuildToolLockError(
                f"cached archive SHA-256 mismatch for {name}: {actual_sha} != {entry.sha256}"
            )
        index_path = extracted_package_path(archive) / "info" / "index.json"
        if not index_path.is_file():
            raise BuildToolLockError(f"cached extracted index is missing for {name}: {index_path}")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        differences: dict[str, Any] = {}
        for field in ("name", "version", "build", "build_number", "subdir", "depends"):
            if index.get(field) != record.get(field):
                differences[field] = {"index": index.get(field), "conda_meta": record.get(field)}
        if differences:
            raise BuildToolLockError(
                f"cached extracted metadata mismatch for {name}: "
                + json.dumps(differences, sort_keys=True)
            )
        verified.append(
            {
                "name": name,
                "version": record["version"],
                "build": record["build"],
                "subdir": record["subdir"],
                "url": entry.url,
                "sha256": entry.sha256,
                "archive": archive.relative_to(package_cache).as_posix(),
            }
        )
    return verified


def verify(
    explicit: Path,
    source_prefix: Path,
    package_cache: Path,
    evidence: Path | None,
    exact_prefix: bool,
) -> dict[str, Any]:
    entries = parse_explicit(explicit)
    records = load_prefix_records(source_prefix)
    closure, virtual_specs = dependency_closure(records)
    locked = verify_lock_against_records(entries, records, closure)
    if exact_prefix:
        extra = sorted(set(records) - closure)
        if extra:
            raise BuildToolLockError(f"exact replay prefix contains non-lock packages: {extra}")
    archives = verify_archives(package_cache, entries, locked)
    payload = {
        "format_version": 1,
        "purpose": "linux-fenics-cxx20-build-toolchain-only",
        "build_only": True,
        "runtime_lock_modified": False,
        "source_rebuilds_claimed": False,
        "dependency_solver_used": False,
        "explicit_sha256": canonical_text_sha256(explicit),
        "package_count": len(entries),
        "source_prefix_record_count": len(records),
        "exact_prefix": exact_prefix,
        "roots": {
            name: {"version": version, "build": build}
            for name, (version, build) in sorted(EXPECTED_ROOTS.items())
        },
        "virtual_dependency_specs": sorted(virtual_specs),
        "packages": sorted(archives, key=lambda item: item["name"]),
    }
    if evidence is not None:
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--explicit", required=True, type=Path)
    parser.add_argument("--source-prefix", required=True, type=Path)
    parser.add_argument("--package-cache", required=True, type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument(
        "--exact-prefix",
        action="store_true",
        help="require the prefix to contain exactly the 41 locked packages",
    )
    args = parser.parse_args()
    try:
        payload = verify(
            args.explicit.resolve(),
            args.source_prefix.resolve(),
            args.package_cache.resolve(),
            args.evidence.resolve() if args.evidence else None,
            args.exact_prefix,
        )
    except BuildToolLockError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"build_tool_explicit_sha256={payload['explicit_sha256']}")
    print(f"build_tool_packages={payload['package_count']}")
    print("dependency_solver=disabled")
    print("runtime_lock_modified=false")
    print("source_rebuilds_claimed=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
