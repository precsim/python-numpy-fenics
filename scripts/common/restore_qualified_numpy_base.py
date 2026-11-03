#!/usr/bin/env python3
"""Restore the immutable, qualified public NumPy base used by FEniCS workflows.

The local lock is the trust anchor: no release tag, download, qualification,
or self-reported manifest may substitute for its pinned SHA-256 identities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile


REPO = "precsim/python-numpy-fenics"
VERSION = "0.1.0"
SHA = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
PLATFORMS = {"win-x64", "macos-arm64", "macos-x64", "linux-x64"}
NUMPY_PREFIX = {
    "win-x64": "Lib/site-packages/",
    "macos-arm64": "Python.framework/Versions/3.14/lib/python3.14/site-packages/",
    "macos-x64": "Python.framework/Versions/3.14/lib/python3.14/site-packages/",
    "linux-x64": "lib/python3.14/site-packages/",
}


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def name_for(platform: str) -> str:
    extension = ".tar.gz" if platform == "linux-x64" else ".zip"
    return f"python-runtime-v{VERSION}-{platform}-cp314-numpy{extension}"


def numpy_tree(manifest: dict, platform: str) -> tuple[str, int]:
    """Bind the NumPy-only payload independently of the enclosing archive."""
    prefix = NUMPY_PREFIX[platform]
    entries = {}
    for item in manifest.get("files", []):
        path = item.get("path", "")
        if not isinstance(path, str) or not path.startswith(prefix):
            continue
        relative = path[len(prefix):]
        if not (relative.startswith(("numpy/", "numpy.libs/")) or
                re.match(r"^numpy-[^/]+\.dist-info/", relative)):
            continue
        require(relative not in entries, f"duplicate NumPy base path: {relative}")
        kind = item.get("kind", "file")
        identity = item.get("target") if kind == "symlink" else item.get("sha256")
        require(kind in ("file", "symlink") and isinstance(identity, str) and bool(identity),
                f"invalid NumPy base file identity: {relative}")
        if kind == "file":
            require(SHA.fullmatch(identity) is not None, f"invalid NumPy file checksum: {relative}")
        entries[relative] = ("L" if kind == "symlink" else "F", identity)
    require(bool(entries), "release manifest contains no NumPy payload")
    result = hashlib.sha256()
    for relative, (kind, identity) in sorted(entries.items()):
        result.update(kind.encode("ascii") + b"\0" + relative.encode("utf-8") +
                      b"\0" + identity.encode("utf-8") + b"\n")
    return result.hexdigest(), len(entries)


def download_asset(tag: str, name: str, root: Path) -> Path:
    subprocess.run(
        ["gh", "release", "download", tag, "--repo", REPO, "--pattern", name, "--dir", str(root)],
        check=True,
    )
    result = root / name
    require(result.is_file(), f"missing release asset: {name}")
    return result


def safe_path(root: Path, raw: str) -> Path:
    require("\\" not in raw, f"archive contains backslash path: {raw!r}")
    rel = PurePosixPath(raw)
    require(bool(raw) and not rel.is_absolute() and ".." not in rel.parts, f"unsafe archive path: {raw!r}")
    return root.joinpath(*rel.parts)


def restore_zip(archive_path: Path, root: Path) -> None:
    seen: set[str] = set()
    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            path = safe_path(root, item.filename)
            key = item.filename.rstrip("/")
            require(key not in seen, f"duplicate ZIP entry: {key}")
            seen.add(key)
            mode = item.external_attr >> 16
            kind = stat.S_IFMT(mode)
            require(kind in {0, stat.S_IFDIR, stat.S_IFREG, stat.S_IFLNK},
                    f"unsupported ZIP entry type: {key}")
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if kind == stat.S_IFLNK:
                target = archive.read(item).decode("utf-8")
                require(bool(target) and not Path(target).is_absolute(), f"absolute ZIP symlink: {key}")
                # Do not permit links to escape the runtime, or writes through
                # an earlier archive-created symlink.
                require(os.path.commonpath([root.resolve(), (path.parent / target).resolve()]) == str(root.resolve()),
                        f"escaping ZIP symlink: {key}")
                path.symlink_to(target)
            else:
                require(os.path.commonpath([root.resolve(), path.parent.resolve()]) == str(root.resolve()),
                        f"ZIP member traverses symlink: {key}")
                with archive.open(item) as source, path.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                if mode:
                    path.chmod(stat.S_IMODE(mode))


def restore_tar(archive_path: Path, root: Path) -> None:
    seen: set[str] = set()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive:
            path = safe_path(root, member.name)
            key = member.name.rstrip("/")
            require(key not in seen, f"duplicate tar entry: {key}")
            seen.add(key)
            require(member.isfile() or member.isdir() or member.issym(),
                    f"unsupported tar entry type: {key}")
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            require(os.path.commonpath([root.resolve(), path.parent.resolve()]) == str(root.resolve()),
                    f"tar member traverses symlink: {key}")
            if member.issym():
                target = member.linkname
                require(bool(target) and not Path(target).is_absolute(), f"absolute tar symlink: {key}")
                require(os.path.commonpath([root.resolve(), (path.parent / target).resolve()]) == str(root.resolve()),
                        f"escaping tar symlink: {key}")
                path.symlink_to(target)
            else:
                source = archive.extractfile(member)
                require(source is not None, f"cannot read tar file: {key}")
                with source, path.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                path.chmod(member.mode & 0o777)


def validate_extracted_profile_marker(output: Path, platform: str) -> None:
    """Validate platform profile markers without inventing one for Linux.

    Windows and macOS runtime layouts carry a RUNTIME_PROFILE marker.  The
    published Linux runtime contract records the profile in the independently
    pinned runtime manifest instead and has never emitted that marker.
    """

    marker = output / "RUNTIME_PROFILE"
    if platform == "linux-x64" and not marker.exists():
        return
    require(marker.is_file() and marker.read_text(encoding="utf-8").strip() == "numpy",
            "extracted base runtime is not the NumPy profile")


def verified_restore(
    tag: str,
    platform: str,
    output: Path,
    archive_output: Path,
    qualification_output: Path,
    lock_path: Path,
) -> dict:
    require(platform in PLATFORMS, f"unsupported platform: {platform}")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    require(lock.get("format_version") == 1 and lock.get("repository") == REPO,
            "invalid FEniCS NumPy base lock")
    require(tag == lock.get("tag"), "release tag differs from immutable NumPy base lock")
    revision = lock.get("producer_revision")
    require(isinstance(revision, str) and GIT_SHA.fullmatch(revision), "invalid base producer revision")
    pins = lock["platforms"][platform]
    require(all(isinstance(pins.get(key), str) and SHA.fullmatch(pins[key]) for key in
                ("archive_sha256", "manifest_sha256", "qualification_sha256", "numpy_tree_sha256")) and
            isinstance(pins.get("numpy_entry_count"), int) and pins["numpy_entry_count"] > 0,
            "invalid locked base SHA-256")
    artifact = name_for(platform)
    qualification_name = f"qualification-{platform}-numpy.json"
    with tempfile.TemporaryDirectory(prefix="fenics-numpy-release-") as tmp:
        temporary = Path(tmp)
        archive = download_asset(tag, artifact, temporary)
        manifest_file = download_asset(tag, artifact + ".manifest.json", temporary)
        qualification_file = download_asset(tag, qualification_name, temporary)
        for file, expected in (
            (archive, pins["archive_sha256"]),
            (manifest_file, pins["manifest_sha256"]),
            (qualification_file, pins["qualification_sha256"]),
        ):
            require(digest(file) == expected, f"pinned SHA-256 mismatch: {file.name}")
        manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        qualification = json.loads(qualification_file.read_text(encoding="utf-8-sig"))
        require(manifest.get("format_version") == 1 and
                manifest.get("platform") == platform and
                manifest.get("profile") == "numpy" and
                manifest.get("python_abi") == "cp314" and
                manifest.get("version") == VERSION and
                manifest.get("producer") == {"repository": REPO, "revision": revision},
                "released NumPy base manifest identity mismatch")
        require(qualification.get("format_version") == 1 and
                qualification.get("platform") == platform and
                qualification.get("profile") == "numpy" and
                qualification.get("artifact") == artifact and
                qualification.get("artifact_sha256") == pins["archive_sha256"] and
                qualification.get("producer_repository") == REPO and
                qualification.get("producer_revision") == revision and
                all(v in ("pass", "not-applicable") for v in qualification.get("checks", {}).values()) and
                len(qualification.get("checks", {})) >= 5,
                "released NumPy base qualification evidence mismatch")
        require(manifest.get("tcl_tk", {}).get("version") == "8.6.18" and
                manifest.get("tcl_tk", {}).get("ownership") == "producer",
                "released NumPy base lacks pinned Tcl/Tk provenance")
        actual_tree, actual_count = numpy_tree(manifest, platform)
        require(actual_tree == pins["numpy_tree_sha256"] and actual_count == pins["numpy_entry_count"],
                "released NumPy base payload does not match locked tree identity")
        require(not output.exists(), f"output runtime already exists: {output}")
        output.mkdir(parents=True)
        try:
            if platform == "linux-x64":
                restore_tar(archive, output)
            else:
                restore_zip(archive, output)
            embedded = output / "runtime-manifest.json"
            require(embedded.is_file() and embedded.read_bytes() == manifest_file.read_bytes(),
                    "extracted base runtime manifest does not match locked release manifest")
            validate_extracted_profile_marker(output, platform)
        except BaseException:
            shutil.rmtree(output)
            raise
        archive_output.parent.mkdir(parents=True, exist_ok=True)
        qualification_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, archive_output)
        shutil.copyfile(qualification_file, qualification_output)
    return {"platform": platform, "tag": tag, "producer_revision": revision,
            "artifact_sha256": pins["archive_sha256"],
            "manifest_sha256": pins["manifest_sha256"],
            "runtime": str(output), "archive": str(archive_output)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--archive-output", type=Path)
    parser.add_argument("--qualification-output", required=True, type=Path)
    parser.add_argument("--lock", type=Path, default=Path("locks/core/fenics-numpy-base-release.json"))
    args = parser.parse_args()
    archive_output = args.archive_output or Path("dist") / name_for(args.platform)
    result = verified_restore(args.tag, args.platform, args.output, archive_output,
                              args.qualification_output, args.lock)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
