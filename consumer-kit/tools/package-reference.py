#!/usr/bin/env python3
"""Assemble a deterministic, offline-testable reference bridge package.

Packaging is deliberately a copy-only operation.  It consumes a previously
qualified MEX and an already-staged producer runtime, verifies that the build
and qualification evidence bind to the MEX bytes, and never invokes a compiler
or dependency resolver.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
from typing import Any, Iterable
import zipfile


FORMAT_VERSION = 1
PYTHON_ABI = "cp314"
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
PLATFORMS = ("win-x64", "linux-x64", "macos-arm64", "macos-x64")
PROFILES = ("core", "numpy", "fenics")
MATLAB_ALIASES = {
    "win-x64": "bridge.mexw64",
    "linux-x64": "bridge.mexa64",
    "macos-arm64": "bridge.mexmaca64",
    "macos-x64": "bridge.mexmaci64",
}
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
VERSION = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?$")
SAFE_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PackageError(ValueError):
    """An input cannot produce an attestable reference package."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PackageError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path, label: str) -> dict[str, Any]:
    require(path.is_file(), f"{label} does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"invalid {label}: {path}: {exc}") from exc
    require(isinstance(payload, dict), f"{label} must contain a JSON object: {path}")
    return payload


def _walk_json(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = path + (key_text,)
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
        if not key_path or not isinstance(value, str) or not HEX64.fullmatch(value):
            continue
        if predicate(key_path[-1].lower(), key_path):
            values.append(value.lower())
    return sorted(set(values))


def evidence_mex_digests(payload: dict[str, Any]) -> list[str]:
    def is_mex_binding(key: str, key_path: tuple[str, ...]) -> bool:
        if key in {"mex_sha256", "candidate_sha256", "qualified_mex_sha256", "bridge_sha256"}:
            return True
        if key == "sha256" and any(part.lower() in {"mex", "candidate", "bridge"} for part in key_path[:-1]):
            return True
        if key == "artifact_sha256":
            # Some evidence writers call the qualified MEX an artifact.  Accept
            # that form only when a sibling artifact name is visibly a MEX.
            return any(part.lower() in {"mex", "candidate", "bridge"} for part in key_path[:-1])
        return False

    values = _digest_values(payload, is_mex_binding)
    if values:
        return values

    # Top-level artifact_sha256 is accepted only when artifact names a MEX.
    artifact = payload.get("artifact")
    artifact_sha = payload.get("artifact_sha256")
    if (
        isinstance(artifact, str)
        and ".mex" in artifact.lower()
        and isinstance(artifact_sha, str)
        and HEX64.fullmatch(artifact_sha)
    ):
        return [artifact_sha.lower()]
    return []


def require_mex_binding(payload: dict[str, Any], expected: str, label: str) -> None:
    digests = evidence_mex_digests(payload)
    require(digests, f"{label} does not bind a MEX/candidate SHA-256")
    require(expected in digests, f"{label} does not bind the supplied qualified MEX SHA-256")


def _first_scalar(payload: dict[str, Any], names: set[str]) -> str | int | None:
    for key_path, value in _walk_json(payload):
        if key_path and key_path[-1].lower() in names and isinstance(value, (str, int)):
            return value
    return None


def validate_evidence_identity(
    payload: dict[str, Any], *, label: str, platform: str, profile: str, mock_revision: str
) -> None:
    for key, expected in (
        ("platform", platform),
        ("profile", profile),
        ("mock_revision", mock_revision),
    ):
        observed = payload.get(key)
        if observed is not None:
            require(observed == expected, f"{label} {key} does not match package identity")


def _tree_entries(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append(
                {
                    "path": rel,
                    "kind": "symlink",
                    "target": os.readlink(path),
                    "mode": stat.S_IMODE(path.lstat().st_mode),
                }
            )
        elif path.is_file():
            entries.append(
                {
                    "path": rel,
                    "kind": "file",
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
            )
    return entries


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_runtime_path(raw: Any, label: str) -> PurePosixPath:
    require(isinstance(raw, str) and raw, f"{label} path must be a non-empty string")
    require("\\" not in raw and ":" not in raw and "//" not in raw, f"unsafe {label} path: {raw!r}")
    path = PurePosixPath(raw)
    require(not path.is_absolute(), f"absolute {label} path: {raw!r}")
    require(all(part not in {"", ".", ".."} for part in path.parts), f"unsafe {label} path: {raw!r}")
    return path


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
        # Early staged-runtime evidence used by downstream consumers did not
        # expose the producer file inventory. Keep that evidence readable while
        # fully re-attesting current producer manifests when the inventory exists.
        return None
    require(isinstance(raw_entries, list), f"{label} files must be an array")
    entries: dict[str, dict[str, Any]] = {}
    for raw in raw_entries:
        require(isinstance(raw, dict), f"{label} contains a non-object file entry")
        rel = _safe_runtime_path(raw.get("path"), label).as_posix()
        require(rel not in entries, f"duplicate {label} path: {rel}")
        kind = raw.get("kind", "file")
        mode = _manifest_mode(raw.get("mode"), f"{label} {rel}")
        if kind == "file":
            digest = raw.get("sha256")
            size = raw.get("bytes") if raw.get("bytes") is not None else raw.get("size")
            if raw.get("bytes") is not None and raw.get("size") is not None:
                require(raw.get("bytes") == raw.get("size"), f"conflicting byte counts for {label} {rel}")
            require(isinstance(digest, str) and HEX64.fullmatch(digest), f"invalid SHA-256 for {label} {rel}")
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
                require(isinstance(digest, str) and HEX64.fullmatch(digest), f"invalid symlink SHA-256 for {label} {rel}")
                require(isinstance(size, int) and not isinstance(size, bool) and size >= 0, f"invalid symlink byte count for {label} {rel}")
                entries[rel] = {
                    "kind": "symlink",
                    "target_bytes": size,
                    "target_sha256": digest.lower(),
                    "mode": mode,
                }
    return entries


def verify_staged_runtime_tree(
    runtime: Path, *, platform: str, profile: str, manifest_sha256s: list[str]
) -> None:
    """Re-attest staged producer bytes from the embedded producer manifests."""

    manifest_names = ["runtime-manifest.json"]
    if profile == "fenics":
        manifest_names.append("fenics-embed-manifest.json")
    expected: dict[str, dict[str, Any]] = {}
    inventories_complete = True
    runtime_root = runtime.resolve()
    for manifest_name in manifest_names:
        manifest_path = runtime / manifest_name
        require(manifest_path.is_file() and not manifest_path.is_symlink(), f"verified runtime has no regular {manifest_name}")
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

    actual = {entry["path"]: entry for entry in _tree_entries(runtime)}
    require(set(actual) == set(expected), "staged runtime inventory differs from embedded producer manifests")
    check_modes = os.name != "nt" and platform != "win-x64"
    for rel, wanted in expected.items():
        observed = actual[rel]
        require(observed["kind"] == wanted["kind"], f"staged runtime entry kind differs: {rel}")
        path = runtime / rel
        if wanted["kind"] == "file":
            require(
                observed["bytes"] == wanted["bytes"] and observed["sha256"] == wanted["sha256"],
                f"staged runtime file differs from embedded producer manifest: {rel}",
            )
        else:
            target = os.readlink(path)
            resolved = (path.parent / target).resolve(strict=False)
            require(_inside(runtime_root, resolved), f"staged runtime symlink escapes runtime root: {rel} -> {target}")
            if "target" in wanted:
                require(target == wanted["target"], f"staged runtime symlink target differs: {rel}")
            else:
                target_bytes = target.encode("utf-8")
                require(
                    len(target_bytes) == wanted["target_bytes"]
                    and hashlib.sha256(target_bytes).hexdigest() == wanted["target_sha256"],
                    f"staged runtime symlink target differs: {rel}",
                )
        if check_modes and wanted.get("mode") is not None:
            require(observed["mode"] == wanted["mode"], f"staged runtime mode differs from embedded producer manifest: {rel}")


def copy_runtime_exact(source: Path, destination: Path) -> None:
    require(source.is_dir(), f"runtime directory does not exist: {source}")
    before = _tree_entries(source)
    shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    after = _tree_entries(destination)
    require(before == after, "copied runtime differs from the staged qualified runtime")


def copy_file(source: Path, destination: Path, label: str) -> None:
    require(source.is_file(), f"{label} does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    require(sha256_file(source) == sha256_file(destination), f"{label} changed while packaging")


def safe_package_name(name: str) -> str:
    require(bool(SAFE_PACKAGE_NAME.fullmatch(name)), f"unsafe package name: {name!r}")
    return name


def runtime_facts_from_receipt(
    payload: dict[str, Any], *, platform: str, profile: str, runtime_manifest_sha256: str
) -> dict[str, Any]:
    """Resolve producer revision and immutable hashes for the staged profile."""
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
        archive_hashes = []
        manifest_hashes = []
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
        matches = [
            item
            for item in layers
            if isinstance(item, dict) and item.get("profile") == profile
        ]
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
                if isinstance(value, str) and HEX64.fullmatch(value):
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
        # Compatibility for early staged evidence records which only carried
        # the archive hash. Current stage evidence always carries manifest hashes.
        manifest_hashes = [runtime_manifest_sha256.lower()]
    return {
        "repository": repository,
        "revision": revision,
        "archive_sha256s": sorted(set(archive_hashes)),
        "manifest_sha256s": sorted(set(manifest_hashes)),
    }


def qualification_identity(payload: dict[str, Any], mock_revision: str) -> dict[str, str]:
    workflow_run_id = payload.get("workflow_run_id")
    source_revision = payload.get("source_revision")
    require(isinstance(workflow_run_id, (str, int)) and str(workflow_run_id).strip(), "qualification evidence has no workflow run id")
    require(isinstance(source_revision, str) and HEX40.fullmatch(source_revision), "qualification evidence has invalid source revision")
    require(source_revision.lower() == mock_revision.lower(), "qualification source revision differs from mock source revision")
    run_id = str(workflow_run_id)
    run_identity = run_id if run_id.startswith("github-actions:") else f"github-actions:{run_id}"
    return {
        "run_identity": run_identity,
        "source_revision": source_revision.lower(),
    }


def native_closure_record(payload: dict[str, Any], platform: str) -> dict[str, str]:
    target = payload.get("target")
    if target is not None:
        require(target == platform, "native closure evidence target differs from package platform")
    status = payload.get("status", "pass")
    require(status in {"pass", "not-applicable"}, "native closure evidence has invalid status")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        checks = payload.get("checks")
        if isinstance(checks, dict) and checks:
            parts = [
                f"{key}={json.dumps(checks[key], sort_keys=True, separators=(',', ':'))}"
                for key in sorted(checks)
            ]
            summary = "; ".join(parts)
        else:
            summary = f"native closure evidence recorded for {platform}"
    return {"status": status, "summary": summary.strip()}


def _manifest_file_entries(package_root: Path) -> list[dict[str, Any]]:
    omitted = {"reference-manifest.json", "SHA256SUMS.txt"}
    return [entry for entry in _tree_entries(package_root) if entry["path"] not in omitted]


def _write_sha256sums(package_root: Path) -> None:
    lines: list[str] = []
    for path in sorted(package_root.rglob("*"), key=lambda item: item.relative_to(package_root).as_posix()):
        rel = path.relative_to(package_root).as_posix()
        if rel == "SHA256SUMS.txt" or path.is_dir():
            continue
        if path.is_symlink() and not path.resolve().is_file():
            # Directory symlinks are attested by reference-manifest.json.  The
            # standard sha256sum file format can only checksum file content.
            continue
        lines.append(f"{sha256_file(path)}  {rel}")
    (package_root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _archive_paths(root: Path) -> list[Path]:
    return sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())


def write_zip(package_root: Path, output: Path) -> None:
    prefix = package_root.name
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        root_info = zipfile.ZipInfo(prefix + "/", FIXED_ZIP_TIME)
        root_info.create_system = 3
        root_info.external_attr = (stat.S_IFDIR | 0o755) << 16
        archive.writestr(root_info, b"")
        for path in _archive_paths(package_root):
            rel = path.relative_to(package_root).as_posix()
            arcname = f"{prefix}/{rel}"
            if path.is_symlink():
                info = zipfile.ZipInfo(arcname, FIXED_ZIP_TIME)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, os.readlink(path).encode("utf-8"))
            elif path.is_dir():
                info = zipfile.ZipInfo(arcname + "/", FIXED_ZIP_TIME)
                info.create_system = 3
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, b"")
            else:
                info = zipfile.ZipInfo(arcname, FIXED_ZIP_TIME)
                info.create_system = 3
                mode = stat.S_IMODE(path.stat().st_mode) or 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


def write_tar_gz(package_root: Path, output: Path) -> None:
    prefix = package_root.name
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w", format=tarfile.PAX_FORMAT) as archive:
                root_info = tarfile.TarInfo(prefix)
                root_info.type = tarfile.DIRTYPE
                root_info.mode = 0o755
                root_info.mtime = root_info.uid = root_info.gid = 0
                archive.addfile(root_info)
                for path in _archive_paths(package_root):
                    rel = path.relative_to(package_root).as_posix()
                    info = archive.gettarinfo(str(path), arcname=f"{prefix}/{rel}")
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    if info.isfile():
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)


def _copy_named_inputs(paths: list[Path], destination: Path, label: str) -> list[str]:
    copied: list[str] = []
    seen: set[str] = set()
    for source in paths:
        require(source.is_file(), f"{label} does not exist: {source}")
        require(source.name not in seen, f"duplicate {label} basename: {source.name}")
        seen.add(source.name)
        target = destination / source.name
        copy_file(source, target, label)
        copied.append(target.as_posix())
    return copied


def _readme_text(platform: str, profile: str, alias: str, launcher: str) -> str:
    launch_example = (
        ".\\run-local-test.ps1 -Octave C:\\path\\to\\octave-cli.exe"
        if platform == "win-x64"
        else "./run-local-test.sh --octave /path/to/octave-cli"
    )
    return f"""# Reference bridge local test

This package contains a prebuilt, previously qualified public mock bridge and its
immutable producer runtime.  Local testing does not compile or download anything.

- Platform: `{platform}`
- Profile: `{profile}`
- Python ABI: `{PYTHON_ABI}`
- GNU Octave is the supported local host prerequisite.
- A bootstrap Python 3 interpreter is required only to run `verify-package.py`.
- `bridge.mex` and `{alias}` contain identical MEX bytes.  The MATLAB-style alias
  is supplied for filename compatibility only; GNU Octave results do not qualify
  MATLAB behavior.

From any working directory, run the bundled launcher with an explicit Octave path:

```text
{launch_example}
```

The launcher first performs complete offline package verification, then invokes
`host/mock_smoke.m` in a fresh Octave process with this package as the runtime
root.  Success prints `reference_bridge_result=pass`.

You can verify without starting Octave:

```text
python verify-package.py
```

The package must remain unchanged.  Verification rejects missing, added, or
modified payload files, a changed MEX alias, and evidence that no longer binds to
the qualified candidate.
"""


def package_reference(args: argparse.Namespace) -> Path:
    mex = Path(args.mex).resolve()
    runtime = Path(args.runtime).resolve()
    receipt_path = Path(args.runtime_receipt).resolve()
    build_path = Path(args.build_evidence).resolve()
    qualification_path = Path(args.qualification_evidence).resolve()
    closure_path = Path(args.native_closure_evidence).resolve()
    backend = Path(args.backend).resolve()
    host_fixture = Path(args.host_fixture).resolve()
    output = Path(args.output).resolve()
    tool_root = Path(__file__).resolve().parent

    require(mex.is_file(), f"qualified MEX does not exist: {mex}")
    require(runtime.is_dir(), f"runtime directory does not exist: {runtime}")
    require(backend.is_file(), f"mock backend does not exist: {backend}")
    require(host_fixture.is_file(), f"host fixture does not exist: {host_fixture}")
    require(output != mex and output != receipt_path, "archive output overlaps an input")
    require(args.producer_repository == "precsim/python-numpy-fenics", "unexpected producer repository")
    require(bool(HEX40.fullmatch(args.producer_revision)), "producer revision must be a 40-character Git SHA")
    require(bool(HEX40.fullmatch(args.mock_revision)), "mock revision must be a 40-character Git SHA")
    require(bool(VERSION.fullmatch(args.version)), "package version must be vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rcN")

    verify_tool = tool_root / "verify-package.py"
    shell_launcher = tool_root / "run-local-test.sh"
    ps_launcher = tool_root / "run-local-test.ps1"
    require(verify_tool.is_file(), f"offline verifier is missing: {verify_tool}")
    launcher_source = ps_launcher if args.platform == "win-x64" else shell_launcher
    require(launcher_source.is_file(), f"local test launcher is missing: {launcher_source}")

    mex_sha = sha256_file(mex)
    receipt = load_json(receipt_path, "runtime receipt")
    build = load_json(build_path, "build evidence")
    qualification = load_json(qualification_path, "qualification evidence")
    native_closure = load_json(closure_path, "native closure evidence")

    require_mex_binding(build, mex_sha, "build evidence")
    require_mex_binding(qualification, mex_sha, "qualification evidence")
    if build.get("target") is not None:
        require(build.get("target") == args.platform, "build evidence target differs from package platform")
    validate_evidence_identity(
        build,
        label="build evidence",
        platform=args.platform,
        profile=args.profile,
        mock_revision=args.mock_revision,
    )
    validate_evidence_identity(
        qualification,
        label="qualification evidence",
        platform=args.platform,
        profile=args.profile,
        mock_revision=args.mock_revision,
    )
    if qualification.get("python_abi") is not None:
        require(qualification.get("python_abi") == PYTHON_ABI, "qualification evidence Python ABI differs from package")
    qualification_record = qualification_identity(qualification, args.mock_revision)
    closure_record = native_closure_record(native_closure, args.platform)

    source_runtime_manifest = runtime / "runtime-manifest.json"
    require(source_runtime_manifest.is_file(), "verified runtime has no runtime-manifest.json")
    runtime_manifest_sha = sha256_file(source_runtime_manifest)
    producer_identity = runtime_facts_from_receipt(
        receipt,
        platform=args.platform,
        profile=args.profile,
        runtime_manifest_sha256=runtime_manifest_sha,
    )
    require(producer_identity["repository"] == args.producer_repository, "runtime receipt producer repository differs from package")
    require(producer_identity["revision"] == args.producer_revision.lower(), "runtime receipt producer revision differs from package")
    verify_staged_runtime_tree(
        runtime,
        platform=args.platform,
        profile=args.profile,
        manifest_sha256s=producer_identity["manifest_sha256s"],
    )

    package_name = args.package_name or (
        f"reference-bridge-{args.platform}-{PYTHON_ABI}-{args.profile}-{args.version}"
    )
    package_name = safe_package_name(package_name)
    alias = MATLAB_ALIASES[args.platform]
    archive_format = args.archive_format
    if archive_format is None:
        archive_format = "zip" if args.platform == "win-x64" else "tar.gz"
    require(
        (archive_format == "zip" and output.name.endswith(".zip"))
        or (archive_format == "tar.gz" and output.name.endswith(".tar.gz")),
        "archive output extension does not match archive format",
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reference-package-", dir=str(output.parent)) as temp:
        package_root = Path(temp) / package_name
        package_root.mkdir()

        copy_file(mex, package_root / "bridge.mex", "qualified MEX")
        copy_file(mex, package_root / alias, "qualified MEX alias")
        require(
            sha256_file(package_root / "bridge.mex") == sha256_file(package_root / alias) == mex_sha,
            "MEX aliases are not byte-identical",
        )
        copy_runtime_exact(runtime, package_root / "runtime")
        copy_file(backend, package_root / "python" / "mock_backend.py", "mock backend")
        copy_file(host_fixture, package_root / "host" / "mock_smoke.m", "host fixture")

        evidence_dir = package_root / "evidence"
        evidence_dir.mkdir()
        evidence_targets = {
            "runtime_receipt": evidence_dir / "runtime-receipt.json",
            "build": evidence_dir / "build-evidence.json",
            "qualification": evidence_dir / "qualification-evidence.json",
            "native_closure": evidence_dir / "native-closure-evidence.json",
        }
        for source, target, label in (
            (receipt_path, evidence_targets["runtime_receipt"], "runtime receipt"),
            (build_path, evidence_targets["build"], "build evidence"),
            (qualification_path, evidence_targets["qualification"], "qualification evidence"),
            (closure_path, evidence_targets["native_closure"], "native closure evidence"),
        ):
            copy_file(source, target, label)

        copy_file(verify_tool, package_root / "verify-package.py", "offline verifier")
        launcher_name = "run-local-test.ps1" if args.platform == "win-x64" else "run-local-test.sh"
        copy_file(launcher_source, package_root / launcher_name, "local test launcher")
        if args.platform != "win-x64":
            (package_root / launcher_name).chmod(0o755)
        (package_root / "verify-package.py").chmod(0o755)

        license_paths = [Path(item).resolve() for item in args.license]
        provenance_paths = [Path(item).resolve() for item in args.provenance]
        if license_paths:
            _copy_named_inputs(license_paths, package_root / "licenses", "license file")
        if provenance_paths:
            _copy_named_inputs(provenance_paths, evidence_dir / "provenance", "provenance file")

        readme = package_root / "README-LOCAL-TEST.md"
        readme.write_text(
            _readme_text(args.platform, args.profile, alias, launcher_name),
            encoding="utf-8",
            newline="\n",
        )
        (package_root / "profile.txt").write_text(
            args.profile + "\n", encoding="utf-8", newline="\n"
        )

        runtime_manifest = package_root / "runtime" / "runtime-manifest.json"
        require(
            runtime_manifest.is_file() and sha256_file(runtime_manifest) == runtime_manifest_sha,
            "packaged runtime manifest differs from staged runtime",
        )

        manifest: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "package": package_name,
            "version": args.version,
            "platform": args.platform,
            "profile": args.profile,
            "python_abi": PYTHON_ABI,
            "producer": {
                "repository": producer_identity["repository"],
                "revision": producer_identity["revision"],
                "receipt_path": "evidence/runtime-receipt.json",
                "receipt_sha256": sha256_file(evidence_targets["runtime_receipt"]),
                "archive_sha256s": producer_identity["archive_sha256s"],
                "manifest_sha256s": producer_identity["manifest_sha256s"],
            },
            "mock": {
                "source_revision": args.mock_revision.lower(),
                "backend_path": "python/mock_backend.py",
                "host_fixture_path": "host/mock_smoke.m",
            },
            "qualification": {
                "run_identity": qualification_record["run_identity"],
                "evidence_path": "evidence/qualification-evidence.json",
                "evidence_sha256": sha256_file(evidence_targets["qualification"]),
            },
            "build": {
                "evidence_path": "evidence/build-evidence.json",
                "evidence_sha256": sha256_file(evidence_targets["build"]),
            },
            "native_closure": {
                "evidence_path": "evidence/native-closure-evidence.json",
                "evidence_sha256": sha256_file(evidence_targets["native_closure"]),
                "summary": closure_record["summary"],
            },
            "runtime": {
                "path": "runtime",
                "manifest": {
                    "path": "runtime/runtime-manifest.json",
                    "sha256": runtime_manifest_sha,
                },
            },
            "mex": {
                "path": "bridge.mex",
                "matlab_alias": alias,
                "sha256": mex_sha,
            },
            "local_host": {
                "launcher": launcher_name,
                "octave_required": True,
                "network_required": False,
                "compiler_required": False,
                "matlab_qualified": False,
            },
            "files": _manifest_file_entries(package_root),
        }
        (package_root / "reference-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        _write_sha256sums(package_root)

        # Verify with the same implementation that ships in the package before
        # committing the archive. Import by path to avoid requiring installation.
        import importlib.util
        import sys

        verifier_path = package_root / "verify-package.py"
        spec = importlib.util.spec_from_file_location("reference_package_verifier", verifier_path)
        require(spec is not None and spec.loader is not None, "cannot load packaged verifier")
        verifier = importlib.util.module_from_spec(spec)
        previous_dont_write_bytecode = sys.dont_write_bytecode
        try:
            sys.dont_write_bytecode = True
            spec.loader.exec_module(verifier)
            verifier.verify_package(package_root)
        finally:
            sys.dont_write_bytecode = previous_dont_write_bytecode

        output.unlink(missing_ok=True)
        if archive_format == "zip":
            write_zip(package_root, output)
        else:
            write_tar_gz(package_root, output)

    print(f"reference_package={output}")
    print(f"reference_package_sha256={sha256_file(output)}")
    print(f"qualified_mex_sha256={mex_sha}")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mex", required=True, help="previously qualified MEX candidate")
    parser.add_argument("--runtime", required=True, help="already-staged verified producer runtime")
    parser.add_argument("--runtime-receipt", required=True)
    parser.add_argument("--build-evidence", "--build-provenance", dest="build_evidence", required=True)
    parser.add_argument("--qualification-evidence", "--qualification", dest="qualification_evidence", required=True)
    parser.add_argument("--native-closure-evidence", "--native-audit", dest="native_closure_evidence", required=True)
    parser.add_argument("--backend", required=True, help="mock_backend.py source fixture")
    parser.add_argument("--host-fixture", required=True, help="mock_smoke.m host fixture")
    parser.add_argument("--platform", choices=PLATFORMS, required=True)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--producer-repository", default="precsim/python-numpy-fenics")
    parser.add_argument("--producer-revision", required=True)
    parser.add_argument("--mock-revision", required=True)
    parser.add_argument("--version", default="v0.1.0")
    parser.add_argument("--package-name")
    parser.add_argument("--output", required=True)
    parser.add_argument("--archive-format", choices=("zip", "tar.gz"))
    parser.add_argument("--license", action="append", default=[], help="redistribution license file")
    parser.add_argument("--provenance", action="append", default=[], help="additional provenance file")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        package_reference(args)
    except PackageError as exc:
        raise SystemExit(f"reference package error: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
