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
FORBIDDEN_SUFFIXES = (".mex", ".mexa64", ".mexmaci64", ".mexw64")
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
    args = parser.parse_args()

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
        if "src" in {part.lower() for part in path.parts}:
            raise SystemExit(f"source-tree payload is forbidden in producer pre-release input: {path}")

    qualifications = sorted(source_root.rglob("qualification-*.json"))
    if not qualifications:
        raise SystemExit("no producer qualification records were downloaded")

    copied: dict[str, str] = {}
    qualification_summaries = []
    coverage: dict[str, set[tuple[str, str]]] = {
        workflow: set() for workflow in EXPECTED_WORKFLOW_COVERAGE
    }
    producer_revision: str | None = None
    source_runs: dict[str, dict[str, object]] = {}
    seen_qualifications: set[tuple[str, str]] = set()
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
        if workflow_name not in EXPECTED_WORKFLOW_COVERAGE:
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

        failed = sorted(
            name
            for name, status in (payload.get("checks") or {}).items()
            if status == "fail"
        )
        if failed:
            raise SystemExit(f"qualification contains failed checks {failed}: {qualification}")

        artifact_name = str(payload.get("artifact", ""))
        expected_sha = str(payload.get("artifact_sha256", "")).lower()
        if not artifact_name or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise SystemExit(f"qualification has invalid artifact identity: {qualification}")
        if f"-v{release_version}-" not in artifact_name:
            raise SystemExit(
                f"artifact version does not match producer pre-release tag {args.tag}: {artifact_name}"
            )
        matches = find_by_name(source_root, artifact_name)
        if len(matches) != 1:
            raise SystemExit(
                f"qualification artifact lookup is ambiguous for {artifact_name}: {matches}"
            )
        actual_sha = sha256_file(matches[0])
        if actual_sha != expected_sha:
            raise SystemExit(
                f"qualification SHA-256 mismatch for {artifact_name}: {actual_sha} != {expected_sha}"
            )

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

        manifest_bytes, embedded_manifest = read_embedded_manifest(matches[0], profile)
        embedded_producer = embedded_manifest.get("producer")
        if not isinstance(embedded_producer, dict):
            raise SystemExit(f"embedded manifest producer is invalid: {artifact_name}")
        if (
            embedded_manifest.get("format_version") != 1
            or embedded_manifest.get("version") != release_version
            or embedded_manifest.get("platform") != platform
            or embedded_manifest.get("profile") != profile
            or embedded_manifest.get("python_abi") != "cp312"
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

    for workflow_name, expected in EXPECTED_WORKFLOW_COVERAGE.items():
        actual = coverage[workflow_name]
        if actual != expected:
            raise SystemExit(
                f"qualification coverage mismatch for {workflow_name}: "
                f"missing={sorted(expected - actual)!r} extra={sorted(actual - expected)!r}"
            )
    if producer_revision is None:
        raise SystemExit("producer revision was not established from qualification metadata")

    manifest = {
        "format_version": 1,
        "tag": args.tag,
        "version": release_version,
        "producer_repository": "precsim/python-numpy-fenics",
        "producer_revision": producer_revision,
        "source_runs": [
            {
                "workflow_run_id": run_id,
                **source_runs[run_id],
            }
            for run_id in sorted(source_runs, key=int)
        ],
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
