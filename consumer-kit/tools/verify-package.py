#!/usr/bin/env python3
"""Verify an extracted reference bridge package entirely offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Iterable


FORMAT_VERSION = 1
PYTHON_ABI = "cp314"
PLATFORMS = {"win-x64", "linux-x64", "macos-arm64", "macos-x64"}
PROFILES = {"core", "numpy", "fenics"}
MATLAB_ALIASES = {
    "win-x64": "bridge.mexw64",
    "linux-x64": "bridge.mexa64",
    "macos-arm64": "bridge.mexmaca64",
    "macos-x64": "bridge.mexmaci64",
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?$")
SAFE_LEAF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TOP_LEVEL_FIELDS = {
    "format_version",
    "package",
    "version",
    "platform",
    "profile",
    "python_abi",
    "producer",
    "mock",
    "qualification",
    "build",
    "native_closure",
    "runtime",
    "mex",
    "local_host",
    "files",
}


class VerificationError(ValueError):
    """The extracted package does not match its offline attestation."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    require(path.is_file() and not path.is_symlink(), f"missing {label}: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"invalid {label}: {path}: {exc}") from exc
    require(isinstance(payload, dict), f"{label} must contain a JSON object")
    return payload


def safe_relative_path(raw: Any) -> PurePosixPath:
    require(isinstance(raw, str) and raw, "inventory path must be a non-empty string")
    require("\\" not in raw and ":" not in raw and "//" not in raw, f"unsafe inventory path: {raw!r}")
    path = PurePosixPath(raw)
    require(not path.is_absolute(), f"absolute inventory path: {raw!r}")
    require(all(part not in {"", ".", ".."} for part in path.parts), f"unsafe inventory path: {raw!r}")
    return path


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _walk_json(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path + (str(key),)
            yield child_path, child
            yield from _walk_json(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = path + (str(index),)
            yield child_path, child
            yield from _walk_json(child, child_path)


def _digest_values(payload: dict[str, Any], predicate) -> list[str]:
    values: list[str] = []
    for key_path, value in _walk_json(payload):
        if not key_path or not isinstance(value, str) or not HEX64.fullmatch(value.lower()):
            continue
        if predicate(key_path[-1].lower(), key_path):
            values.append(value.lower())
    return sorted(set(values))


def evidence_mex_digests(payload: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for key_path, value in _walk_json(payload):
        if not key_path or not isinstance(value, str) or not HEX64.fullmatch(value.lower()):
            continue
        key = key_path[-1].lower()
        if key in {"mex_sha256", "candidate_sha256", "qualified_mex_sha256", "bridge_sha256"}:
            result.add(value.lower())
        elif key == "sha256" and any(part.lower() in {"mex", "candidate", "bridge"} for part in key_path[:-1]):
            result.add(value.lower())
    artifact = payload.get("artifact")
    artifact_sha = payload.get("artifact_sha256")
    if (
        isinstance(artifact, str)
        and ".mex" in artifact.lower()
        and isinstance(artifact_sha, str)
        and HEX64.fullmatch(artifact_sha.lower())
    ):
        result.add(artifact_sha.lower())
    return result


def runtime_facts_from_receipt(
    payload: dict[str, Any], *, platform: str, profile: str, runtime_manifest_sha256: str
) -> dict[str, Any]:
    profiles = payload.get("profiles")
    if isinstance(profiles, dict):
        platform_record = profiles.get(platform)
        require(isinstance(platform_record, dict), f"runtime receipt has no {platform} record")
        profile_record = platform_record.get(profile)
        require(isinstance(profile_record, dict), f"runtime receipt has no {platform}/{profile} record")
        repository = str(payload.get("producer_repository", ""))
        revision = str(profile_record.get("producer_revision", "")).lower()
        records = [profile_record]
        for key in ("base", "overlay"):
            child = profile_record.get(key)
            if isinstance(child, dict):
                records.append(child)
        archive_hashes: list[str] = []
        manifest_hashes: list[str] = []
        for record in records:
            archive = record.get("archive")
            require(isinstance(archive, dict), "runtime receipt layer has no archive record")
            archive_hashes.append(str(archive.get("sha256", "")).lower())
            manifest_hashes.append(str(archive.get("manifest_sha256", "")).lower())
    else:
        require(payload.get("platform") == platform, "runtime evidence platform differs from package")
        require(payload.get("profile") == profile, "runtime evidence profile differs from package")
        layers = payload.get("layers")
        require(isinstance(layers, list) and layers, "staged runtime evidence has no materialized layers")
        matches = [item for item in layers if isinstance(item, dict) and item.get("profile") == profile]
        require(len(matches) == 1, "staged runtime evidence has no unique selected profile layer")
        repository = "precsim/python-numpy-fenics"
        revision = str(payload.get("release_revision") or matches[0].get("producer_revision") or "").lower()
        archive_hashes = [
            str(item.get("artifact_sha256", "")).lower()
            for item in layers
            if isinstance(item, dict)
        ]
        manifest_hashes = _digest_values(payload, lambda key, _path: key.endswith("manifest_sha256"))
        for item in layers:
            if not isinstance(item, dict):
                continue
            manifest = item.get("manifest")
            if isinstance(manifest, dict):
                value = manifest.get("sha256")
                if isinstance(value, str) and HEX64.fullmatch(value.lower()):
                    manifest_hashes.append(value.lower())

    require(repository == "precsim/python-numpy-fenics", "unexpected producer repository")
    require(bool(HEX40.fullmatch(revision)), "runtime receipt has invalid producer revision")
    require(archive_hashes and all(HEX64.fullmatch(value) for value in archive_hashes), "runtime receipt has invalid archive SHA-256")
    require(all(HEX64.fullmatch(value) for value in manifest_hashes), "runtime receipt has invalid manifest SHA-256")
    if manifest_hashes:
        require(
            runtime_manifest_sha256.lower() in manifest_hashes,
            "runtime-manifest.json is not bound by the runtime receipt",
        )
    else:
        manifest_hashes = [runtime_manifest_sha256.lower()]
    return {
        "repository": repository,
        "revision": revision,
        "archive_sha256s": sorted(set(archive_hashes)),
        "manifest_sha256s": sorted(set(manifest_hashes)),
    }


def qualification_run_identity(payload: dict[str, Any]) -> str:
    run_id = payload.get("workflow_run_id")
    require(isinstance(run_id, (str, int)) and str(run_id).strip(), "qualification evidence has no workflow run id")
    text = str(run_id)
    return text if text.startswith("github-actions:") else f"github-actions:{text}"


def native_closure_summary(payload: dict[str, Any], platform: str) -> str:
    target = payload.get("target")
    if target is not None:
        require(target == platform, "native closure evidence target differs from package platform")
    summary = payload.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    checks = payload.get("checks")
    if isinstance(checks, dict) and checks:
        return "; ".join(
            f"{key}={json.dumps(checks[key], sort_keys=True, separators=(',', ':'))}"
            for key in sorted(checks)
        )
    return f"native closure evidence recorded for {platform}"


def _actual_inventory(root: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            target = os.readlink(path)
            resolved = (path.parent / target).resolve(strict=False)
            require(_inside(root, resolved), f"package symlink escapes package root: {rel} -> {target}")
            entries[rel] = {
                "path": rel,
                "kind": "symlink",
                "target": target,
                "mode": stat.S_IMODE(path.lstat().st_mode),
            }
        elif path.is_file():
            entries[rel] = {
                "path": rel,
                "kind": "file",
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
    return entries


def _manifest_mode(raw: Any, label: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, str) and re.fullmatch(r"0o[0-7]{1,4}", raw):
        raw = int(raw, 8)
    require(isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= 0o7777, f"invalid {label} mode")
    return raw


def _runtime_manifest_entries(payload: dict[str, Any], label: str) -> dict[str, dict[str, Any]] | None:
    raw_entries = payload.get("files")
    if raw_entries is None:
        return None
    require(isinstance(raw_entries, list), f"{label} files must be an array")
    entries: dict[str, dict[str, Any]] = {}
    for raw in raw_entries:
        require(isinstance(raw, dict), f"{label} contains a non-object file entry")
        rel = safe_relative_path(raw.get("path")).as_posix()
        require(rel not in entries, f"duplicate {label} path: {rel}")
        kind = raw.get("kind", "file")
        mode = _manifest_mode(raw.get("mode"), f"{label} {rel}")
        if kind == "file":
            digest = raw.get("sha256")
            size = raw.get("bytes") if raw.get("bytes") is not None else raw.get("size")
            if raw.get("bytes") is not None and raw.get("size") is not None:
                require(raw.get("bytes") == raw.get("size"), f"conflicting byte counts for {label} {rel}")
            require(isinstance(digest, str) and HEX64.fullmatch(digest.lower()), f"invalid SHA-256 for {label} {rel}")
            require(isinstance(size, int) and not isinstance(size, bool) and size >= 0, f"invalid byte count for {label} {rel}")
            entries[rel] = {"kind": "file", "bytes": size, "sha256": digest.lower(), "mode": mode}
        else:
            require(kind == "symlink", f"unsupported {label} entry kind for {rel}: {kind!r}")
            target = raw.get("target")
            if isinstance(target, str) and target:
                entries[rel] = {"kind": "symlink", "target": target, "mode": mode}
            else:
                digest = raw.get("sha256")
                size = raw.get("bytes") if raw.get("bytes") is not None else raw.get("size")
                require(isinstance(digest, str) and HEX64.fullmatch(digest.lower()), f"invalid symlink SHA-256 for {label} {rel}")
                require(isinstance(size, int) and not isinstance(size, bool) and size >= 0, f"invalid symlink byte count for {label} {rel}")
                entries[rel] = {
                    "kind": "symlink",
                    "target_bytes": size,
                    "target_sha256": digest.lower(),
                    "mode": mode,
                }
    return entries


def verify_embedded_runtime_manifests(
    runtime: Path, *, platform: str, profile: str, manifest_sha256s: list[str]
) -> None:
    manifest_names = ["runtime-manifest.json"]
    if profile == "fenics":
        manifest_names.append("fenics-embed-manifest.json")
    expected: dict[str, dict[str, Any]] = {}
    inventories_complete = True
    for manifest_name in manifest_names:
        manifest_path = runtime / manifest_name
        require(manifest_path.is_file() and not manifest_path.is_symlink(), f"packaged runtime has no regular {manifest_name}")
        manifest_sha = sha256_file(manifest_path)
        require(manifest_sha in manifest_sha256s, f"{manifest_name} is not bound by runtime receipt manifest hashes")
        payload = load_json(manifest_path, manifest_name)
        if payload.get("platform") is not None:
            require(payload.get("platform") == platform, f"{manifest_name} platform differs from package")
        if payload.get("python_abi") is not None:
            require(payload.get("python_abi") == PYTHON_ABI, f"{manifest_name} Python ABI differs from package")
        manifest_profile = payload.get("profile")
        if manifest_name == "fenics-embed-manifest.json":
            require(manifest_profile in {None, "fenics"}, "FEniCS overlay manifest has unexpected profile")
        elif profile != "fenics":
            require(manifest_profile in {None, profile}, "runtime manifest profile differs from package")

        manifest_entries = _runtime_manifest_entries(payload, manifest_name)
        if manifest_entries is None:
            inventories_complete = False
            continue
        for rel, record in manifest_entries.items():
            require(rel not in expected, f"producer runtime manifests claim the same path: {rel}")
            expected[rel] = record
        require(manifest_name not in expected, f"producer manifest inventories itself: {manifest_name}")
        expected[manifest_name] = {
            "kind": "file",
            "bytes": manifest_path.stat().st_size,
            "sha256": manifest_sha,
            "mode": None,
        }

    if not inventories_complete:
        return

    actual = _actual_inventory(runtime)
    require(set(actual) == set(expected), "packaged runtime inventory differs from embedded producer manifests")
    check_modes = os.name != "nt" and platform != "win-x64"
    for rel, wanted in expected.items():
        observed = actual[rel]
        require(observed["kind"] == wanted["kind"], f"packaged runtime entry kind differs: {rel}")
        if wanted["kind"] == "file":
            require(
                observed["bytes"] == wanted["bytes"] and observed["sha256"] == wanted["sha256"],
                f"packaged runtime file differs from embedded producer manifest: {rel}",
            )
        else:
            target = observed["target"]
            if "target" in wanted:
                require(target == wanted["target"], f"packaged runtime symlink target differs: {rel}")
            else:
                target_bytes = target.encode("utf-8")
                require(
                    len(target_bytes) == wanted["target_bytes"]
                    and hashlib.sha256(target_bytes).hexdigest() == wanted["target_sha256"],
                    f"packaged runtime symlink target differs: {rel}",
                )
        if check_modes and wanted.get("mode") is not None:
            require(observed["mode"] == wanted["mode"], f"packaged runtime mode differs from embedded producer manifest: {rel}")


def _normalize_manifest_entry(raw: Any) -> dict[str, Any]:
    require(isinstance(raw, dict), "manifest file entry must be an object")
    rel = safe_relative_path(raw.get("path")).as_posix()
    kind = raw.get("kind")
    if kind == "file":
        require(set(raw) == {"path", "kind", "bytes", "sha256", "mode"}, f"file inventory has unexpected fields: {rel}")
        digest = raw.get("sha256")
        size = raw.get("bytes")
        mode = raw.get("mode")
        require(isinstance(digest, str) and HEX64.fullmatch(digest), f"invalid SHA-256 for {rel}")
        require(isinstance(size, int) and not isinstance(size, bool) and size >= 0, f"invalid byte count for {rel}")
        require(isinstance(mode, int) and not isinstance(mode, bool) and 0 <= mode <= 0o7777, f"invalid mode for {rel}")
        return {"path": rel, "kind": "file", "bytes": size, "sha256": digest, "mode": mode}
    require(kind == "symlink", f"unsupported inventory kind for {rel}")
    require(set(raw) == {"path", "kind", "target", "mode"}, f"symlink inventory has unexpected fields: {rel}")
    target = raw.get("target")
    mode = raw.get("mode")
    require(isinstance(target, str) and target, f"invalid symlink target for {rel}")
    require(isinstance(mode, int) and not isinstance(mode, bool) and 0 <= mode <= 0o7777, f"invalid mode for {rel}")
    return {"path": rel, "kind": "symlink", "target": target, "mode": mode}


def _parse_sha256sums(path: Path) -> dict[str, str]:
    require(path.is_file() and not path.is_symlink(), "missing SHA256SUMS.txt")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise VerificationError(f"cannot read SHA256SUMS.txt: {exc}") from exc
    require(lines, "SHA256SUMS.txt is empty")
    result: dict[str, str] = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        require(match is not None, f"malformed SHA256SUMS.txt line: {line!r}")
        digest, raw_path = match.groups()
        rel = safe_relative_path(raw_path).as_posix()
        require(rel != "SHA256SUMS.txt", "SHA256SUMS.txt cannot checksum itself")
        require(rel not in result, f"duplicate checksum entry: {rel}")
        result[rel] = digest
    return result


def _hashed_evidence(root: Path, record: Any, *, label: str, expected_path: str) -> Path:
    require(isinstance(record, dict), f"missing {label} record")
    require(record.get("evidence_path") == expected_path, f"unexpected {label} path")
    digest = record.get("evidence_sha256")
    require(isinstance(digest, str) and HEX64.fullmatch(digest), f"invalid {label} SHA-256")
    path = root.joinpath(*PurePosixPath(expected_path).parts)
    require(path.is_file() and not path.is_symlink(), f"missing {label}: {expected_path}")
    require(sha256_file(path) == digest, f"changed {label}: {expected_path}")
    return path


def _validate_manifest_contract(manifest: dict[str, Any], root_name: str) -> None:
    require(set(manifest) == TOP_LEVEL_FIELDS, "reference manifest fields differ from reference-package-v1")
    require(manifest.get("format_version") == FORMAT_VERSION, "unsupported reference manifest format")
    package = manifest.get("package")
    require(isinstance(package, str) and SAFE_LEAF.fullmatch(package), "invalid package identity")
    require(package == root_name, "package directory name differs from manifest identity")
    version = manifest.get("version")
    require(isinstance(version, str) and VERSION.fullmatch(version), "invalid package version")
    platform = manifest.get("platform")
    profile = manifest.get("profile")
    require(platform in PLATFORMS, f"unsupported package platform: {platform!r}")
    require(profile in PROFILES, f"unsupported package profile: {profile!r}")
    require(manifest.get("python_abi") == PYTHON_ABI, "reference package Python ABI is not cp314")

    producer = manifest.get("producer")
    producer_fields = {"repository", "revision", "receipt_path", "receipt_sha256", "archive_sha256s", "manifest_sha256s"}
    require(isinstance(producer, dict) and set(producer) == producer_fields, "invalid producer manifest record")
    require(producer.get("repository") == "precsim/python-numpy-fenics", "invalid producer repository")
    require(isinstance(producer.get("revision"), str) and HEX40.fullmatch(producer["revision"]), "invalid producer revision")
    require(producer.get("receipt_path") == "evidence/runtime-receipt.json", "unexpected runtime receipt path")
    require(isinstance(producer.get("receipt_sha256"), str) and HEX64.fullmatch(producer["receipt_sha256"]), "invalid runtime receipt SHA-256")
    for key in ("archive_sha256s", "manifest_sha256s"):
        values = producer.get(key)
        require(isinstance(values, list) and values, f"producer {key} is missing")
        require(len(values) == len(set(values)), f"producer {key} contains duplicates")
        require(all(isinstance(value, str) and HEX64.fullmatch(value) for value in values), f"invalid producer {key}")

    mock = manifest.get("mock")
    require(isinstance(mock, dict) and set(mock) == {"source_revision", "backend_path", "host_fixture_path"}, "invalid mock manifest record")
    require(isinstance(mock.get("source_revision"), str) and HEX40.fullmatch(mock["source_revision"]), "invalid mock source revision")
    require(mock.get("backend_path") == "python/mock_backend.py", "unexpected mock backend path")
    require(mock.get("host_fixture_path") == "host/mock_smoke.m", "unexpected host fixture path")

    qualification = manifest.get("qualification")
    require(isinstance(qualification, dict) and set(qualification) == {"run_identity", "evidence_path", "evidence_sha256"}, "invalid qualification manifest record")
    require(isinstance(qualification.get("run_identity"), str) and qualification["run_identity"], "invalid qualification run identity")
    require(qualification.get("evidence_path") == "evidence/qualification-evidence.json", "unexpected qualification evidence path")
    require(isinstance(qualification.get("evidence_sha256"), str) and HEX64.fullmatch(qualification["evidence_sha256"]), "invalid qualification evidence SHA-256")

    build = manifest.get("build")
    require(isinstance(build, dict) and set(build) == {"evidence_path", "evidence_sha256"}, "invalid build manifest record")
    require(build.get("evidence_path") == "evidence/build-evidence.json", "unexpected build evidence path")
    require(isinstance(build.get("evidence_sha256"), str) and HEX64.fullmatch(build["evidence_sha256"]), "invalid build evidence SHA-256")

    native = manifest.get("native_closure")
    require(isinstance(native, dict) and set(native) == {"evidence_path", "evidence_sha256", "summary"}, "invalid native closure manifest record")
    require(native.get("evidence_path") == "evidence/native-closure-evidence.json", "unexpected native closure evidence path")
    require(isinstance(native.get("evidence_sha256"), str) and HEX64.fullmatch(native["evidence_sha256"]), "invalid native closure evidence SHA-256")
    require(isinstance(native.get("summary"), str) and native["summary"], "invalid native closure summary")

    runtime = manifest.get("runtime")
    require(isinstance(runtime, dict) and set(runtime) == {"path", "manifest"}, "invalid runtime manifest record")
    require(runtime.get("path") == "runtime", "unexpected runtime path")
    runtime_manifest = runtime.get("manifest")
    require(isinstance(runtime_manifest, dict) and set(runtime_manifest) == {"path", "sha256"}, "invalid runtime manifest identity")
    require(runtime_manifest.get("path") == "runtime/runtime-manifest.json", "unexpected runtime manifest path")
    require(isinstance(runtime_manifest.get("sha256"), str) and HEX64.fullmatch(runtime_manifest["sha256"]), "invalid runtime manifest SHA-256")

    mex = manifest.get("mex")
    require(isinstance(mex, dict) and set(mex) == {"path", "matlab_alias", "sha256"}, "invalid MEX manifest record")
    require(mex.get("path") == "bridge.mex", "canonical MEX path must be bridge.mex")
    require(mex.get("matlab_alias") == MATLAB_ALIASES[platform], "MATLAB MEX alias does not match platform")
    require(isinstance(mex.get("sha256"), str) and HEX64.fullmatch(mex["sha256"]), "invalid MEX SHA-256")

    local_host = manifest.get("local_host")
    expected_launcher = "run-local-test.ps1" if platform == "win-x64" else "run-local-test.sh"
    require(
        isinstance(local_host, dict)
        and set(local_host) == {"launcher", "octave_required", "network_required", "compiler_required", "matlab_qualified"},
        "invalid local host manifest record",
    )
    require(local_host.get("launcher") == expected_launcher, "local launcher does not match platform")
    require(local_host.get("octave_required") is True, "GNU Octave prerequisite is not recorded")
    require(local_host.get("network_required") is False, "local test must be offline")
    require(local_host.get("compiler_required") is False, "local test must not require a compiler")
    require(local_host.get("matlab_qualified") is False, "Octave qualification cannot claim MATLAB qualification")

    files = manifest.get("files")
    require(isinstance(files, list) and files, "reference manifest files must be a non-empty list")
    seen: set[str] = set()
    for raw in files:
        entry = _normalize_manifest_entry(raw)
        require(entry["path"] not in {"reference-manifest.json", "SHA256SUMS.txt"}, "metadata cannot self-attest in file inventory")
        require(entry["path"] not in seen, f"duplicate manifest file entry: {entry['path']}")
        seen.add(entry["path"])


def verify_package(root: Path) -> dict[str, Any]:
    root = root.resolve()
    require(root.is_dir(), f"package root does not exist: {root}")
    manifest = load_json(root / "reference-manifest.json", "reference manifest")
    _validate_manifest_contract(manifest, root.name)
    platform = str(manifest["platform"])
    profile = str(manifest["profile"])

    actual = _actual_inventory(root)
    expected: dict[str, dict[str, Any]] = {}
    for raw in manifest["files"]:
        entry = _normalize_manifest_entry(raw)
        expected[entry["path"]] = entry
    actual_payload = {
        rel: item
        for rel, item in actual.items()
        if rel not in {"reference-manifest.json", "SHA256SUMS.txt"}
    }
    require(set(expected) == set(actual_payload), "manifest inventory differs from extracted package inventory")
    for rel, entry in expected.items():
        observed = actual_payload[rel]
        if platform == "win-x64":
            # Windows ZIP extractors do not preserve POSIX permission bits,
            # including when the Windows archive is inspected on Unix.
            # Retain byte/hash/type/symlink attestation but do not reject an
            # otherwise identical Windows package on host-dependent st_mode.
            require(
                {key: value for key, value in entry.items() if key != "mode"}
                == {key: value for key, value in observed.items() if key != "mode"},
                f"package entry differs from manifest: {rel}",
            )
        else:
            require(entry == observed, f"package entry differs from manifest: {rel}")

    checksums = _parse_sha256sums(root / "SHA256SUMS.txt")
    checkable = {
        rel
        for rel in actual
        if rel != "SHA256SUMS.txt"
        and (not (root / rel).is_symlink() or (root / rel).resolve().is_file())
    }
    require(set(checksums) == checkable, "SHA256SUMS.txt inventory differs from extracted package files")
    for rel, digest in checksums.items():
        require(sha256_file(root / rel) == digest, f"checksum mismatch: {rel}")

    mex = manifest["mex"]
    mex_sha = str(mex["sha256"])
    alias = str(mex["matlab_alias"])
    require(sha256_file(root / "bridge.mex") == mex_sha, "bridge.mex differs from manifest identity")
    require(sha256_file(root / alias) == mex_sha, "MATLAB alias is not byte-identical to bridge.mex")

    producer = manifest["producer"]
    receipt_path = root / "evidence" / "runtime-receipt.json"
    receipt = load_json(receipt_path, "runtime receipt")
    require(sha256_file(receipt_path) == producer["receipt_sha256"], "runtime receipt differs from manifest identity")

    runtime_manifest = root / "runtime" / "runtime-manifest.json"
    require(runtime_manifest.is_file() and not runtime_manifest.is_symlink(), "packaged runtime-manifest.json is missing")
    runtime_manifest_sha = sha256_file(runtime_manifest)
    require(runtime_manifest_sha == manifest["runtime"]["manifest"]["sha256"], "runtime manifest differs from manifest identity")
    require(runtime_manifest_sha in producer["manifest_sha256s"], "runtime manifest is not bound by producer manifest hashes")
    receipt_facts = runtime_facts_from_receipt(
        receipt,
        platform=platform,
        profile=profile,
        runtime_manifest_sha256=runtime_manifest_sha,
    )
    for key in ("repository", "revision", "archive_sha256s", "manifest_sha256s"):
        require(receipt_facts[key] == producer[key], f"producer {key} differs from runtime receipt")
    verify_embedded_runtime_manifests(
        root / "runtime",
        platform=platform,
        profile=profile,
        manifest_sha256s=receipt_facts["manifest_sha256s"],
    )

    build_path = _hashed_evidence(
        root,
        manifest["build"],
        label="build evidence",
        expected_path="evidence/build-evidence.json",
    )
    build = load_json(build_path, "build evidence")
    require(mex_sha in evidence_mex_digests(build), "build evidence no longer binds bridge.mex")
    if build.get("target") is not None:
        require(build.get("target") == platform, "build evidence target differs from package platform")

    qualification_path = _hashed_evidence(
        root,
        manifest["qualification"],
        label="qualification evidence",
        expected_path="evidence/qualification-evidence.json",
    )
    qualification = load_json(qualification_path, "qualification evidence")
    require(mex_sha in evidence_mex_digests(qualification), "qualification evidence no longer binds bridge.mex")
    if qualification.get("platform") is not None:
        require(qualification.get("platform") == platform, "qualification platform differs from package")
    if qualification.get("profile") is not None:
        require(qualification.get("profile") == profile, "qualification profile differs from package")
    if qualification.get("python_abi") is not None:
        require(qualification.get("python_abi") == PYTHON_ABI, "qualification Python ABI differs from package")
    source_revision = qualification.get("source_revision")
    require(
        isinstance(source_revision, str)
        and HEX40.fullmatch(source_revision)
        and source_revision == manifest["mock"]["source_revision"],
        "qualification source revision differs from mock source revision",
    )
    require(qualification_run_identity(qualification) == manifest["qualification"]["run_identity"], "qualification run identity differs from evidence")

    closure_path = _hashed_evidence(
        root,
        manifest["native_closure"],
        label="native closure evidence",
        expected_path="evidence/native-closure-evidence.json",
    )
    native_closure = load_json(closure_path, "native closure evidence")
    require(native_closure_summary(native_closure, platform) == manifest["native_closure"]["summary"], "native closure summary differs from evidence")

    require((root / "runtime").is_dir(), "packaged runtime is missing")
    require((root / manifest["mock"]["backend_path"]).is_file(), "mock backend is missing")
    require((root / manifest["mock"]["host_fixture_path"]).is_file(), "host fixture is missing")
    require((root / "verify-package.py").is_file(), "offline verifier is missing")
    require((root / "README-LOCAL-TEST.md").is_file(), "local test README is missing")
    require((root / manifest["local_host"]["launcher"]).is_file(), "local test launcher is missing")
    profile_marker = root / "profile.txt"
    require(profile_marker.is_file() and not profile_marker.is_symlink(), "profile.txt is missing")
    try:
        marker_value = profile_marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise VerificationError(f"cannot read profile.txt: {exc}") from exc
    require(marker_value == profile, "profile.txt differs from manifest profile")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    args = parser.parse_args()
    try:
        manifest = verify_package(Path(args.root))
    except VerificationError as exc:
        print("reference_package_result=fail")
        print(f"reference_package_error={exc}")
        return 1
    print("reference_package_result=pass")
    print(f"reference_package={manifest['package']}")
    print(f"qualified_mex_sha256={manifest['mex']['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
