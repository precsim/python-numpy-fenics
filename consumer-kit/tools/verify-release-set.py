#!/usr/bin/env python3
"""Offline verification for a downloaded reference-bridge qualification set."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any
import zipfile


TARGETS = {"win-x64", "linux-x64", "macos-arm64", "macos-x64"}
PROFILES = {"core", "numpy", "fenics"}
ARCHIVE_RE = re.compile(
    r"^reference-bridge-(win-x64|linux-x64|macos-arm64|macos-x64)-cp314-"
    r"(core|numpy|fenics)-(.+)\.(zip|tar\.gz)$"
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_member(raw: str) -> PurePosixPath:
    if "\\" in raw or "\x00" in raw or ":" in raw:
        raise SystemExit(f"unsafe release archive member: {raw!r}")
    value = raw.rstrip("/")
    if not value or raw not in {value, value + "/"}:
        raise SystemExit(f"unsafe release archive member: {raw!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SystemExit(f"unsafe release archive member: {raw!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise SystemExit(f"unsafe release archive member: {raw!r}")
    return path


def safe_symlink_target(member: PurePosixPath, raw: str, label: str) -> None:
    if not raw or "\\" in raw or "\x00" in raw or ":" in raw:
        raise SystemExit(f"unsafe {label} symlink: {member.as_posix()} -> {raw}")
    target = PurePosixPath(raw)
    if target.is_absolute():
        raise SystemExit(f"unsafe {label} symlink: {member.as_posix()} -> {raw}")

    # Resolve the target lexically relative to the symlink. Parent components
    # are valid only while they remain inside this package's top-level root.
    resolved = list(member.parent.parts)
    for part in raw.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise SystemExit(f"unsafe {label} symlink: {member.as_posix()} -> {raw}")
            resolved.pop()
        else:
            resolved.append(part)
    if not resolved or not member.parts or resolved[0] != member.parts[0]:
        raise SystemExit(f"unsafe {label} symlink: {member.as_posix()} -> {raw}")


def validate_archive_namespace(
    entries: list[tuple[PurePosixPath, str, Any]], label: str
) -> None:
    """Reject path aliases and any archive member reached through a link/file."""

    seen: dict[str, str] = {}
    non_directories: set[str] = set()
    for rel, kind, _raw in entries:
        key = rel.as_posix()
        collision = key.casefold()
        previous = seen.get(collision)
        if previous is not None:
            raise SystemExit(f"duplicate {label} member: {key} conflicts with {previous}")
        seen[collision] = key
        if kind != "dir":
            non_directories.add(key)

    for rel, _kind, _raw in entries:
        parts = rel.parts
        for index in range(1, len(parts)):
            ancestor = PurePosixPath(*parts[:index]).as_posix()
            if ancestor in non_directories:
                raise SystemExit(
                    f"unsafe {label} member traverses non-directory archive entry: "
                    f"{rel.as_posix()} via {ancestor}"
                )


def output_path(destination: Path, rel: PurePosixPath, label: str) -> Path:
    root = destination.resolve()
    output = destination.joinpath(*rel.parts)
    parent = output.parent.resolve(strict=False)
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise SystemExit(f"unsafe {label} extraction path: {rel.as_posix()}") from exc
    return output


def extract_zip(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        entries: list[tuple[PurePosixPath, str, zipfile.ZipInfo]] = []
        for info in archive.infolist():
            rel = safe_member(info.filename)
            key = rel.as_posix()
            mode = info.external_attr >> 16
            if info.is_dir():
                kind = "dir"
            elif stat.S_IFMT(mode) == stat.S_IFLNK:
                kind = "symlink"
                try:
                    target = archive.read(info).decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise SystemExit(f"invalid ZIP symlink target encoding: {key}") from exc
                safe_symlink_target(rel, target, "ZIP")
            else:
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFREG}:
                    raise SystemExit(f"unsupported ZIP member: {key}")
                kind = "file"
            entries.append((rel, kind, info))

        validate_archive_namespace(entries, "ZIP")
        for rel, kind, info in entries:
            output = output_path(destination, rel, "ZIP")
            mode = info.external_attr >> 16
            if kind == "dir":
                output.mkdir(parents=True, exist_ok=True)
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            if kind == "symlink":
                target = archive.read(info).decode("utf-8")
                output.symlink_to(target)
            else:
                output.write_bytes(archive.read(info))
                if mode:
                    output.chmod(stat.S_IMODE(mode))


def extract_tar(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r:gz") as archive:
        entries: list[tuple[PurePosixPath, str, tarfile.TarInfo]] = []
        for member in archive.getmembers():
            rel = safe_member(member.name)
            key = rel.as_posix()
            if member.isdir():
                kind = "dir"
            elif member.isfile():
                kind = "file"
            elif member.issym():
                kind = "symlink"
                safe_symlink_target(rel, member.linkname, "tar")
            else:
                raise SystemExit(f"unsupported tar member: {key}")
            entries.append((rel, kind, member))

        validate_archive_namespace(entries, "tar")
        for rel, kind, member in entries:
            output = output_path(destination, rel, "tar")
            if kind == "dir":
                output.mkdir(parents=True, exist_ok=True)
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            if kind == "symlink":
                output.symlink_to(member.linkname)
            else:
                source = archive.extractfile(member)
                if source is None:
                    raise SystemExit(f"cannot read tar member: {rel.as_posix()}")
                with source, output.open("wb") as sink:
                    while block := source.read(1024 * 1024):
                        sink.write(block)
                output.chmod(member.mode & 0o777)


def parse_sidecar(archive: Path) -> str:
    sidecar = Path(str(archive) + ".sha256")
    if not sidecar.is_file():
        raise SystemExit(f"missing package SHA-256 sidecar: {sidecar.name}")
    line = sidecar.read_text(encoding="utf-8").strip()
    match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
    if match is None or match.group(2) != archive.name:
        raise SystemExit(f"invalid SHA-256 sidecar: {sidecar.name}")
    actual = digest(archive)
    if actual != match.group(1):
        raise SystemExit(f"package SHA-256 mismatch: {archive.name}")
    return actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    archives: dict[tuple[str, str], Path] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.name.endswith(".sha256"):
            continue
        match = ARCHIVE_RE.fullmatch(path.name)
        if match is None:
            continue
        target, profile = match.group(1), match.group(2)
        key = (target, profile)
        if key in archives:
            raise SystemExit(f"duplicate reference package: {target}/{profile}")
        archives[key] = path
    required = {(target, profile) for target in TARGETS for profile in ("core", "numpy")}
    missing = sorted(required - set(archives))
    if missing:
        raise SystemExit(f"release set is missing required packages: {missing}")
    fenics = {target for target in TARGETS if (target, "fenics") in archives}
    if fenics and fenics != TARGETS:
        raise SystemExit(f"FEniCS packages must be all-or-none; present={sorted(fenics)}")

    for (target, profile), archive in sorted(archives.items()):
        parse_sidecar(archive)
        with tempfile.TemporaryDirectory(prefix="reference-release-verify-") as raw:
            destination = Path(raw)
            if archive.name.endswith(".zip"):
                extract_zip(archive, destination)
            else:
                extract_tar(archive, destination)
            roots = [path for path in destination.iterdir() if path.is_dir()]
            if len(roots) != 1:
                raise SystemExit(f"package has no unique root directory: {archive.name}")
            package_root = roots[0]
            verifier = package_root / "verify-package.py"
            result = subprocess.run(
                [sys.executable, str(verifier), "--root", str(package_root)],
                text=True,
                capture_output=True,
            )
            if result.returncode:
                raise SystemExit(
                    f"offline verification failed for {archive.name}:\n{result.stdout}{result.stderr}"
                )
            manifest = json.loads((package_root / "reference-manifest.json").read_text(encoding="utf-8"))
            if manifest.get("platform") != target or manifest.get("profile") != profile:
                raise SystemExit(f"package filename/manifest identity mismatch: {archive.name}")
            if manifest.get("mock", {}).get("source_revision") != args.source_revision:
                raise SystemExit(f"package source revision differs from qualified revision: {archive.name}")
    print(f"reference_release_packages={len(archives)}")
    print("reference_release_set=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
