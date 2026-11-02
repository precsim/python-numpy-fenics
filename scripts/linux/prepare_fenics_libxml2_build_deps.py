#!/usr/bin/env python3
"""Prepare pinned zlib/liblzma build dependencies for the Linux FEniCS rebuild."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import urlparse


DEVELOPMENT_PACKAGES = ("zlib", "liblzma-devel")
RUNTIME_PACKAGES = ("libzlib", "liblzma")
IDENTITY_FIELDS = (
    "name",
    "version",
    "build",
    "build_number",
    "subdir",
    "channel",
    "url",
    "sha256",
    "md5",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MD5_RE = re.compile(r"^[0-9a-f]{32}$")


class BuildDepsError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildDepsError(f"cannot read JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BuildDepsError(f"JSON input must contain an object: {path}")
    return value


def require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise BuildDepsError(f"{label} must be a lowercase SHA-256")
    return value


def require_md5(value: object, label: str) -> str:
    if not isinstance(value, str) or MD5_RE.fullmatch(value) is None:
        raise BuildDepsError(f"{label} must be a lowercase MD5")
    return value


def safe_relative(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise BuildDepsError(f"{label} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise BuildDepsError(f"{label} is not a safe relative path: {value!r}")
    return path


def normalize_identity(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in IDENTITY_FIELDS}


def records_by_name(records: object, expected_names: tuple[str, ...], label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(records, list):
        raise BuildDepsError(f"{label} must be a list")
    result: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get("name"), str):
            raise BuildDepsError(f"{label} record {index} is invalid")
        name = record["name"]
        if name in result:
            raise BuildDepsError(f"duplicate {label} record: {name}")
        result[name] = record
        order.append(name)
    if tuple(order) != expected_names:
        raise BuildDepsError(f"{label} order must be exactly {expected_names!r}")
    return result


def validate_package_identity(record: dict[str, Any], label: str) -> None:
    missing = [field for field in IDENTITY_FIELDS if record.get(field) in (None, "")]
    if missing:
        raise BuildDepsError(f"{label} is missing identity fields: {missing!r}")
    require_sha256(record.get("sha256"), f"{label} sha256")
    require_md5(record.get("md5"), f"{label} md5")
    if record.get("channel") != "conda-forge" or record.get("subdir") != "linux-64":
        raise BuildDepsError(f"{label} must be a conda-forge linux-64 package")
    url = str(record.get("url"))
    prefix = "https://conda.anaconda.org/conda-forge/linux-64/"
    if not url.startswith(prefix) or Path(urlparse(url).path).name == "":
        raise BuildDepsError(f"{label} has an unexpected package URL: {url!r}")


def validate_file_records(record: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    files = record.get("files")
    if not isinstance(files, dict) or not files:
        raise BuildDepsError(f"{label} has no pinned files")
    validated: dict[str, dict[str, Any]] = {}
    for relative, file_record in files.items():
        safe_relative(relative, f"{label} file")
        if not isinstance(file_record, dict):
            raise BuildDepsError(f"{label} file record is invalid: {relative}")
        sha = require_sha256(file_record.get("sha256"), f"{label} file {relative} sha256")
        size = file_record.get("size")
        if not isinstance(size, int) or size <= 0:
            raise BuildDepsError(f"{label} file {relative} has invalid size")
        validated[relative] = {"sha256": sha, "size": size}
    return validated


def validate_lock(lock_path: Path, fenics_package_lock_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    lock = load_json(lock_path)
    if lock.get("schema_version") != 1:
        raise BuildDepsError("unsupported libxml2 build-dependency lock schema")
    if lock.get("target") != {"architecture": "x86_64", "glibc_floor": "2.17"}:
        raise BuildDepsError("libxml2 build-dependency target differs from the Linux ABI contract")
    expected_policy = {
        "dependency_solver_used": False,
        "development_replay_input_format": "@EXPLICIT-url-plus-sha256",
        "index_access": False,
        "network_access_after_replay": False,
        "runtime_packages_from_verified_fenics_prefix": True,
        "output_files_regular": True,
    }
    if lock.get("policy") != expected_policy:
        raise BuildDepsError("libxml2 build-dependency policy differs from the fail-closed contract")

    development = records_by_name(lock.get("development_packages"), DEVELOPMENT_PACKAGES, "development_packages")
    runtime = records_by_name(lock.get("runtime_packages"), RUNTIME_PACKAGES, "runtime_packages")
    for name, record in [*development.items(), *runtime.items()]:
        validate_package_identity(record, f"package {name}")
        validate_file_records(record, f"package {name}")

    if development["zlib"]["version"] != runtime["libzlib"]["version"]:
        raise BuildDepsError("zlib development/runtime versions differ")
    if development["liblzma-devel"]["version"] != runtime["liblzma"]["version"]:
        raise BuildDepsError("liblzma development/runtime versions differ")
    if development["liblzma-devel"]["build"] != runtime["liblzma"]["build"]:
        raise BuildDepsError("liblzma development/runtime builds differ")

    fenics_lock = load_json(fenics_package_lock_path)
    if fenics_lock.get("format_version") != 1 or fenics_lock.get("architecture") != "x86_64":
        raise BuildDepsError("FEniCS package lock has an unsupported identity")
    package_records = fenics_lock.get("packages")
    if not isinstance(package_records, list) or fenics_lock.get("record_count") != len(package_records):
        raise BuildDepsError("FEniCS package lock has an invalid package record set")
    fenics_by_name = {
        record.get("name"): record for record in package_records
        if isinstance(record, dict) and isinstance(record.get("name"), str)
    }
    for name in RUNTIME_PACKAGES:
        source = fenics_by_name.get(name)
        if not isinstance(source, dict) or normalize_identity(source) != normalize_identity(runtime[name]):
            raise BuildDepsError(f"runtime package {name} differs from the immutable FEniCS package lock")

    output = lock.get("output")
    if not isinstance(output, dict):
        raise BuildDepsError("libxml2 build-dependency lock has no output contract")
    headers = output.get("headers")
    if not isinstance(headers, dict):
        raise BuildDepsError("libxml2 build-dependency output has no headers contract")
    expected_headers = set(development["zlib"]["files"]) | set(development["liblzma-devel"]["files"])
    if set(headers) != expected_headers:
        raise BuildDepsError("libxml2 output header set differs from development package file pins")
    for relative, expected_sha in headers.items():
        require_sha256(expected_sha, f"output header {relative} sha256")
        owner = development["zlib"] if relative in development["zlib"]["files"] else development["liblzma-devel"]
        if owner["files"][relative]["sha256"] != expected_sha:
            raise BuildDepsError(f"output header hash differs from source package pin: {relative}")

    libraries = output.get("libraries")
    if not isinstance(libraries, dict) or tuple(libraries) != ("zlib", "lzma"):
        raise BuildDepsError("libxml2 output libraries must be exactly zlib then lzma")
    expected_library_packages = {"zlib": "libzlib", "lzma": "liblzma"}
    for name, package_name in expected_library_packages.items():
        record = libraries[name]
        if not isinstance(record, dict) or record.get("source_package") != package_name:
            raise BuildDepsError(f"libxml2 output library source package differs for {name}")
        source_path = safe_relative(record.get("source_path"), f"output library {name} source_path").as_posix()
        relative_path = safe_relative(record.get("relative_path"), f"output library {name} relative_path").as_posix()
        if source_path != relative_path:
            raise BuildDepsError(f"libxml2 output library path must retain the pinned runtime package path for {name}")
        source_record = runtime[package_name]["files"].get(source_path)
        if not isinstance(source_record, dict) or source_record.get("sha256") != record.get("sha256"):
            raise BuildDepsError(f"libxml2 output library hash differs from runtime package pin: {name}")
    return lock, development, runtime


def render_explicit(development: dict[str, dict[str, Any]]) -> str:
    lines = [
        "# Exact Linux FEniCS libxml2 development-header packages.",
        "# Dependency solving and channel/index resolution are forbidden.",
        "@EXPLICIT",
    ]
    for name in DEVELOPMENT_PACKAGES:
        record = development[name]
        lines.append(f"{record['url']}#sha256:{record['sha256']}")
    return "\n".join(lines) + "\n"


def package_filename(record: dict[str, Any]) -> str:
    filename = Path(urlparse(str(record["url"])).path).name
    if not filename:
        raise BuildDepsError(f"package URL has no filename: {record['url']!r}")
    return filename


def verify_cached_archive(package_cache: Path, record: dict[str, Any]) -> dict[str, Any]:
    filename = package_filename(record)
    candidates = sorted(
        (path for path in package_cache.rglob(filename) if path.is_file()),
        key=lambda path: str(path),
    )
    if not candidates:
        raise BuildDepsError(f"package cache has no archive for {record['name']}: {filename}")
    expected_sha = record["sha256"]
    cache_paths = []
    for path in candidates:
        actual = sha256_file(path)
        if actual != expected_sha:
            raise BuildDepsError(
                f"cached package SHA-256 mismatch for {record['name']}: {actual} != {expected_sha}"
            )
        cache_paths.append(path.relative_to(package_cache).as_posix())
    return {
        "name": record["name"],
        "filename": filename,
        "sha256": expected_sha,
        "cache_paths": cache_paths,
    }


def conda_meta_records(prefix: Path) -> dict[str, dict[str, Any]]:
    meta = prefix / "conda-meta"
    if not meta.is_dir():
        raise BuildDepsError(f"conda prefix has no conda-meta directory: {prefix}")
    result: dict[str, dict[str, Any]] = {}
    for path in sorted(meta.glob("*.json")):
        record = load_json(path)
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise BuildDepsError(f"invalid conda metadata record: {path}")
        if name in result:
            raise BuildDepsError(f"duplicate installed conda package: {name}")
        result[name] = record
    return result


def verify_installed_identity(installed: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    for field in IDENTITY_FIELDS:
        got = installed.get(field)
        want = expected.get(field)
        if field in {"sha256", "md5"}:
            got = str(got or "").lower()
            want = str(want or "").lower()
        if got != want:
            raise BuildDepsError(f"{label} identity mismatch for {field}: {got!r} != {want!r}")


def verify_owned_file(prefix: Path, metadata: dict[str, Any], relative: str, expected: dict[str, Any], label: str) -> Path:
    safe = safe_relative(relative, label)
    files = metadata.get("files")
    if not isinstance(files, list) or relative not in files:
        raise BuildDepsError(f"{label} is not owned by the expected package: {relative}")
    path = prefix / safe
    if not path.is_file() or path.is_symlink():
        raise BuildDepsError(f"{label} is missing or is not a regular file: {path}")
    actual_sha = sha256_file(path)
    actual_size = path.stat().st_size
    if actual_sha != expected["sha256"] or actual_size != expected["size"]:
        raise BuildDepsError(
            f"{label} differs from pinned package contents: sha={actual_sha} size={actual_size}"
        )
    return path


def validate_prefix_verification(path: Path, fenics_package_lock_path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    evidence = load_json(path)
    if (
        evidence.get("format_version") != 1
        or evidence.get("architecture") != "x86_64"
        or evidence.get("lock_sha256") != sha256_file(fenics_package_lock_path)
        or evidence.get("solver_used") is not False
        or evidence.get("package_hash_verification") != "cached-package-sha256-plus-installed-record-match"
        or evidence.get("replay_input_format") != "@EXPLICIT-url-plus-sha256"
    ):
        raise BuildDepsError("FEniCS prefix verification evidence does not satisfy the immutable replay contract")
    packages = records_by_name(evidence.get("packages"), tuple(sorted(
        str(record.get("name")) for record in evidence.get("packages", []) if isinstance(record, dict)
    )), "verified FEniCS packages")
    archives_raw = evidence.get("verified_package_archives")
    if not isinstance(archives_raw, list):
        raise BuildDepsError("FEniCS prefix verification has no verified package archives")
    archives = {
        record.get("name"): record for record in archives_raw
        if isinstance(record, dict) and isinstance(record.get("name"), str)
    }
    return evidence, packages, archives


def copy_regular(source: Path, destination: Path, expected_sha: str, expected_size: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, destination.open("wb") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
    if destination.is_symlink() or not destination.is_file():
        raise BuildDepsError(f"prepared output is not a regular file: {destination}")
    actual_sha = sha256_file(destination)
    if actual_sha != expected_sha or destination.stat().st_size != expected_size:
        raise BuildDepsError(f"prepared output differs from pinned input: {destination}")


def tree_identity(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    files = []
    for path in sorted((candidate for candidate in root.rglob("*") if candidate.is_file()), key=lambda candidate: candidate.relative_to(root).as_posix()):
        if path.is_symlink():
            raise BuildDepsError(f"prepared libxml2 dependency tree contains a symlink: {path}")
        relative = path.relative_to(root).as_posix()
        item_sha = sha256_file(path)
        size = path.stat().st_size
        digest.update(relative.encode("utf-8") + b"\0" + item_sha.encode("ascii") + b"\0" + str(size).encode("ascii") + b"\n")
        files.append({"path": relative, "sha256": item_sha, "size": size})
    if not files:
        raise BuildDepsError("prepared libxml2 dependency tree is empty")
    return {"file_count": len(files), "tree_sha256": digest.hexdigest(), "files": files}


def prepare(
    *,
    lock_path: Path,
    fenics_package_lock_path: Path,
    fenics_prefix_evidence_path: Path,
    fenics_prefix: Path,
    development_prefix: Path,
    package_cache: Path,
    output_dir: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    lock, development, runtime = validate_lock(lock_path, fenics_package_lock_path)
    if not package_cache.is_dir():
        raise BuildDepsError(f"package cache is missing: {package_cache}")
    if not fenics_prefix.is_dir():
        raise BuildDepsError(f"verified FEniCS prefix is missing: {fenics_prefix}")
    if not development_prefix.is_dir():
        raise BuildDepsError(f"development package prefix is missing: {development_prefix}")

    verification, verified_packages, verified_archives = validate_prefix_verification(
        fenics_prefix_evidence_path,
        fenics_package_lock_path,
    )
    fenics_meta = conda_meta_records(fenics_prefix)
    for name in RUNTIME_PACKAGES:
        expected = runtime[name]
        installed = fenics_meta.get(name)
        if not isinstance(installed, dict):
            raise BuildDepsError(f"verified FEniCS prefix is missing runtime package {name}")
        verify_installed_identity(installed, expected, f"runtime package {name}")
        verified = verified_packages.get(name)
        if not isinstance(verified, dict) or normalize_identity(verified) != normalize_identity(expected):
            raise BuildDepsError(f"prefix verification package identity differs for {name}")
        archive = verified_archives.get(name)
        if (
            not isinstance(archive, dict)
            or archive.get("filename") != package_filename(expected)
            or archive.get("sha256") != expected["sha256"]
            or not archive.get("cache_paths")
        ):
            raise BuildDepsError(f"prefix verification archive identity differs for {name}")

    dev_meta = conda_meta_records(development_prefix)
    if set(dev_meta) != set(DEVELOPMENT_PACKAGES):
        raise BuildDepsError(
            f"development prefix package set must be exactly {list(DEVELOPMENT_PACKAGES)!r}: {sorted(dev_meta)!r}"
        )
    development_archives = {}
    for name in DEVELOPMENT_PACKAGES:
        verify_installed_identity(dev_meta[name], development[name], f"development package {name}")
        development_archives[name] = verify_cached_archive(package_cache, development[name])

    if output_dir.exists() and any(output_dir.iterdir()):
        raise BuildDepsError(f"libxml2 build-dependency output directory must be absent or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if manifest_path.parent.resolve() != output_dir.resolve():
        raise BuildDepsError("libxml2 build-dependency manifest must be written directly inside the output directory")
    prefix = output_dir / "prefix"
    prefix.mkdir()

    headers: dict[str, str] = {}
    for package_name in DEVELOPMENT_PACKAGES:
        for relative, expected_file in development[package_name]["files"].items():
            source = verify_owned_file(
                development_prefix,
                dev_meta[package_name],
                relative,
                expected_file,
                f"development header {relative}",
            )
            destination = prefix / safe_relative(relative, f"development header {relative}")
            copy_regular(source, destination, expected_file["sha256"], expected_file["size"])
            headers[relative] = expected_file["sha256"]

    libraries: dict[str, dict[str, Any]] = {}
    for library_name in ("zlib", "lzma"):
        output_record = lock["output"]["libraries"][library_name]
        package_name = output_record["source_package"]
        relative = output_record["source_path"]
        expected_file = runtime[package_name]["files"][relative]
        source = verify_owned_file(
            fenics_prefix,
            fenics_meta[package_name],
            relative,
            expected_file,
            f"runtime library {library_name}",
        )
        destination_relative = output_record["relative_path"]
        destination = prefix / safe_relative(destination_relative, f"output library {library_name}")
        copy_regular(source, destination, expected_file["sha256"], expected_file["size"])
        libraries[library_name] = {
            "relative_path": destination_relative,
            "sha256": expected_file["sha256"],
            "source_package": package_name,
            "source_path": relative,
        }

    tree = tree_identity(prefix)
    expected_file_count = sum(len(record["files"]) for record in development.values()) + 2
    if tree["file_count"] != expected_file_count:
        raise BuildDepsError(
            f"prepared libxml2 build-dependency file count mismatch: {tree['file_count']} != {expected_file_count}"
        )
    manifest = {
        "format_version": 1,
        "kind": "fenics-libxml2-build-deps",
        "target": lock["target"],
        "policy": lock["policy"],
        "prefix": "prefix",
        "headers": {relative: headers[relative] for relative in sorted(headers)},
        "libraries": libraries,
        "source_packages": {
            "development": [normalize_identity(development[name]) for name in DEVELOPMENT_PACKAGES],
            "runtime": [normalize_identity(runtime[name]) for name in RUNTIME_PACKAGES],
        },
        "input_evidence": {
            "build_deps_lock_sha256": sha256_file(lock_path),
            "fenics_package_lock_sha256": sha256_file(fenics_package_lock_path),
            "fenics_prefix_verification_sha256": sha256_file(fenics_prefix_evidence_path),
            "fenics_prefix_verified_package_count": verification.get("installed_record_count"),
            "development_replay_input_format": "@EXPLICIT-url-plus-sha256",
            "dependency_solver_used": False,
            "development_archives": [development_archives[name] for name in DEVELOPMENT_PACKAGES],
        },
        "tree": {"file_count": tree["file_count"], "tree_sha256": tree["tree_sha256"]},
        "production_frozen": True,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    explicit = subparsers.add_parser("explicit", help="render the exact development package transaction")
    explicit.add_argument("--lock", type=Path, required=True)
    explicit.add_argument("--fenics-package-lock", type=Path, required=True)
    explicit.add_argument("--output", type=Path, required=True)

    prepare_parser = subparsers.add_parser("prepare", help="prepare the build-only prefix from verified package inputs")
    prepare_parser.add_argument("--lock", type=Path, required=True)
    prepare_parser.add_argument("--fenics-package-lock", type=Path, required=True)
    prepare_parser.add_argument("--fenics-prefix-evidence", type=Path, required=True)
    prepare_parser.add_argument("--fenics-prefix", type=Path, required=True)
    prepare_parser.add_argument("--development-prefix", type=Path, required=True)
    prepare_parser.add_argument("--package-cache", type=Path, required=True)
    prepare_parser.add_argument("--output-dir", type=Path, required=True)
    prepare_parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    try:
        lock_path = args.lock.resolve()
        fenics_lock_path = args.fenics_package_lock.resolve()
        lock, development, _ = validate_lock(lock_path, fenics_lock_path)
        if args.command == "explicit":
            output = args.output.resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(render_explicit(development), encoding="utf-8")
            print(f"libxml2_build_deps_lock_sha256={sha256_file(lock_path)}")
            print(f"development_package_count={len(development)}")
            print("dependency_solver=disabled")
            print(f"explicit={output}")
            return 0

        manifest_path = args.manifest.resolve()
        manifest = prepare(
            lock_path=lock_path,
            fenics_package_lock_path=fenics_lock_path,
            fenics_prefix_evidence_path=args.fenics_prefix_evidence.resolve(),
            fenics_prefix=args.fenics_prefix.resolve(),
            development_prefix=args.development_prefix.resolve(),
            package_cache=args.package_cache.resolve(),
            output_dir=args.output_dir.resolve(),
            manifest_path=manifest_path,
        )
        print(f"libxml2_build_deps_manifest={manifest_path}")
        print(f"libxml2_build_deps_tree_sha256={manifest['tree']['tree_sha256']}")
        print("dependency_solver_used=false")
        print("network_access_after_replay=false")
        return 0
    except BuildDepsError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
