#!/usr/bin/env python3
"""Audit macOS FEniCS overlay ZIP metadata without re-extracting it."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import py_compile
import re
import stat
import tempfile
import zipfile


BOOTSTRAP = "Python.framework/Versions/3.14/lib/python3.14/site-packages/fenics_embed_runtime.py"
TOOL_NAMES = {
    "arm64": (
        "fenics-runtime/conda/bin/arm64-apple-darwin20.0.0-clang",
        "fenics-runtime/conda/bin/arm64-apple-darwin20.0.0-ld",
    ),
    "x86_64": (
        "fenics-runtime/conda/bin/x86_64-apple-darwin13.4.0-clang",
        "fenics-runtime/conda/bin/x86_64-apple-darwin13.4.0-ld",
    ),
}
FORBIDDEN_NUMPY = re.compile(
    r"^fenics-runtime/conda/lib/python3\.14/site-packages/"
    r"(?:numpy/|numpy\.libs/|numpy-[^/]+\.dist-info/)"
)


def _single_member(members: dict[str, list[zipfile.ZipInfo]], name: str) -> zipfile.ZipInfo:
    matches = members.get(name, [])
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one ZIP member {name!r}, found {len(matches)}")
    return matches[0]


def _validate_member_name(name: str) -> None:
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "//" in name
        or posixpath.normpath(name) != name
    ):
        raise RuntimeError(f"ZIP member name is not canonical: {name!r}")


def _resolve_archive_symlink(name: str, target: str) -> str:
    if target.startswith("/"):
        raise RuntimeError(f"packaged tool symlink is absolute: {name} -> {target}")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
    if resolved == ".." or resolved.startswith("../"):
        raise RuntimeError(f"packaged tool symlink escapes archive root: {name} -> {target}")
    return PurePosixPath(resolved).as_posix()


def _require_executable_member(
    archive: zipfile.ZipFile,
    members: dict[str, list[zipfile.ZipInfo]],
    name: str,
    *,
    seen: set[str] | None = None,
) -> None:
    if seen is None:
        seen = set()
    if name in seen:
        raise RuntimeError(f"packaged tool symlink cycle: {name}")
    seen.add(name)

    info = _single_member(members, name)
    if info.create_system != 3:
        raise RuntimeError(f"packaged tool lacks Unix ZIP metadata: {name}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        target = archive.read(info).decode("utf-8")
        _require_executable_member(
            archive,
            members,
            _resolve_archive_symlink(name, target),
            seen=seen,
        )
        return
    if not stat.S_ISREG(mode) or not mode & 0o111:
        raise RuntimeError(f"packaged tool is not executable: {name} mode={mode:#o}")


def audit_overlay_zip(archive_path: Path, summary_path: Path, arch: str) -> str:
    if arch not in TOOL_NAMES:
        raise ValueError(f"unsupported architecture: {arch}")

    with zipfile.ZipFile(archive_path) as archive:
        members: dict[str, list[zipfile.ZipInfo]] = {}
        for info in archive.infolist():
            _validate_member_name(info.filename)
            members.setdefault(info.filename, []).append(info)
            if len(members[info.filename]) != 1:
                raise RuntimeError(f"duplicate ZIP member name: {info.filename}")
            if FORBIDDEN_NUMPY.match(info.filename):
                raise RuntimeError(f"producer-owned NumPy path found in overlay: {info.filename}")

        bootstrap_info = _single_member(members, BOOTSTRAP)
        if bootstrap_info.create_system != 3:
            raise RuntimeError(f"packaged bootstrap lacks Unix ZIP metadata: {BOOTSTRAP}")
        bootstrap_mode = bootstrap_info.external_attr >> 16
        if not stat.S_ISREG(bootstrap_mode):
            raise RuntimeError(f"packaged bootstrap is not a regular file: {BOOTSTRAP}")
        with tempfile.TemporaryDirectory() as temporary:
            bootstrap_path = Path(temporary) / "fenics_embed_runtime.py"
            bootstrap_path.write_bytes(archive.read(bootstrap_info))
            py_compile.compile(str(bootstrap_path), doraise=True)

        for tool_name in TOOL_NAMES[arch]:
            _require_executable_member(archive, members, tool_name)

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if summary.get("artifact") != archive_path.name or summary.get("sha256") != actual:
        raise RuntimeError("outer summary does not bind the packaged ZIP")
    return actual


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=sorted(TOOL_NAMES))
    args = parser.parse_args()

    actual = audit_overlay_zip(args.zip, args.summary, args.arch)
    print(f"overlay_sha256={actual}")
    print("packaged_zip_structure=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
