"""Reference downstream staging of pinned, published NumPy + FEniCS layers.

This public integration mock is not a private host application's installer.
It deliberately inherits the FEniCS overlay's *rc1* NumPy base rather than
substituting the separately qualified rc2 NumPy profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile


class ConsumerStageError(ValueError):
    """A published input violates the pinned consumer contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ConsumerStageError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_file(path: Path, expected: str) -> None:
    require(path.is_file() and len(expected) == 64 and sha256(path) == expected,
            f"missing or changed pinned asset: {path.name}")


def validate_manifest(lock: dict, record: dict, platform: str) -> dict:
    require(record.get("format_version") == 1
            and record.get("tag") == lock["tag"]
            and record.get("producer_repository") == lock["repository"]
            and record.get("producer_revision") == lock["producer_revision"],
            "published release manifest identity mismatch")
    selected = lock["platforms"][platform]
    files = {item["name"]: item["sha256"] for item in record["files"]}
    require(len(files) == len(record["files"]), "duplicate release asset name")
    for profile in ("fenics", "numpy_base"):
        artifact = selected[profile]
        if profile == "fenics":
            require(files.get(artifact["name"]) == artifact["sha256"],
                    "published FEniCS asset differs from consumer lock")

    matching = [item for item in record["qualifications"]
                if item["platform"] == platform and item["profile"] == "fenics"]
    require(len(matching) == 1 and matching[0]["artifact"] == selected["fenics"]["name"]
            and matching[0]["manifest_sha256"] == selected["fenics"]["manifest_sha256"]
            and matching[0]["artifact_sha256"] == selected["fenics"]["sha256"],
            "FEniCS qualification does not match consumer lock")
    bindings = [binding for binding in record["base_runtime_bindings"]
                if binding["platform"] == platform]
    base = selected["numpy_base"]
    require(len(bindings) == 1
            and bindings[0]["numpy_base_release_tag"] == base["tag"]
            and bindings[0]["numpy_base_producer_revision"] == base["producer_revision"]
            and bindings[0]["numpy_artifact"] == base["name"]
            and bindings[0]["numpy_artifact_sha256"] == base["sha256"]
            and bindings[0]["numpy_manifest_sha256"] == base["manifest_sha256"]
            and bindings[0]["numpy_tree_sha256"] == base["numpy_tree_sha256"]
            and bindings[0]["numpy_entry_count"] == base["numpy_entry_count"],
            "FEniCS NumPy base binding does not match pinned prior release")
    return selected


def validate_qualification(lock: dict, base: dict, record: dict, platform: str) -> None:
    checks = record.get("checks")
    require(record.get("format_version") == 1
            and record.get("producer_repository") == lock["repository"]
            and record.get("producer_revision") == base["producer_revision"]
            and record.get("platform") == platform
            and record.get("profile") == "numpy"
            and record.get("artifact") == base["name"]
            and record.get("artifact_sha256") == base["sha256"]
            and isinstance(checks, dict) and bool(checks)
            and all(value in ("pass", "not-applicable") for value in checks.values()),
            "NumPy base qualification does not match pinned prior release")


def embedded_manifest(archive: Path, manifest_name: str, expected_sha: str) -> dict:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as opened:
            matches = [name for name in opened.namelist() if name == manifest_name]
            require(len(matches) == 1, f"missing or duplicate {manifest_name}")
            content = opened.read(manifest_name)
    else:
        with tarfile.open(archive, "r:gz") as opened:
            matches = [member for member in opened if member.name == manifest_name]
            require(len(matches) == 1 and matches[0].isfile(),
                    f"missing or duplicate {manifest_name}")
            source = opened.extractfile(matches[0])
            require(source is not None, "unreadable embedded manifest")
            with source:
                content = source.read()
    require(hashlib.sha256(content).hexdigest() == expected_sha,
            f"embedded {manifest_name} SHA-256 mismatch")
    return json.loads(content)


def _destination(root: Path, name: str) -> Path:
    require(bool(name) and "\\" not in name and "\x00" not in name and ":" not in name,
            "invalid archive member name")
    path = PurePosixPath(name)
    require(not path.is_absolute() and all(part not in ("", ".", "..") for part in name.split("/")),
            f"unsafe archive path: {name!r}")
    return root.joinpath(*path.parts)


def _safe_parent(root: Path, path: Path) -> None:
    require(os.path.commonpath([str(root.resolve()), str(path.parent.resolve())])
            == str(root.resolve()), f"archive path traverses a symlink: {path}")


def _symlink(root: Path, path: Path, target: str) -> None:
    require(bool(target) and not target.startswith("/") and "\\" not in target
            and ":" not in target,
            "unsafe absolute symlink target")
    require(os.path.commonpath([str(root.resolve()), str((path.parent / target).resolve())])
            == str(root.resolve()), "archive symlink escapes runtime")
    path.symlink_to(target)


def extract_layer(archive: Path, root: Path, occupied: dict[str, str]) -> None:
    """Extract safely and reject collisions across the base/overlay boundary."""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as opened:
            seen: set[str] = set()
            for item in opened.infolist():
                key = item.filename.rstrip("/")
                path = _destination(root, key)
                require(key not in seen, f"duplicate archive entry: {key}")
                seen.add(key)
                entry_kind = "dir" if item.is_dir() else "file"
                require(key not in occupied or (occupied[key] == "dir" and entry_kind == "dir"),
                        f"duplicate or overwritten runtime entry: {key}")
                occupied[key] = entry_kind
                _safe_parent(root, path)
                mode = item.external_attr >> 16
                kind = stat.S_IFMT(mode)
                require(kind in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK),
                        f"unsupported ZIP file type: {key}")
                if item.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                if kind == stat.S_IFLNK:
                    _symlink(root, path, opened.read(item).decode("utf-8"))
                else:
                    with opened.open(item) as source, path.open("xb") as target:
                        shutil.copyfileobj(source, target)
                    if os.name != "nt" and mode:
                        path.chmod(stat.S_IMODE(mode))
    else:
        with tarfile.open(archive, "r:gz") as opened:
            seen: set[str] = set()
            for member in opened:
                # GNU tar archives have a harmless leading ./ directory marker.
                key = member.name.removeprefix("./").rstrip("/")
                if key in ("", ".") and member.isdir():
                    continue
                path = _destination(root, key)
                require(key not in seen, f"duplicate archive entry: {key}")
                seen.add(key)
                entry_kind = "dir" if member.isdir() else "file"
                require(key not in occupied or (occupied[key] == "dir" and entry_kind == "dir"),
                        f"duplicate or overwritten runtime entry: {key}")
                occupied[key] = entry_kind
                _safe_parent(root, path)
                require(member.isdir() or member.isfile() or member.issym(),
                        f"unsupported tar member: {key}")
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                if member.issym():
                    _symlink(root, path, member.linkname)
                else:
                    source = opened.extractfile(member)
                    require(source is not None, "unreadable tar member")
                    with source, path.open("xb") as target:
                        shutil.copyfileobj(source, target)
                    if os.name != "nt":
                        path.chmod(member.mode & 0o777)


def download(tag: str, filename: str, cache: Path, repository: str) -> Path:
    path = cache / filename
    if not path.exists():
        subprocess.run(["gh", "release", "download", tag, "--repo", repository,
                        "--pattern", filename, "--dir", str(cache)], check=True)
    return path


def stage(lock_file: Path, platform: str, cache: Path, output: Path) -> dict:
    lock = json.loads(lock_file.read_text(encoding="utf-8"))
    require(lock.get("format_version") == 1 and platform in lock.get("platforms", {}),
            "unknown or unsupported consumer lock/platform")
    require(not output.exists(), "destination already exists; refusing to overwrite runtime")
    cache.mkdir(parents=True, exist_ok=True)
    manifest_file = download(lock["tag"], "prerelease-manifest.json", cache, lock["repository"])
    verify_file(manifest_file, lock["prerelease_manifest_sha256"])
    release = json.loads(manifest_file.read_text(encoding="utf-8"))
    pins = validate_manifest(lock, release, platform)
    base = pins["numpy_base"]
    overlay = pins["fenics"]
    base_file = download(base["tag"], base["name"], cache, lock["repository"])
    qualification_name = base.get("qualification_name")
    qualification_sha = base.get("qualification_sha256")
    require(isinstance(qualification_name, str) and bool(qualification_name)
            and isinstance(qualification_sha, str),
            "incomplete pinned NumPy base qualification identity")
    qualification_file = download(base["tag"], qualification_name, cache, lock["repository"])
    overlay_file = download(lock["tag"], overlay["name"], cache, lock["repository"])
    verify_file(base_file, base["sha256"])
    verify_file(qualification_file, qualification_sha)
    verify_file(overlay_file, overlay["sha256"])
    validate_qualification(lock, base,
                           json.loads(qualification_file.read_text(encoding="utf-8")), platform)

    base_manifest = embedded_manifest(base_file, "runtime-manifest.json", base["manifest_sha256"])
    overlay_manifest = embedded_manifest(overlay_file, "fenics-embed-manifest.json",
                                         overlay["manifest_sha256"])
    identity = overlay_manifest.get("numpy", {})
    require(base_manifest.get("profile") == "numpy"
            and base_manifest.get("platform") == platform
            and base_manifest.get("producer", {}).get("revision") == base["producer_revision"]
            and overlay_manifest.get("profile") == "fenics"
            and overlay_manifest.get("platform") == platform
            and overlay_manifest.get("producer", {}).get("revision") == lock["producer_revision"]
            and overlay_manifest.get("python_abi") == "cp314"
            and identity.get("base_producer_revision") == base["producer_revision"]
            and identity.get("base_manifest_sha256") == base["manifest_sha256"]
            and identity.get("base_tree_sha256") == base["numpy_tree_sha256"]
            and identity.get("base_entry_count") == base["numpy_entry_count"],
            "embedded runtimes do not match pinned cross-release identity")
    if platform == "win-x64":
        # Windows records the release tag and archive SHA directly. POSIX
        # records only the pinned base revision/manifest/tree; its immutable
        # release-tag and archive binding is enforced by validate_manifest.
        require(identity.get("base_release_tag") == base["tag"]
                and identity.get("base_archive_sha256") == base["sha256"],
                "embedded Windows FEniCS release base identity mismatch")
    else:
        require(identity.get("ownership") == "consumer-numpy-profile"
                and identity.get("producer_payload_excluded") is True,
                "POSIX FEniCS overlay claims unexpected NumPy ownership")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="staged-fenics-", dir=output.parent) as directory:
        temporary = Path(directory) / "runtime"
        temporary.mkdir()
        occupied: dict[str, str] = {}
        extract_layer(base_file, temporary, occupied)
        extract_layer(overlay_file, temporary, occupied)
        require((temporary / "fenics-embed-manifest.json").is_file()
                and (temporary / "runtime-manifest.json").is_file(),
                "staged runtime has missing manifests")
        temporary.rename(output)
    return {"platform": platform, "release_tag": lock["tag"],
            "release_revision": lock["producer_revision"], "base_tag": base["tag"],
            "base_revision": base["producer_revision"], "runtime": str(output)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=Path("locks/consumer/runtime-v0.1.0-rc2.json"))
    parser.add_argument("--platform", choices=("win-x64", "linux-x64", "macos-arm64", "macos-x64"), required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.lock, args.platform, args.cache, args.output), sort_keys=True))


if __name__ == "__main__":
    main()
