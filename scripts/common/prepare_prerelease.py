#!/usr/bin/env python3
"""Collect verified producer artifacts for a GitHub pre-release."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import shutil
import sys
import tarfile
import zipfile


TAG_RE = re.compile(
    r"^runtime-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)-rc(?P<rc>[0-9]+)$"
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
FORBIDDEN_SUFFIXES = (".mex", ".mexa64", ".mexmaci64", ".mexw64")
PRIVATE_SOURCE_ROOTS = {"src", "private"}
PRIVATE_SOURCE_MARKERS = (
    "python-matlab-octave-bridge",
    "private-consumer",
    "private_consumer",
)
NUMPY_SITE_PREFIXES = {
    "win-x64": "Lib/site-packages/",
    "macos-arm64": "Python.framework/Versions/3.14/lib/python3.14/site-packages/",
    "macos-x64": "Python.framework/Versions/3.14/lib/python3.14/site-packages/",
    "linux-x64": "lib/python3.14/site-packages/",
}
EXPECTED_WORKFLOW_COVERAGE = {
    "qualify-windows-core-numpy": {
        ("win-x64", "core"),
        ("win-x64", "numpy"),
    },
    "qualify-windows-fenics": {
        ("win-x64", "fenics"),
    },
    "qualify-macos-core-numpy": {
        ("macos-arm64", "core"),
        ("macos-arm64", "numpy"),
        ("macos-x64", "core"),
        ("macos-x64", "numpy"),
    },
    "qualify-macos-fenics": {
        ("macos-arm64", "fenics"),
        ("macos-x64", "fenics"),
    },
    "qualify-linux-core-numpy": {
        ("linux-x64", "core"),
        ("linux-x64", "numpy"),
    },
    "qualify-linux-fenics": {
        ("linux-x64", "fenics"),
    },
}
CORE_NUMPY_WORKFLOWS = {
    "qualify-windows-core-numpy",
    "qualify-macos-core-numpy",
    "qualify-linux-core-numpy",
}
FENICS_WORKFLOWS = set(EXPECTED_WORKFLOW_COVERAGE) - CORE_NUMPY_WORKFLOWS
EXPECTED_QUALIFICATION_CHECKS = {
    ("win-x64", "core"): {
        "isolated_cpython": "pass",
        "tkinter": "pass",
        "tcl_tk_8_6_18": "pass",
        "numpy_absent": "pass",
        "pe_native_closure": "pass",
        "relocation": "pass",
        "private_consumer_checkout": "not-applicable",
    },
    ("win-x64", "numpy"): {
        "inherited_core_identity": "pass",
        "pinned_numpy_wheel": "pass",
        "openblas": "pass",
        "blas_lapack_functional": "pass",
        "pe_native_closure": "pass",
        "relocation": "pass",
        "private_consumer_checkout": "not-applicable",
    },
    ("win-x64", "fenics"): {
        "package_lock_replay": "pass",
        "package_hash_verification": "pass",
        "dependency_solver": "not-applicable",
        "producer_python_excluded": "pass",
        "producer_numpy_excluded": "pass",
        "inherited_numpy_identity": "pass",
        "relocation": "pass",
        "isolated_cpython": "pass",
        "fresh_ffcx_jit": "pass",
        "serial_petsc_solve": "pass",
        "private_consumer_checkout": "not-applicable",
    },
    ("macos-arm64", "core"): {
        "pinned_cpython_asset": "pass",
        "relocated_framework": "pass",
        "tkinter": "pass",
        "tcl_tk_8_6_18": "pass",
        "relocated_tcl_tk": "pass",
        "native_closure": "pass",
        "macos_floor": "pass",
        "numpy_absent": "pass",
        "private_consumer_checkout": "not-applicable",
    },
    ("macos-x64", "core"): {
        "pinned_cpython_asset": "pass",
        "relocated_framework": "pass",
        "tkinter": "pass",
        "tcl_tk_8_6_18": "pass",
        "relocated_tcl_tk": "pass",
        "native_closure": "pass",
        "macos_floor": "pass",
        "numpy_absent": "pass",
        "private_consumer_checkout": "not-applicable",
    },
    ("macos-arm64", "numpy"): {
        "inherited_core_identity": "pass",
        "pinned_numpy_wheel": "pass",
        "accelerate": "pass",
        "blas_lapack_functional": "pass",
        "relocated_tcl_tk": "pass",
        "relocation": "pass",
        "native_closure": "pass",
        "macos_floor": "pass",
        "private_consumer_checkout": "not-applicable",
    },
    ("macos-x64", "numpy"): {
        "inherited_core_identity": "pass",
        "pinned_numpy_wheel": "pass",
        "accelerate": "pass",
        "blas_lapack_functional": "pass",
        "relocated_tcl_tk": "pass",
        "relocation": "pass",
        "native_closure": "pass",
        "macos_floor": "pass",
        "private_consumer_checkout": "not-applicable",
    },
    ("macos-arm64", "fenics"): {
        "package_lock_replay": "pass",
        "package_hash_verification": "pass",
        "public_numpy_base": "pass",
        "inherited_numpy_identity": "pass",
        "producer_python_excluded": "pass",
        "producer_numpy_excluded": "pass",
        "fresh_ffcx_jit": "pass",
        "serial_petsc_solve": "pass",
        "deterministic_archive": "pass",
        "native_closure": "pass",
        "private_consumer_checkout": "not-applicable",
    },
    ("macos-x64", "fenics"): {
        "package_lock_replay": "pass",
        "package_hash_verification": "pass",
        "public_numpy_base": "pass",
        "inherited_numpy_identity": "pass",
        "producer_python_excluded": "pass",
        "producer_numpy_excluded": "pass",
        "fresh_ffcx_jit": "pass",
        "serial_petsc_solve": "pass",
        "deterministic_archive": "pass",
        "native_closure": "pass",
        "private_consumer_checkout": "not-applicable",
    },
    ("linux-x64", "core"): {
        "pinned_manylinux": "pass",
        "glibc_floor": "pass",
        "isolated_cpython": "pass",
        "tcl_tk_8_6_18": "pass",
        "native_closure": "pass",
        "numpy_absent": "pass",
        "relocation": "pass",
        "private_consumer_checkout": "not-applicable",
    },
    ("linux-x64", "numpy"): {
        "pinned_manylinux": "pass",
        "glibc_floor": "pass",
        "inherited_core_identity": "pass",
        "numpy_source_build": "pass",
        "private_openblas": "pass",
        "blas_lapack_functional": "pass",
        "native_closure": "pass",
        "relocation": "pass",
        "private_consumer_checkout": "not-applicable",
    },
    ("linux-x64", "fenics"): {
        "pinned_manylinux": "pass",
        "glibc_floor": "pass",
        "package_lock_replay": "pass",
        "package_hash_verification": "pass",
        "dependency_solver_disabled": "pass",
        "stock_package_reuse": "pass",
        "public_numpy_base": "pass",
        "inherited_numpy_identity": "pass",
        "producer_python_excluded": "pass",
        "producer_numpy_excluded": "pass",
        "hostile_ambient_state": "pass",
        "relocation": "pass",
        "fresh_ffcx_jit": "pass",
        "serial_petsc_poisson": "pass",
        "mpich_provider": "pass",
        "native_closure": "pass",
        "native_symbol_ceilings": "pass",
        "deterministic_archive": "pass",
        "private_consumer_checkout": "not-applicable",
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_by_name(root: Path, name: str) -> list[Path]:
    return sorted(path for path in root.rglob(name) if path.is_file())


def copy_unique(source: Path, destination: Path, copied: dict[str, str]) -> None:
    digest = sha256_file(source)
    previous = copied.get(source.name)
    if previous is not None and previous != digest:
        raise SystemExit(f"conflicting pre-release basename: {source.name}")
    if previous is None:
        shutil.copy2(source, destination / source.name)
        copied[source.name] = digest


def write_unique(
    name: str,
    data: bytes,
    destination: Path,
    copied: dict[str, str],
) -> str:
    digest = hashlib.sha256(data).hexdigest()
    previous = copied.get(name)
    if previous is not None and previous != digest:
        raise SystemExit(f"conflicting pre-release basename: {name}")
    if previous is None:
        (destination / name).write_bytes(data)
        copied[name] = digest
    return digest


def is_forbidden_private_source_path(parts: tuple[str, ...]) -> bool:
    lowered = tuple(part.lower() for part in parts if part)
    if not lowered:
        return False
    if lowered[0] in PRIVATE_SOURCE_ROOTS:
        return True
    for index, part in enumerate(lowered):
        if part != "private":
            continue
        # PETSc ships public package headers under include/petsc/private.
        # This is a third-party implementation namespace, not a private
        # downstream consumer source tree.
        if index >= 2 and lowered[index - 2 : index] == ("include", "petsc"):
            continue
        return True
    joined = "/".join(lowered)
    return any(marker in joined for marker in PRIVATE_SOURCE_MARKERS)


def validate_archive_members(artifact: Path) -> None:
    if zipfile.is_zipfile(artifact):
        with zipfile.ZipFile(artifact) as archive:
            names = [info.filename for info in archive.infolist()]
    elif tarfile.is_tarfile(artifact):
        with tarfile.open(artifact, mode="r:*") as archive:
            names = [member.name for member in archive.getmembers()]
    else:
        raise SystemExit(f"producer artifact is not a supported ZIP/tar archive: {artifact}")

    seen: set[str] = set()
    for raw_name in names:
        normalized = raw_name.replace("\\", "/")
        member = PurePosixPath(normalized)
        if not normalized or member.is_absolute() or ".." in member.parts:
            raise SystemExit(f"producer artifact contains unsafe member path: {artifact}: {raw_name!r}")
        key = member.as_posix().rstrip("/")
        if key in seen:
            raise SystemExit(f"producer artifact contains duplicate member path: {artifact}: {key!r}")
        seen.add(key)
        lower_name = member.name.lower()
        if lower_name.endswith(FORBIDDEN_SUFFIXES) or ".mex" in lower_name:
            raise SystemExit(
                f"private-consumer MEX-like artifact is forbidden inside producer artifact: {artifact}: {key}"
            )
        if is_forbidden_private_source_path(member.parts):
            raise SystemExit(
                f"private-consumer source payload is forbidden inside producer artifact: {artifact}: {key}"
            )


def read_archive_json_member(artifact: Path, expected_path: str) -> dict[str, object]:
    raw: bytes | None = None
    if zipfile.is_zipfile(artifact):
        with zipfile.ZipFile(artifact) as archive:
            matches = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.replace("\\", "/") == expected_path
            ]
            if len(matches) == 1:
                raw = archive.read(matches[0])
    elif tarfile.is_tarfile(artifact):
        with tarfile.open(artifact, mode="r:*") as archive:
            matches = [
                member
                for member in archive.getmembers()
                if member.isfile() and member.name.replace("\\", "/") == expected_path
            ]
            if len(matches) == 1:
                stream = archive.extractfile(matches[0])
                if stream is not None:
                    raw = stream.read()
    if raw is None:
        raise SystemExit(f"producer artifact is missing required JSON member {expected_path}: {artifact}")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise SystemExit(f"invalid JSON member {expected_path} in {artifact}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"JSON member {expected_path} is not an object in {artifact}")
    return payload


def validate_qualification_checks(
    platform: str,
    profile: str,
    checks: object,
    qualification: Path,
) -> None:
    expected = EXPECTED_QUALIFICATION_CHECKS.get((platform, profile))
    if expected is None:
        raise SystemExit(f"qualification has no declared check contract: {(platform, profile)!r}")
    if not isinstance(checks, dict):
        raise SystemExit(f"qualification checks must be an object: {qualification}")
    actual_names = set(checks)
    expected_names = set(expected)
    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names)
    invalid = sorted(
        name
        for name in expected_names & actual_names
        if checks.get(name) != expected[name]
    )
    if missing or extra or invalid:
        raise SystemExit(
            f"qualification check contract mismatch for {(platform, profile)!r}: "
            f"missing={missing!r} extra={extra!r} invalid={invalid!r}: {qualification}"
        )


def numpy_tree_identity_from_manifest(
    manifest: dict[str, object],
    *,
    context: str,
) -> dict[str, object]:
    platform = str(manifest.get("platform", ""))
    prefix = NUMPY_SITE_PREFIXES.get(platform)
    if prefix is None:
        raise SystemExit(f"unsupported NumPy runtime platform for base identity: {platform!r}: {context}")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise SystemExit(f"NumPy runtime manifest has no file inventory: {context}")

    entries: dict[str, tuple[str, str]] = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise SystemExit(f"NumPy runtime manifest contains a non-object file entry: {context}")
        raw_path = str(entry.get("path", "")).replace("\\", "/")
        path = PurePosixPath(raw_path)
        if not raw_path or path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"NumPy runtime manifest contains unsafe file path {raw_path!r}: {context}")
        if not raw_path.startswith(prefix):
            continue
        relative = raw_path[len(prefix):]
        if not (
            relative.startswith("numpy/")
            or relative.startswith("numpy.libs/")
            or re.match(r"^numpy-[^/]+\.dist-info/", relative)
        ):
            continue
        if relative in entries:
            raise SystemExit(f"NumPy runtime manifest contains duplicate payload path {relative!r}: {context}")
        kind = str(entry.get("kind", "file"))
        if kind == "symlink":
            target = entry.get("target")
            if not isinstance(target, str) or not target:
                raise SystemExit(f"NumPy runtime manifest has invalid symlink target for {relative}: {context}")
            entries[relative] = ("L", target)
        elif kind == "file":
            digest = str(entry.get("sha256", "")).lower()
            if SHA256_RE.fullmatch(digest) is None:
                raise SystemExit(f"NumPy runtime manifest has invalid SHA-256 for {relative}: {context}")
            entries[relative] = ("F", digest)
        else:
            raise SystemExit(f"NumPy runtime manifest has unsupported kind {kind!r} for {relative}: {context}")
    if not entries:
        raise SystemExit(f"NumPy runtime manifest contains no NumPy payload: {context}")

    aggregate = hashlib.sha256()
    for relative, (kind, identity) in sorted(entries.items()):
        aggregate.update(kind.encode("ascii") + b"\0")
        aggregate.update(relative.encode("utf-8") + b"\0")
        aggregate.update(identity.encode("utf-8") + b"\n")
    return {"tree_sha256": aggregate.hexdigest(), "entry_count": len(entries)}


def validate_cross_profile_base_identities(
    records: dict[tuple[str, str], dict[str, object]],
) -> list[dict[str, object]]:
    bindings: list[dict[str, object]] = []
    for platform in sorted(NUMPY_SITE_PREFIXES):
        numpy_record = records.get((platform, "numpy"))
        fenics_record = records.get((platform, "fenics"))
        if numpy_record is None or fenics_record is None:
            raise SystemExit(f"missing NumPy/FEniCS artifact pair for base identity validation: {platform}")
        numpy_manifest = numpy_record["manifest"]
        fenics_manifest = fenics_record["manifest"]
        if not isinstance(numpy_manifest, dict) or not isinstance(fenics_manifest, dict):
            raise SystemExit(f"invalid stored manifest pair for {platform}")
        expected_tree = numpy_tree_identity_from_manifest(
            numpy_manifest,
            context=str(numpy_record["artifact"]),
        )
        fenics_numpy = fenics_manifest.get("numpy")
        if not isinstance(fenics_numpy, dict):
            raise SystemExit(f"FEniCS manifest has no NumPy base identity: {fenics_record['artifact']}")
        actual_tree_sha = str(fenics_numpy.get("base_tree_sha256", "")).lower()
        actual_entry_count = fenics_numpy.get("base_entry_count")
        if (
            actual_tree_sha != expected_tree["tree_sha256"]
            or actual_entry_count != expected_tree["entry_count"]
        ):
            raise SystemExit(
                f"FEniCS NumPy base tree identity does not match qualified NumPy artifact for {platform}: "
                f"tree={actual_tree_sha!r}/{expected_tree['tree_sha256']!r} "
                f"entries={actual_entry_count!r}/{expected_tree['entry_count']!r}"
            )

        expected_manifest_sha = str(numpy_record["manifest_sha256"])
        actual_manifest_sha = str(fenics_numpy.get("base_manifest_sha256", "")).lower()
        if not actual_manifest_sha and platform == "linux-x64":
            source_provenance = fenics_record.get("source_provenance")
            if isinstance(source_provenance, dict):
                numpy_base = source_provenance.get("numpy_base")
                if isinstance(numpy_base, dict):
                    actual_manifest_sha = str(numpy_base.get("manifest_sha256", "")).lower()
        if actual_manifest_sha != expected_manifest_sha:
            raise SystemExit(
                f"FEnICS NumPy base manifest identity does not match qualified NumPy artifact for {platform}: "
                f"{actual_manifest_sha!r} != {expected_manifest_sha!r}"
            )

        bindings.append(
            {
                "platform": platform,
                "fenics_artifact": fenics_record["artifact"],
                "fenics_artifact_sha256": fenics_record["artifact_sha256"],
                "numpy_artifact": numpy_record["artifact"],
                "numpy_artifact_sha256": numpy_record["artifact_sha256"],
                "numpy_manifest_sha256": expected_manifest_sha,
                "numpy_tree_sha256": expected_tree["tree_sha256"],
                "numpy_entry_count": expected_tree["entry_count"],
            }
        )
    return bindings


def read_embedded_manifest(artifact: Path, profile: str) -> tuple[bytes, dict[str, object]]:
    expected_name = "fenics-embed-manifest.json" if profile == "fenics" else "runtime-manifest.json"
    candidates: list[tuple[str, bytes]] = []

    if zipfile.is_zipfile(artifact):
        with zipfile.ZipFile(artifact) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if PurePosixPath(info.filename).name == expected_name:
                    candidates.append((info.filename, archive.read(info)))
    elif tarfile.is_tarfile(artifact):
        with tarfile.open(artifact, mode="r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                if PurePosixPath(member.name).name != expected_name:
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    raise SystemExit(f"cannot read embedded manifest from {artifact}: {member.name}")
                candidates.append((member.name, stream.read()))
    else:
        raise SystemExit(f"producer artifact is not a supported ZIP/tar archive: {artifact}")

    if len(candidates) != 1:
        names = [name for name, _ in candidates]
        raise SystemExit(
            f"producer artifact must contain exactly one {expected_name}: {artifact} candidates={names!r}"
        )
    raw = candidates[0][1]
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise SystemExit(f"embedded manifest is invalid JSON in {artifact}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"embedded manifest is not an object in {artifact}")
    return raw, payload


def validate_checksum_sidecar(
    sidecar: Path,
    artifact_name: str,
    expected_sha: str,
) -> None:
    lines = [
        line.strip()
        for line in sidecar.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if len(lines) != 1:
        raise SystemExit(f"artifact checksum sidecar must contain exactly one record: {sidecar}")
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", lines[0])
    if match is None:
        raise SystemExit(f"artifact checksum sidecar is malformed: {sidecar}")
    recorded_sha = match.group(1).lower()
    recorded_name = PurePosixPath(match.group(2).replace("\\", "/")).name
    if recorded_sha != expected_sha or recorded_name != artifact_name:
        raise SystemExit(
            f"artifact checksum sidecar identity mismatch: {sidecar} "
            f"sha={recorded_sha} name={recorded_name!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--include-fenics",
        action="store_true",
        help="include the deferred FEniCS profile qualifications and artifacts",
    )
    args = parser.parse_args()

    selected_workflows = (
        set(EXPECTED_WORKFLOW_COVERAGE)
        if args.include_fenics
        else set(CORE_NUMPY_WORKFLOWS)
    )
    selected_profiles = ["core", "numpy"] + (["fenics"] if args.include_fenics else [])

    tag_match = TAG_RE.fullmatch(args.tag)
    if tag_match is None:
        raise SystemExit(
            "producer pre-release tag must match runtime-vX.Y.Z-rcN"
        )
    release_version = tag_match.group("version")

    source_root = Path(args.input).resolve()
    output = Path(args.output).resolve()
    if not source_root.is_dir():
        raise SystemExit(f"input directory does not exist: {source_root}")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        lower = path.name.lower()
        if lower.endswith(FORBIDDEN_SUFFIXES) or ".mex" in lower:
            raise SystemExit(f"private-consumer MEX-like artifact is forbidden: {path}")
        relative_parts = path.relative_to(source_root).parts
        payload_parts = relative_parts[1:] if len(relative_parts) > 1 else relative_parts
        if is_forbidden_private_source_path(payload_parts):
            raise SystemExit(f"private-consumer source payload is forbidden in producer pre-release input: {path}")

    qualifications = sorted(source_root.rglob("qualification-*.json"))
    if not qualifications:
        raise SystemExit("no producer qualification records were downloaded")

    copied: dict[str, str] = {}
    qualification_summaries = []
    coverage: dict[str, set[tuple[str, str]]] = {
        workflow: set() for workflow in selected_workflows
    }
    producer_revision: str | None = None
    source_runs: dict[str, dict[str, object]] = {}
    seen_qualifications: set[tuple[str, str]] = set()
    artifact_records: dict[tuple[str, str], dict[str, object]] = {}
    for qualification in qualifications:
        payload = json.loads(qualification.read_text(encoding="utf-8"))
        if payload.get("format_version") != 1:
            raise SystemExit(f"unsupported qualification format: {qualification}")
        if payload.get("producer_repository") != "precsim/python-numpy-fenics":
            raise SystemExit(f"unexpected qualification producer: {qualification}")

        relative = qualification.relative_to(source_root)
        if len(relative.parts) < 2:
            raise SystemExit(f"qualification is not nested under a run directory: {qualification}")
        run_dir = source_root / relative.parts[0]
        metadata_path = run_dir / "run-metadata.json"
        if not metadata_path.is_file():
            raise SystemExit(f"qualification run metadata is missing: {metadata_path}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        run_id = str(metadata.get("databaseId", ""))
        workflow_name = str(metadata.get("workflowName", ""))
        head_sha = str(metadata.get("headSha", ""))
        head_branch = str(metadata.get("headBranch", ""))
        conclusion = str(metadata.get("conclusion", ""))
        event = str(metadata.get("event", ""))
        if conclusion != "success":
            raise SystemExit(f"qualification run is not successful: {metadata_path}")
        if workflow_name not in selected_workflows:
            raise SystemExit(f"unexpected qualification workflow: {workflow_name!r}")
        if event not in {"push", "workflow_dispatch"}:
            raise SystemExit(
                f"qualification run must be a push/workflow_dispatch run, not {event!r}: {metadata_path}"
            )
        if head_branch != "main":
            raise SystemExit(
                f"qualification run must target producer main, not {head_branch!r}: {metadata_path}"
            )
        if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise SystemExit(f"qualification run has invalid head SHA: {metadata_path}")
        if str(payload.get("workflow_run_id", "")) != run_id:
            raise SystemExit(
                f"qualification workflow_run_id does not match downloaded run metadata: {qualification}"
            )
        if payload.get("producer_revision") != head_sha:
            raise SystemExit(
                f"qualification producer revision does not match downloaded run metadata: {qualification}"
            )
        if producer_revision is None:
            producer_revision = head_sha
        elif producer_revision != head_sha:
            raise SystemExit(
                f"pre-release qualifications span producer revisions: {producer_revision} != {head_sha}"
            )
        previous_run = source_runs.get(run_id)
        normalized_run = {
            "workflow": workflow_name,
            "head_sha": head_sha,
            "head_branch": head_branch,
            "event": event,
        }
        if previous_run is not None and previous_run != normalized_run:
            raise SystemExit(f"conflicting metadata for qualification run {run_id}")
        source_runs[run_id] = normalized_run

        artifact_name = str(payload.get("artifact", ""))
        expected_sha = str(payload.get("artifact_sha256", "")).lower()
        if not artifact_name or SHA256_RE.fullmatch(expected_sha) is None:
            raise SystemExit(f"qualification has invalid artifact identity: {qualification}")
        if f"-v{release_version}-" not in artifact_name:
            raise SystemExit(
                f"artifact version does not match producer pre-release tag {args.tag}: {artifact_name}"
            )
        matches = find_by_name(run_dir, artifact_name)
        if len(matches) != 1:
            raise SystemExit(
                f"qualification artifact lookup is ambiguous for {artifact_name}: {matches}"
            )
        actual_sha = sha256_file(matches[0])
        if actual_sha != expected_sha:
            raise SystemExit(
                f"qualification SHA-256 mismatch for {artifact_name}: {actual_sha} != {expected_sha}"
            )
        validate_archive_members(matches[0])

        platform = str(payload.get("platform", ""))
        profile = str(payload.get("profile", ""))
        identity = (platform, profile)
        if identity not in EXPECTED_WORKFLOW_COVERAGE[workflow_name]:
            raise SystemExit(
                f"qualification {identity!r} does not belong to workflow {workflow_name}"
            )
        if identity in seen_qualifications:
            raise SystemExit(f"duplicate qualification identity: {identity!r}")
        seen_qualifications.add(identity)
        coverage[workflow_name].add(identity)
        validate_qualification_checks(platform, profile, payload.get("checks"), qualification)

        manifest_bytes, embedded_manifest = read_embedded_manifest(matches[0], profile)
        embedded_producer = embedded_manifest.get("producer")
        if not isinstance(embedded_producer, dict):
            raise SystemExit(f"embedded manifest producer is invalid: {artifact_name}")
        if (
            embedded_manifest.get("format_version") != 1
            or embedded_manifest.get("version") != release_version
            or embedded_manifest.get("platform") != platform
            or embedded_manifest.get("profile") != profile
            or embedded_manifest.get("python_abi") != "cp314"
            or embedded_producer.get("repository") != "precsim/python-numpy-fenics"
            or embedded_producer.get("revision") != head_sha
        ):
            raise SystemExit(
                f"embedded manifest identity does not match qualification/run metadata: {artifact_name}"
            )
        manifest_name = f"{artifact_name}.manifest.json"
        manifest_sha = write_unique(manifest_name, manifest_bytes, output, copied)
        manifest_sha_name = f"{manifest_name}.sha256"
        write_unique(
            manifest_sha_name,
            f"{manifest_sha}  {manifest_name}\n".encode("utf-8"),
            output,
            copied,
        )

        copy_unique(matches[0], output, copied)
        copy_unique(qualification, output, copied)

        checksum_sidecar = matches[0].with_name(matches[0].name + ".sha256")
        if not checksum_sidecar.is_file():
            raise SystemExit(f"artifact checksum sidecar is missing: {artifact_name}")
        validate_checksum_sidecar(checksum_sidecar, artifact_name, expected_sha)
        write_unique(
            checksum_sidecar.name,
            f"{expected_sha}  {artifact_name}\n".encode("utf-8"),
            output,
            copied,
        )

        source_provenance: dict[str, object] | None = None
        if identity == ("linux-x64", "fenics"):
            source_provenance = read_archive_json_member(
                matches[0],
                "fenics-runtime/provenance/source-provenance.json",
            )
        artifact_records[identity] = {
            "artifact": artifact_name,
            "artifact_sha256": expected_sha,
            "manifest": embedded_manifest,
            "manifest_sha256": manifest_sha,
            "source_provenance": source_provenance,
        }

        adjacent_summary = matches[0].with_name(matches[0].name + ".json")
        if adjacent_summary.is_file():
            summary = json.loads(adjacent_summary.read_text(encoding="utf-8"))
            if not isinstance(summary, dict):
                raise SystemExit(f"artifact summary is not an object: {adjacent_summary}")
            summary_sha = summary.get("sha256")
            if summary_sha is not None and str(summary_sha).lower() != expected_sha:
                raise SystemExit(f"artifact summary SHA-256 mismatch for {artifact_name}")
            summary_manifest_sha = summary.get("manifest_sha256")
            if (
                summary_manifest_sha is not None
                and str(summary_manifest_sha).lower() != manifest_sha
            ):
                raise SystemExit(
                    f"artifact summary manifest SHA-256 mismatch for {artifact_name}"
                )
            copy_unique(adjacent_summary, output, copied)

        qualification_summaries.append(
            {
                "platform": platform,
                "profile": profile,
                "artifact": artifact_name,
                "artifact_sha256": expected_sha,
                "manifest": manifest_name,
                "manifest_sha256": manifest_sha,
                "producer_revision": head_sha,
                "workflow_run_id": run_id,
                "workflow": workflow_name,
            }
        )

    for workflow_name in sorted(selected_workflows):
        expected = EXPECTED_WORKFLOW_COVERAGE[workflow_name]
        actual = coverage[workflow_name]
        if actual != expected:
            raise SystemExit(
                f"qualification coverage mismatch for {workflow_name}: "
                f"missing={sorted(expected - actual)!r} extra={sorted(actual - expected)!r}"
            )
    if producer_revision is None:
        raise SystemExit("producer revision was not established from qualification metadata")
    base_runtime_bindings = (
        validate_cross_profile_base_identities(artifact_records)
        if args.include_fenics
        else []
    )

    manifest = {
        "format_version": 1,
        "tag": args.tag,
        "version": release_version,
        "producer_repository": "precsim/python-numpy-fenics",
        "producer_revision": producer_revision,
        "profiles": selected_profiles,
        "source_runs": [
            {
                "workflow_run_id": run_id,
                **source_runs[run_id],
            }
            for run_id in sorted(source_runs, key=int)
        ],
        "base_runtime_bindings": base_runtime_bindings,
        "qualifications": qualification_summaries,
        "files": [
            {"name": name, "sha256": digest}
            for name, digest in sorted(copied.items())
        ],
    }
    manifest_path = output / "prerelease-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"producer_prerelease_files={len(copied) + 1}")
    print("producer_prerelease_validation=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
