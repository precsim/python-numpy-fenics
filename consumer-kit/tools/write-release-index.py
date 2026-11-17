#!/usr/bin/env python3
"""Validate and index the exact reference-bridge release asset set."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile
from typing import Any
import zipfile


REPOSITORY = "precsim/python-numpy-fenics"
TARGETS = ("linux-x64", "macos-arm64", "macos-x64", "win-x64")
PROFILE_ORDERS = (
    ("core", "numpy"),
    ("core", "numpy", "fenics"),
)
EXPECTED_EXTENSION = {
    "win-x64": "zip",
    "linux-x64": "tar.gz",
    "macos-arm64": "tar.gz",
    "macos-x64": "tar.gz",
}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
RUN_ID = re.compile(r"^[1-9][0-9]*$")
VERSION = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?$")
ARCHIVE_RE = re.compile(
    r"^reference-bridge-(win-x64|linux-x64|macos-arm64|macos-x64)-cp314-"
    r"(core|numpy|fenics)-(v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?)\.(zip|tar\.gz)$"
)
QUALIFICATION_RE = re.compile(
    r"^qualification-reference-(win-x64|linux-x64|macos-arm64|macos-x64)-"
    r"(core|numpy|fenics)\.json$"
)
QUALIFICATION_FIELDS = {
    "format_version",
    "producer_repository",
    "source_revision",
    "workflow_run_id",
    "workflow_run_attempt",
    "platform",
    "profile",
    "python_abi",
    "mex_sha256",
    "build_provenance_sha256",
    "runtime_receipt_sha256",
    "native_audit_sha256",
    "checks",
}
EXPECTED_QUALIFICATION_CHECKS = {
    "octave_host",
    "repeated_calls",
    "fresh_process",
    "relocated_runtime",
    "native_boundary",
}
# FEniCS runtime inventories can exceed 4 MiB even when their compressed
# archives are compact. Keep a bounded read, but accommodate full inventories.
MAX_MANIFEST_BYTES = 64 * 1024 * 1024


class ReleaseIndexError(ValueError):
    """The promotion directory is not one exact qualified release set."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseIndexError(message)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseIndexError(f"invalid {label}: {path.name}: {exc}") from exc
    require(isinstance(payload, dict), f"{label} must contain a JSON object: {path.name}")
    return payload


def parse_sidecar(archive: Path, sidecar: Path) -> str:
    try:
        line = sidecar.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ReleaseIndexError(f"cannot read package sidecar: {sidecar.name}: {exc}") from exc
    match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
    require(match is not None, f"invalid package SHA-256 sidecar: {sidecar.name}")
    require(match.group(2) == archive.name, f"package sidecar names a different archive: {sidecar.name}")
    actual = digest(archive)
    require(actual == match.group(1), f"package SHA-256 mismatch: {archive.name}")
    return actual


def package_root_name(archive: Path) -> str:
    if archive.name.endswith(".tar.gz"):
        return archive.name[: -len(".tar.gz")]
    require(archive.name.endswith(".zip"), f"unsupported package archive: {archive.name}")
    return archive.name[: -len(".zip")]


def embedded_manifest_bytes(archive: Path) -> bytes:
    expected = f"{package_root_name(archive)}/reference-manifest.json"
    if archive.name.endswith(".zip"):
        try:
            with zipfile.ZipFile(archive) as package:
                matches = [info for info in package.infolist() if info.filename == expected]
                require(len(matches) == 1, f"package has no unique reference manifest: {archive.name}")
                info = matches[0]
                require(not info.is_dir(), f"reference manifest is not a file: {archive.name}")
                require(info.file_size <= MAX_MANIFEST_BYTES, f"reference manifest is too large: {archive.name}")
                return package.read(info)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ReleaseIndexError(f"cannot read ZIP package: {archive.name}: {exc}") from exc

    try:
        with tarfile.open(archive, "r:gz") as package:
            matches = [member for member in package.getmembers() if member.name == expected]
            require(len(matches) == 1, f"package has no unique reference manifest: {archive.name}")
            member = matches[0]
            require(member.isfile(), f"reference manifest is not a file: {archive.name}")
            require(member.size <= MAX_MANIFEST_BYTES, f"reference manifest is too large: {archive.name}")
            source = package.extractfile(member)
            require(source is not None, f"cannot read reference manifest: {archive.name}")
            with source:
                return source.read(MAX_MANIFEST_BYTES + 1)
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseIndexError(f"cannot read tar package: {archive.name}: {exc}") from exc


def load_embedded_manifest(archive: Path) -> dict[str, Any]:
    raw = embedded_manifest_bytes(archive)
    require(len(raw) <= MAX_MANIFEST_BYTES, f"reference manifest is too large: {archive.name}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseIndexError(f"invalid embedded reference manifest: {archive.name}: {exc}") from exc
    require(isinstance(payload, dict), f"embedded reference manifest is not an object: {archive.name}")
    return payload


def validate_qualification(
    path: Path,
    *,
    target: str,
    profile: str,
    source_revision: str,
    qualification_run_id: str,
    qualification_run_attempt: str,
) -> dict[str, Any]:
    record = load_json(path, "qualification record")
    require(set(record) == QUALIFICATION_FIELDS, f"qualification record fields differ from v1: {path.name}")
    require(record.get("format_version") == 1, f"unsupported qualification format: {path.name}")
    require(record.get("producer_repository") == REPOSITORY, f"qualification repository mismatch: {path.name}")
    require(record.get("source_revision") == source_revision, f"qualification source revision mismatch: {path.name}")
    require(str(record.get("workflow_run_id")) == qualification_run_id, f"qualification run ID mismatch: {path.name}")
    require(
        str(record.get("workflow_run_attempt")) == qualification_run_attempt,
        f"qualification run attempt mismatch: {path.name}",
    )
    require(record.get("platform") == target, f"qualification platform mismatch: {path.name}")
    require(record.get("profile") == profile, f"qualification profile mismatch: {path.name}")
    require(record.get("python_abi") == "cp314", f"qualification Python ABI mismatch: {path.name}")
    for key in ("mex_sha256", "build_provenance_sha256", "runtime_receipt_sha256", "native_audit_sha256"):
        value = record.get(key)
        require(isinstance(value, str) and HEX64.fullmatch(value) is not None, f"invalid {key}: {path.name}")
    checks = record.get("checks")
    require(isinstance(checks, dict), f"qualification checks are missing: {path.name}")
    require(
        set(checks) == EXPECTED_QUALIFICATION_CHECKS,
        f"qualification check contract mismatch: {path.name}",
    )
    require(
        all(checks[name] == "pass" for name in EXPECTED_QUALIFICATION_CHECKS),
        f"qualification checks must all pass: {path.name}",
    )
    return record


def validate_manifest_binding(
    archive: Path,
    manifest: dict[str, Any],
    qualification: dict[str, Any],
    qualification_sha256: str,
    *,
    target: str,
    profile: str,
    version: str,
    source_revision: str,
    qualification_run_id: str,
) -> None:
    require(manifest.get("package") == package_root_name(archive), f"package identity mismatch: {archive.name}")
    require(manifest.get("version") == version, f"package version mismatch: {archive.name}")
    require(manifest.get("platform") == target, f"package platform mismatch: {archive.name}")
    require(manifest.get("profile") == profile, f"package profile mismatch: {archive.name}")
    require(manifest.get("python_abi") == "cp314", f"package Python ABI mismatch: {archive.name}")
    mock = manifest.get("mock")
    require(isinstance(mock, dict), f"package mock identity is missing: {archive.name}")
    require(mock.get("source_revision") == source_revision, f"package source revision mismatch: {archive.name}")
    qualification_record = manifest.get("qualification")
    require(isinstance(qualification_record, dict), f"package qualification identity is missing: {archive.name}")
    require(
        qualification_record.get("run_identity") == f"github-actions:{qualification_run_id}",
        f"package qualification run identity mismatch: {archive.name}",
    )
    require(
        qualification_record.get("evidence_path") == "evidence/qualification-evidence.json",
        f"package qualification evidence path mismatch: {archive.name}",
    )
    require(
        qualification_record.get("evidence_sha256") == qualification_sha256,
        f"external qualification differs from packaged qualification: {archive.name}",
    )
    mex = manifest.get("mex")
    require(isinstance(mex, dict), f"package MEX identity is missing: {archive.name}")
    require(mex.get("sha256") == qualification.get("mex_sha256"), f"package MEX differs from qualification: {archive.name}")


def build_release_index(
    root: Path,
    *,
    source_revision: str,
    qualification_run_id: str,
    qualification_run_attempt: str,
) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir(), f"promotion root does not exist: {root}")
    require(HEX40.fullmatch(source_revision) is not None, "source revision must be a lowercase 40-hex Git SHA")
    require(RUN_ID.fullmatch(qualification_run_id) is not None, "qualification run ID must be a positive integer")
    require(RUN_ID.fullmatch(qualification_run_attempt) is not None, "qualification run attempt must be a positive integer")

    archives: dict[tuple[str, str], tuple[Path, str, str]] = {}
    qualifications: dict[tuple[str, str], Path] = {}
    sidecars: dict[tuple[str, str], Path] = {}
    source_files: list[tuple[Path, str]] = []

    entries = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    for path in entries:
        relative = path.relative_to(root)
        require(not path.is_symlink(), f"promotion root contains a symlink: {path.name}")
        if path.is_dir():
            require(relative.as_posix() == "packages", f"promotion root contains an unexpected directory: {relative.as_posix()}")
            continue
        require(path.is_file(), f"promotion root contains an unexpected non-file entry: {relative.as_posix()}")

        archive_match = ARCHIVE_RE.fullmatch(path.name)
        if archive_match is not None:
            require(relative.parent.as_posix() == "packages", f"package archive is outside packages/: {relative.as_posix()}")
            target, profile, version, extension = archive_match.groups()
            require(extension == EXPECTED_EXTENSION[target], f"wrong archive format for {target}: {path.name}")
            key = (target, profile)
            require(key not in archives, f"duplicate reference package: {target}/{profile}")
            archives[key] = (path, version, extension)
            source_files.append((path, relative.as_posix()))
            continue

        qualification_match = QUALIFICATION_RE.fullmatch(path.name)
        if qualification_match is not None:
            require(relative.parent == Path("."), f"qualification record is not at promotion root: {relative.as_posix()}")
            key = (qualification_match.group(1), qualification_match.group(2))
            require(key not in qualifications, f"duplicate qualification record: {key[0]}/{key[1]}")
            qualifications[key] = path
            source_files.append((path, relative.as_posix()))
            continue

        if path.name.endswith(".sha256"):
            archive_name = path.name[: -len(".sha256")]
            sidecar_match = ARCHIVE_RE.fullmatch(archive_name)
            if sidecar_match is not None:
                require(relative.parent.as_posix() == "packages", f"package sidecar is outside packages/: {relative.as_posix()}")
                target, profile = sidecar_match.group(1), sidecar_match.group(2)
                key = (target, profile)
                require(key not in sidecars, f"duplicate package sidecar: {target}/{profile}")
                sidecars[key] = path
                source_files.append((path, relative.as_posix()))
                continue

        raise ReleaseIndexError(f"unexpected promotion file: {relative.as_posix()}")

    archive_keys = set(archives)
    require(archive_keys, "promotion set contains no reference packages")
    require(set(qualifications) == archive_keys, "qualification records do not exactly match package matrix")
    require(set(sidecars) == archive_keys, "package sidecars do not exactly match package matrix")

    selected_profiles = tuple(profile for profile in PROFILE_ORDERS[-1] if any((target, profile) in archive_keys for target in TARGETS))
    require(selected_profiles in PROFILE_ORDERS, f"release profile set is not promotable: {selected_profiles}")
    expected_keys = {(target, profile) for target in TARGETS for profile in selected_profiles}
    require(archive_keys == expected_keys, "release packages must contain every target for every promoted profile")
    versions = {record[1] for record in archives.values()}
    require(len(versions) == 1, f"release packages span versions: {sorted(versions)}")
    version = next(iter(versions))
    require(VERSION.fullmatch(version) is not None, f"invalid package version: {version}")
    require(len(source_files) == len(expected_keys) * 3, "promotion set contains duplicate or unexpected files")

    package_records: list[dict[str, Any]] = []
    mex_by_target: dict[str, str] = {}
    for target in TARGETS:
        for profile in selected_profiles:
            key = (target, profile)
            archive, _version, _extension = archives[key]
            sidecar = sidecars[key]
            qualification_path = qualifications[key]
            archive_sha256 = parse_sidecar(archive, sidecar)
            qualification_sha256 = digest(qualification_path)
            qualification = validate_qualification(
                qualification_path,
                target=target,
                profile=profile,
                source_revision=source_revision,
                qualification_run_id=qualification_run_id,
                qualification_run_attempt=qualification_run_attempt,
            )
            manifest = load_embedded_manifest(archive)
            validate_manifest_binding(
                archive,
                manifest,
                qualification,
                qualification_sha256,
                target=target,
                profile=profile,
                version=version,
                source_revision=source_revision,
                qualification_run_id=qualification_run_id,
            )
            mex_sha256 = str(qualification["mex_sha256"])
            previous_mex = mex_by_target.setdefault(target, mex_sha256)
            require(previous_mex == mex_sha256, f"profiles use different MEX bytes for target {target}")
            package_records.append(
                {
                    "platform": target,
                    "profile": profile,
                    "archive": archive.name,
                    "archive_sha256": archive_sha256,
                    "qualification": qualification_path.name,
                    "qualification_sha256": qualification_sha256,
                    "mex_sha256": mex_sha256,
                }
            )

    files = [
        {
            "path": relative,
            "bytes": path.stat().st_size,
            "sha256": digest(path),
        }
        for path, relative in sorted(source_files, key=lambda item: item[1])
    ]
    return {
        "format_version": 1,
        "repository": REPOSITORY,
        "source_revision": source_revision,
        "qualification_run_id": qualification_run_id,
        "qualification_run_attempt": qualification_run_attempt,
        "qualification_run_identity": f"github-actions:{qualification_run_id}",
        "python_abi": "cp314",
        "version": version,
        "targets": list(TARGETS),
        "profiles": list(selected_profiles),
        "packages": package_records,
        "files": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--qualification-run-id", required=True)
    parser.add_argument("--qualification-run-attempt", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    output = args.output.resolve()
    try:
        require(output.parent == root, "release index output must be directly inside the promotion root")
        require(output.name == "reference-release-index.json", "release index output has an unexpected filename")
        require(not output.exists() and not output.is_symlink(), "release index output already exists")
        payload = build_release_index(
            root,
            source_revision=args.source_revision,
            qualification_run_id=args.qualification_run_id,
            qualification_run_attempt=args.qualification_run_attempt,
        )
    except ReleaseIndexError as exc:
        raise SystemExit(str(exc)) from exc

    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"reference_release_index={output}")
    print(f"reference_release_profiles={','.join(payload['profiles'])}")
    print(f"reference_release_packages={len(payload['packages'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
