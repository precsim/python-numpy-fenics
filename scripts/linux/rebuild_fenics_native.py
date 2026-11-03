#!/usr/bin/env python3
"""Fail-closed Linux FEniCS selective native rebuild runner.

This script deliberately separates immutable-input validation from execution.
It does not resolve dependencies or construct the private C++ provider.  The
checked-in GCC-14 build-tool lock supplies the C/C++ toolchain, and separately
controlled manifests identify the Python wheel builder and libfenics_cxxrt.so.1.

Successful output is a rebuild candidate, not qualified source-rebuild
provenance.  Qualification remains a separate hosted-runtime gate.
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
import sys
import tarfile
from urllib.parse import urlparse
import zipfile


REQUIRED_SOURCES = ("basix", "dolfinx", "fmt", "spdlog", "pugixml", "libxml2")
REQUIRED_PACKAGES = (
    "fmt",
    "spdlog",
    "pugixml",
    "fenics-libbasix",
    "fenics-basix",
    "fenics-libdolfinx",
    "fenics-dolfinx",
    "libxml2-16",
)
EXPECTED_STOCK_ABI_VIOLATING_PACKAGES = (
    "fenics-basix",
    "fenics-dolfinx",
    "fenics-libbasix",
    "fenics-libdolfinx",
    "fmt",
    "icu",
    "kahip",
    "libadios2",
    "pugixml",
    "spdlog",
    "zeromq",
)
BUILD_TOOL_BINARIES = {
    "cc": "x86_64-conda-linux-gnu-gcc",
    "cxx": "x86_64-conda-linux-gnu-g++",
    "cmake": "cmake",
    "ninja": "ninja",
    "readelf": "x86_64-conda-linux-gnu-readelf",
    "nm": "x86_64-conda-linux-gnu-nm",
    "pkg_config": "pkg-config",
    "make": "make",
}
PYTHON_BUILD_MODULES = (
    "pip",
    "packaging",
    "pyproject-metadata",
    "pathspec",
    "scikit-build-core",
    "nanobind",
    "wheel",
)
PYTHON_BUILD_VERSIONS = {
    "nanobind": "2.9.2",
    "packaging": "25.0",
    "pathspec": "0.12.1",
    "pip": "25.3",
    "pyproject-metadata": "0.9.1",
    "scikit-build-core": "0.11.6",
    "wheel": "0.48.0",
}
PYTHON_BUILD_SITE_SHA256 = "de8582981bdeea3c178eeb71265427229a66ceb421ad435c24136cce67b94147"
PYTHON_BUILD_SITE_FILE_COUNT = 725
ABI_CEILINGS = {
    "GLIBC": (2, 17),
    "GLIBCXX": (3, 4, 19),
    "CXXABI": (1, 3, 7),
    "GCC": (4, 8, 0),
}
HANDOFF_FORMAT_VERSION = 1
HANDOFF_KIND = "linux-fenics-native-rebuild-handoff"
HANDOFF_STATUS = "candidate-unqualified"
CANDIDATE_BUILD_HANDOFF_FORMAT_VERSION = 1
CANDIDATE_BUILD_HANDOFF_KIND = "linux-fenics-native-build-candidate-handoff"
CANDIDATE_BUILD_HANDOFF_STATUS = "candidate-built-unqualified"
HANDOFF_OMISSION_PACKAGES = ("kahip", "libadios2", "zeromq")
HANDOFF_PROVIDER_OWNER = "python-numpy-fenics-native-provider"
REBUILD_COMPONENT_OWNERS = {
    "basix": "fenics-libbasix",
    "fmt": "fmt",
    "spdlog": "spdlog",
    "pugixml": "pugixml",
    "libxml2": "libxml2-16",
    "dolfinx": "fenics-libdolfinx",
}
WHEEL_OWNERS = {
    "fenics_basix": "fenics-basix",
    "fenics_dolfinx": "fenics-dolfinx",
}
STOCK_PYTHON_SITE = PurePosixPath("lib/python3.12/site-packages")
TARGET_PYTHON_SITE = PurePosixPath("lib/python3.14/site-packages")
WHEEL_MEMBER_ROOTS = {
    "fenics-basix": ("basix", "fenics_basix-0.11.0.dist-info"),
    "fenics-dolfinx": ("dolfinx", "fenics_dolfinx-0.11.0.dist-info"),
}


class InputError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"JSON input must contain an object: {path}")
    return value


def require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise InputError(f"{label} must be a lowercase SHA-256")
    return value


def canonical_json_sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def normalize_candidate_output_binding(value: object, label: str = "candidate output binding") -> dict:
    if not isinstance(value, dict) or set(value) != {"replacements", "elf", "wheels"}:
        raise InputError(f"{label} must contain exactly replacements, elf, and wheels")

    def normalize_records(
        records: object,
        *,
        kind: str,
        required_keys: set[str],
        key_name: str,
    ) -> list[dict]:
        if not isinstance(records, list):
            raise InputError(f"{label} {kind} records must be a list")
        normalized: list[dict] = []
        keys: list[str] = []
        for record in records:
            if not isinstance(record, dict) or set(record) != required_keys:
                raise InputError(f"{label} contains an invalid {kind} record: {record!r}")
            raw_key = record.get(key_name)
            relative = safe_handoff_relative(raw_key, f"{label} {kind} {key_name}").as_posix()
            digest = require_sha(record.get("sha256"), f"{label} {kind} {relative} sha256")
            normalized_record = dict(record)
            normalized_record[key_name] = relative
            normalized_record["sha256"] = digest
            if "owner" in required_keys:
                owner = record.get("owner")
                if not isinstance(owner, str) or not owner:
                    raise InputError(f"{label} {kind} {relative} owner is invalid")
            if "bytes" in required_keys:
                size = record.get("bytes")
                if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                    raise InputError(f"{label} {kind} {relative} byte count is invalid")
            keys.append(relative)
            normalized.append(normalized_record)
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise InputError(f"{label} {kind} records must be uniquely sorted by {key_name}")
        return normalized

    return {
        "replacements": normalize_records(
            value["replacements"],
            kind="replacement",
            required_keys={"destination", "owner", "sha256", "bytes"},
            key_name="destination",
        ),
        "elf": normalize_records(
            value["elf"],
            kind="ELF",
            required_keys={"path", "sha256"},
            key_name="path",
        ),
        "wheels": normalize_records(
            value["wheels"],
            kind="wheel",
            required_keys={"path", "sha256", "bytes"},
            key_name="path",
        ),
    }


def safe_handoff_relative(raw: object, label: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise InputError(f"{label} must be a canonical POSIX relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != raw:
        raise InputError(f"{label} must be a canonical POSIX relative path: {raw!r}")
    return path


def verify_file(path: Path, expected: object, label: str) -> dict:
    if not path.is_file() or path.is_symlink():
        raise InputError(f"{label} is missing or is not a regular file: {path}")
    expected_sha = require_sha(expected, f"{label} sha256")
    actual = sha256(path)
    if actual != expected_sha:
        raise InputError(f"{label} SHA-256 mismatch: {actual} != {expected_sha}")
    return {"path": str(path.resolve()), "sha256": actual, "size": path.stat().st_size}


def package_records(lock: dict) -> dict[str, dict]:
    records = lock.get("packages")
    if not isinstance(records, list):
        raise InputError("package lock has no packages list")
    result: dict[str, dict] = {}
    for record in records:
        if isinstance(record, dict) and isinstance(record.get("name"), str):
            name = record["name"]
            if name in result:
                raise InputError(f"duplicate package-lock record: {name}")
            result[name] = record
    return result


def locate_archive(root: Path, filename: str) -> Path:
    matches = sorted(path for path in root.rglob(filename) if path.is_file())
    if len(matches) != 1:
        raise InputError(f"expected exactly one package archive {filename!r} below {root}, found {len(matches)}")
    return matches[0]


def validate_package_closure(lock_path: Path, cache: Path, source_lock: dict) -> list[dict]:
    lock = load_json(lock_path)
    records = package_records(lock)
    expected = source_lock.get("rebuild_package_closure")
    if expected != list(REQUIRED_PACKAGES):
        raise InputError("source manifest rebuild_package_closure differs from the fixed minimal closure")
    evidence = []
    for name in REQUIRED_PACKAGES:
        record = records.get(name)
        if record is None:
            raise InputError(f"package lock is missing rebuild input {name}")
        expected_sha = require_sha(record.get("sha256"), f"package {name} sha256")
        url = record.get("url")
        if not isinstance(url, str) or not url.endswith((".conda", ".tar.bz2")):
            raise InputError(f"package {name} has invalid locked URL")
        archive = locate_archive(cache, url.rsplit("/", 1)[-1])
        file_evidence = verify_file(archive, expected_sha, f"package {name}")
        evidence.append({
            "name": name,
            "version": record.get("version"),
            "build": record.get("build"),
            "url": url,
            **file_evidence,
        })
    return evidence


def validate_sources(source_lock_path: Path, source_dir: Path) -> tuple[dict, list[dict]]:
    manifest = load_json(source_lock_path)
    if manifest.get("schema_version") != 1:
        raise InputError("unsupported FEniCS rebuild source-manifest schema")
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or tuple(sources) != REQUIRED_SOURCES:
        raise InputError("source manifest must contain exactly the fixed Basix/DOLFINx/fmt/spdlog/pugixml/libxml2 sources")
    evidence = []
    for key in REQUIRED_SOURCES:
        record = sources[key]
        if not isinstance(record, dict):
            raise InputError(f"invalid source record: {key}")
        filename = record.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise InputError(f"invalid source filename: {key}")
        item = verify_file(source_dir / filename, record.get("sha256"), f"source {key}")
        evidence.append({"component": key, "filename": filename, "url": record.get("url"), **item})
    return manifest, evidence


def extracted_package_dir(archive: Path) -> Path:
    name = archive.name
    if name.endswith(".conda"):
        return archive.with_name(name[: -len(".conda")])
    if name.endswith(".tar.bz2"):
        return archive.with_name(name[: -len(".tar.bz2")])
    raise InputError(f"unsupported package archive extension: {archive}")


def validate_recipe_bindings(source_manifest: dict, lock_path: Path, package_cache: Path) -> tuple[list[dict], Path, dict]:
    records = package_records(load_json(lock_path))
    bindings = []
    for key in REQUIRED_SOURCES:
        source = source_manifest["sources"][key]
        package_name = source.get("recipe_package")
        if not isinstance(package_name, str) or package_name not in records:
            raise InputError(f"source {key} has no locked recipe package")
        record = records[package_name]
        archive = locate_archive(package_cache, str(record["url"]).rsplit("/", 1)[-1])
        verify_file(archive, record.get("sha256"), f"recipe package {package_name}")
        recipe_root = extracted_package_dir(archive) / "info" / "recipe"
        recipe_files = [path for path in (recipe_root / "recipe.yaml", recipe_root / "meta.yaml") if path.is_file()]
        if not recipe_files:
            raise InputError(f"locked recipe metadata is missing for source {key}: {recipe_root}")
        expected_sha = require_sha(source.get("sha256"), f"source {key} sha256")
        matching = [path for path in recipe_files if expected_sha in path.read_text(encoding="utf-8", errors="strict")]
        if not matching:
            raise InputError(f"source {key} SHA-256 is absent from locked recipe metadata")
        bindings.append({
            "component": key,
            "recipe_package": package_name,
            "recipe_package_sha256": str(record["sha256"]).lower(),
            "recipe_metadata": str(matching[0].resolve()),
            "source_sha256": expected_sha,
        })

    patches = source_manifest.get("patches")
    if not isinstance(patches, dict) or set(patches) != {"spdlog_fix"}:
        raise InputError("source manifest must contain exactly the locked spdlog patch")
    patch_record = patches["spdlog_fix"]
    package_name = patch_record.get("recipe_package")
    recipe_path = patch_record.get("recipe_path")
    if package_name != "spdlog" or not isinstance(recipe_path, str):
        raise InputError("invalid locked spdlog patch binding")
    record = records.get(package_name)
    if record is None:
        raise InputError("package lock is missing spdlog")
    archive = locate_archive(package_cache, str(record["url"]).rsplit("/", 1)[-1])
    patch_path = extracted_package_dir(archive) / recipe_path
    patch_evidence = verify_file(patch_path, patch_record.get("sha256"), "spdlog recipe patch")
    return bindings, patch_path, patch_evidence


def verify_build_toolchain(explicit: Path, prefix: Path, package_cache: Path, evidence: Path) -> tuple[dict, dict[str, Path]]:
    repo = Path(__file__).resolve().parents[2]
    verifier = repo / "scripts" / "linux" / "verify_fenics_build_tools.py"
    run(
        sys.executable,
        str(verifier),
        "--explicit", str(explicit),
        "--source-prefix", str(prefix),
        "--package-cache", str(package_cache),
        "--evidence", str(evidence),
        "--exact-prefix",
    )
    payload = load_json(evidence)
    if payload.get("package_count") != 41 or payload.get("dependency_solver_used") is not False:
        raise InputError("frozen build-tool verifier returned an unexpected contract")
    if payload.get("roots", {}).get("gcc_linux-64", {}).get("version") != "14.4.0":
        raise InputError("frozen build-tool verifier did not prove GCC 14.4.0")
    tools: dict[str, Path] = {}
    for name, relative in BUILD_TOOL_BINARIES.items():
        path = (prefix / "bin" / relative).resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise InputError(f"verified build-tool prefix is missing executable {name}: {path}")
        tools[name] = path
    payload["executables"] = {
        name: {"path": str(path), "sha256": sha256(path)} for name, path in sorted(tools.items())
    }
    return payload, tools


def tree_sha256(root: Path) -> tuple[str, int]:
    if not root.is_dir():
        raise InputError(f"Python wheel-builder site-packages is missing: {root}")
    digest = hashlib.sha256()
    count = 0
    for item in sorted((candidate for candidate in root.rglob("*") if candidate.is_file()), key=lambda candidate: candidate.relative_to(root).as_posix()):
        if item.is_symlink():
            raise InputError(f"Python wheel-builder site-packages contains a symlink: {item}")
        relative = item.relative_to(root).as_posix()
        item_sha = sha256(item)
        size = item.stat().st_size
        digest.update(relative.encode("utf-8") + b"\0" + item_sha.encode("ascii") + b"\0" + str(size).encode("ascii") + b"\n")
        count += 1
    if count == 0:
        raise InputError("Python wheel-builder site-packages is empty")
    return digest.hexdigest(), count


def resolve_manifest_path(manifest_path: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise InputError(f"{label} path is missing")
    candidate = Path(value)
    if candidate.is_absolute():
        raise InputError(f"{label} path must be relative for reproducible builder manifests")
    return (manifest_path.parent / candidate).resolve()


def validate_python_builder(path: Path) -> tuple[dict, Path, Path]:
    manifest = load_json(path)
    if manifest.get("format_version") != 1 or manifest.get("kind") != "fenics-python-wheel-builder":
        raise InputError("unsupported Python wheel-builder manifest identity")
    if manifest.get("target") != {
        "architecture": "x86_64",
        "python_version": "3.14.10",
        "python_abi": "cp314",
        "glibc_floor": "2.17",
    }:
        raise InputError("Python wheel-builder target differs from the frozen Linux CPython contract")
    if manifest.get("policy") != {
        "dependency_resolution": False,
        "index_access": False,
        "network_access_after_input_verification": False,
        "wheel_installation": "deterministic-pure-wheel-extraction",
        "base_runtime_modified": False,
    }:
        raise InputError("Python wheel-builder policy differs from the offline no-solve contract")
    python_record = manifest.get("python")
    if not isinstance(python_record, dict):
        raise InputError("Python wheel-builder manifest has no python record")
    executable = resolve_manifest_path(path, python_record.get("path"), "Python wheel-builder executable")
    verify_file(executable, python_record.get("sha256"), "Python wheel-builder executable")
    if not os.access(executable, os.X_OK):
        raise InputError(f"Python wheel-builder executable is not executable: {executable}")
    pythonpath_record = manifest.get("pythonpath")
    if not isinstance(pythonpath_record, dict):
        raise InputError("Python wheel-builder manifest has no pythonpath record")
    pythonpath = resolve_manifest_path(path, pythonpath_record.get("path"), "Python wheel-builder pythonpath")
    actual_tree_sha, actual_file_count = tree_sha256(pythonpath)
    if (
        pythonpath_record.get("tree_sha256") != PYTHON_BUILD_SITE_SHA256
        or pythonpath_record.get("file_count") != PYTHON_BUILD_SITE_FILE_COUNT
        or actual_tree_sha != PYTHON_BUILD_SITE_SHA256
        or actual_file_count != PYTHON_BUILD_SITE_FILE_COUNT
    ):
        raise InputError("Python wheel-builder site-packages tree identity mismatch")
    expected_modules = manifest.get("modules")
    if expected_modules != PYTHON_BUILD_VERSIONS:
        raise InputError(f"Python wheel-builder modules must be exactly {PYTHON_BUILD_VERSIONS!r}")
    wheels = manifest.get("wheels")
    if not isinstance(wheels, list) or [record.get("name") for record in wheels if isinstance(record, dict)] != list(PYTHON_BUILD_MODULES):
        raise InputError("Python wheel-builder wheel evidence differs from the fixed seven-wheel closure")
    for record in wheels:
        if not isinstance(record, dict):
            raise InputError("Python wheel-builder wheel evidence contains an invalid record")
        name = record.get("name")
        if name not in PYTHON_BUILD_VERSIONS or record.get("version") != PYTHON_BUILD_VERSIONS[name]:
            raise InputError(f"Python wheel-builder wheel version differs for {name!r}")
        require_sha(record.get("sha256"), f"Python wheel-builder wheel {name} sha256")
    base_runtime = manifest.get("base_runtime")
    base_tcl_tk = base_runtime.get("tcl_tk") if isinstance(base_runtime, dict) else None
    if (
        not isinstance(base_runtime, dict)
        or base_runtime.get("profile") != "numpy"
        or base_runtime.get("python_version") != "3.14.10"
        or base_runtime.get("python_abi") != "cp314"
        or base_runtime.get("python_sha256") != python_record.get("sha256")
        or not isinstance(base_tcl_tk, dict)
        or base_tcl_tk.get("version") != "8.6.18"
        or base_tcl_tk.get("ownership") != "producer"
        or not re.fullmatch(r"[0-9a-f]{64}", str(base_tcl_tk.get("provenance_sha256", "")))
    ):
        raise InputError("Python wheel-builder base-runtime identity is incomplete or inconsistent")
    for key in ("runtime_manifest_sha256", "qualified_archive_sha256"):
        require_sha(base_runtime.get(key), f"Python wheel-builder base runtime {key}")
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONUSERBASE", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        environment.pop(name, None)
    environment.update({
        "PYTHONPATH": str(pythonpath),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
    })
    code = (
        "import importlib.metadata,json; "
        f"names={list(PYTHON_BUILD_MODULES)!r}; "
        "print(json.dumps({n:importlib.metadata.version(n) for n in names},sort_keys=True))"
    )
    actual_modules = json.loads(run(executable, "-s", "-c", code, env=environment).stdout)
    if actual_modules != expected_modules:
        raise InputError(f"Python wheel-builder module versions differ: {actual_modules!r} != {expected_modules!r}")
    return (
        {
            **manifest,
            "python": {**python_record, "path": str(executable)},
            "pythonpath": {**pythonpath_record, "path": str(pythonpath)},
        },
        executable,
        pythonpath,
    )


def validate_libxml2_build_deps(path: Path, package_lock: Path) -> tuple[dict, Path, Path, Path]:
    manifest = load_json(path)
    if manifest.get("format_version") != 1 or manifest.get("kind") != "fenics-libxml2-build-deps":
        raise InputError("unsupported libxml2 build-dependency manifest identity")
    repo = Path(__file__).resolve().parents[2]
    build_deps_lock_path = repo / "locks" / "linux" / "linux-x86_64-fenics-libxml2-build-deps.json"
    build_deps_lock = load_json(build_deps_lock_path)
    if manifest.get("target") != build_deps_lock.get("target"):
        raise InputError("libxml2 build-dependency target differs from the checked-in build-deps lock")
    if manifest.get("policy") != build_deps_lock.get("policy"):
        raise InputError("libxml2 build-dependency policy differs from the checked-in build-deps lock")
    input_evidence = manifest.get("input_evidence")
    if not isinstance(input_evidence, dict):
        raise InputError("libxml2 build-dependency manifest has no input evidence")
    if input_evidence.get("build_deps_lock_sha256") != sha256(build_deps_lock_path):
        raise InputError("libxml2 build-dependency lock identity mismatch")
    if input_evidence.get("fenics_package_lock_sha256") != sha256(package_lock):
        raise InputError("libxml2 build-dependency FEniCS package-lock identity mismatch")
    require_sha(input_evidence.get("fenics_prefix_verification_sha256"), "libxml2 build-dependency prefix verification sha256")
    if (
        input_evidence.get("fenics_prefix_verified_package_count") != 128
        or input_evidence.get("development_replay_input_format") != "@EXPLICIT-url-plus-sha256"
        or input_evidence.get("dependency_solver_used") is not False
    ):
        raise InputError("libxml2 build-dependency input evidence does not prove the exact no-solve replay")
    development_lock = build_deps_lock.get("development_packages")
    runtime_lock = build_deps_lock.get("runtime_packages")
    if not isinstance(development_lock, list) or not isinstance(runtime_lock, list):
        raise InputError("checked-in libxml2 build-dependency package records are invalid")
    identity_fields = ("name", "version", "build", "build_number", "subdir", "channel", "url", "sha256", "md5")
    expected_source_packages = {
        "development": [{field: record.get(field) for field in identity_fields} for record in development_lock],
        "runtime": [{field: record.get(field) for field in identity_fields} for record in runtime_lock],
    }
    if manifest.get("source_packages") != expected_source_packages:
        raise InputError("libxml2 build-dependency source package identities differ from the checked-in lock")
    archives = input_evidence.get("development_archives")
    if not isinstance(archives, list) or len(archives) != len(development_lock):
        raise InputError("libxml2 build-dependency development archive evidence is incomplete")
    for archive, record in zip(archives, development_lock, strict=True):
        expected_filename = Path(urlparse(str(record.get("url", ""))).path).name
        if (
            not isinstance(archive, dict)
            or archive.get("name") != record.get("name")
            or archive.get("filename") != expected_filename
            or archive.get("sha256") != record.get("sha256")
            or not isinstance(archive.get("cache_paths"), list)
            or not archive.get("cache_paths")
        ):
            raise InputError(f"libxml2 build-dependency archive evidence differs for {record.get('name')}")

    prefix = resolve_manifest_path(path, manifest.get("prefix"), "libxml2 build-dependency prefix")
    if not prefix.is_dir():
        raise InputError(f"libxml2 build-dependency prefix is missing: {prefix}")
    headers = manifest.get("headers")
    expected_headers = build_deps_lock.get("output", {}).get("headers")
    if not isinstance(headers, dict) or not isinstance(expected_headers, dict):
        raise InputError("libxml2 build-dependency manifest has no headers object")
    if headers != expected_headers:
        raise InputError("libxml2 build-dependency header hashes differ from the checked-in package pins")
    header_evidence = {}
    for relative, expected_sha in sorted(headers.items()):
        header_evidence[relative] = verify_file(prefix / relative, expected_sha, f"libxml2 build header {relative}")
    libraries = manifest.get("libraries")
    expected_libraries = build_deps_lock.get("output", {}).get("libraries")
    if not isinstance(libraries, dict) or set(libraries) != {"zlib", "lzma"}:
        raise InputError("libxml2 build-dependency libraries must be exactly zlib and lzma")
    if not isinstance(expected_libraries, dict):
        raise InputError("checked-in libxml2 build-dependency library contract is invalid")
    library_paths = {}
    library_evidence = {}
    for name in ("zlib", "lzma"):
        record = libraries[name]
        expected_record = expected_libraries.get(name)
        if not isinstance(record, dict) or not isinstance(record.get("relative_path"), str):
            raise InputError(f"invalid libxml2 build library record: {name}")
        if not isinstance(expected_record, dict) or record != expected_record:
            raise InputError(f"libxml2 build library record differs from the checked-in package pins: {name}")
        relative = Path(record["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise InputError(f"invalid libxml2 build library path: {relative}")
        library = prefix / relative
        library_evidence[name] = verify_file(library, record.get("sha256"), f"libxml2 build library {name}")
        library_paths[name] = library.resolve()
    expected_files = set(headers) | {record["relative_path"] for record in libraries.values()}
    actual_files = {
        candidate.relative_to(prefix).as_posix()
        for candidate in prefix.rglob("*")
        if candidate.is_file()
    }
    if actual_files != expected_files:
        raise InputError(
            f"libxml2 build-dependency output file set differs: missing={sorted(expected_files-actual_files)} extra={sorted(actual_files-expected_files)}"
        )
    actual_tree_sha, actual_file_count = tree_sha256(prefix)
    tree = manifest.get("tree")
    if (
        not isinstance(tree, dict)
        or tree.get("file_count") != actual_file_count
        or tree.get("tree_sha256") != actual_tree_sha
    ):
        raise InputError("libxml2 build-dependency tree identity mismatch")
    if manifest.get("production_frozen") is not True:
        raise InputError("libxml2 build-dependency manifest is not production-frozen")
    evidence = {
        "format_version": 1,
        "kind": "fenics-libxml2-build-deps",
        "prefix": str(prefix),
        "headers": header_evidence,
        "libraries": library_evidence,
        "source_packages": expected_source_packages,
        "input_evidence": input_evidence,
        "tree": {"file_count": actual_file_count, "tree_sha256": actual_tree_sha},
        "production_frozen": True,
    }
    return evidence, prefix / "include", library_paths["zlib"], library_paths["lzma"]


def validate_native_closure_report(path: Path) -> dict:
    report = load_json(path)
    flags = report.get("dolfinx_build_flags")
    if flags != {"DOLFINX_ENABLE_ADIOS2": "OFF", "DOLFINX_ENABLE_KAHIP": "OFF"}:
        raise InputError("native closure proof does not match required DOLFINx ADIOS2/KAHIP=OFF flags")
    if report.get("unresolved_non_system") != []:
        raise InputError("native closure proof has unresolved non-system dependencies")
    candidates = report.get("candidates")
    if not isinstance(candidates, dict):
        raise InputError("native closure proof has no candidates object")
    omitted: dict[str, dict] = {}
    for name in HANDOFF_OMISSION_PACKAGES:
        candidate = candidates.get(name)
        if not isinstance(candidate, dict) or candidate.get("reached_paths") != []:
            raise InputError(f"conditional omission is not proven for {name}")
        reverse = candidate.get("reverse_direct_noncandidate")
        if (
            not isinstance(reverse, list)
            or any(not isinstance(item, dict) for item in reverse)
            or any(item.get("in_closure") is not False for item in reverse)
        ):
            raise InputError(f"conditional omission has an in-closure reverse dependency for {name}")
        staged_paths = candidate.get("staged_paths")
        if not isinstance(staged_paths, list) or any(not isinstance(item, str) for item in staged_paths):
            raise InputError(f"conditional omission has invalid staged paths for {name}")
        normalized_staged_paths = sorted(
            safe_handoff_relative(item, f"{name} omission staged path").as_posix()
            for item in staged_paths
        )
        omission_evidence = {
            "reached_paths": [],
            "reverse_direct_noncandidate": sorted(
                reverse, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
            ),
            "staged_paths": normalized_staged_paths,
            "abi_maxima": candidate.get("abi_maxima"),
        }
        omitted[name] = {
            "staged_path_count": len(normalized_staged_paths),
            "abi_maxima": candidate.get("abi_maxima"),
            "evidence": omission_evidence,
            "evidence_sha256": canonical_json_sha(omission_evidence),
        }
    icu = candidates.get("icu")
    if not isinstance(icu, dict):
        raise InputError("native closure proof has no ICU candidate analysis")
    icu_reached = icu.get("reached_paths")
    if not isinstance(icu_reached, list):
        raise InputError("native closure proof has invalid ICU reached paths")
    candidate_output_binding = normalize_candidate_output_binding(
        report.get("candidate_output_binding"), "native closure candidate output binding"
    )
    candidate_output_binding_sha = require_sha(
        report.get("candidate_output_binding_sha256"), "native closure candidate output binding sha256"
    )
    if canonical_json_sha(candidate_output_binding) != candidate_output_binding_sha:
        raise InputError("native closure candidate output binding SHA-256 mismatch")
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "closure_elf_count": report.get("closure_elf_count"),
        "dolfinx_build_flags": flags,
        "conditional_omissions": omitted,
        "icu_reached_paths": icu_reached,
        "icu_resolution_required": bool(icu_reached),
        "candidate_output_binding": candidate_output_binding,
        "candidate_output_binding_sha256": candidate_output_binding_sha,
    }


def validate_stock_abi_preflight(path: Path, package_lock: Path) -> dict:
    report = load_json(path)
    if report.get("format_version") != 1 or report.get("stage") != "stock-binary-closure-before-selective-rebuild":
        raise InputError("unsupported stock ABI preflight identity")
    if report.get("package_lock_sha256") != sha256(package_lock):
        raise InputError("stock ABI preflight package-lock identity mismatch")
    if report.get("status") != "fail":
        raise InputError("stock ABI preflight must record the executable stock failure")
    if report.get("source_rebuilds_authorized") is not False:
        raise InputError("stock ABI preflight must not authorize source rebuild provenance")
    violations = report.get("ceiling_violations")
    unexpected = report.get("unexpected_nonnumeric_requirements")
    if not isinstance(violations, list) or not isinstance(unexpected, list):
        raise InputError("stock ABI preflight has invalid violation lists")
    violation_count = report.get("violation_count")
    if not isinstance(violation_count, int) or violation_count <= 0 or violation_count != len(violations):
        raise InputError("stock ABI preflight violation count is inconsistent")
    violating_packages = report.get("violating_packages")
    if violating_packages != list(EXPECTED_STOCK_ABI_VIOLATING_PACKAGES):
        raise InputError("stock ABI preflight violating package set differs from the pinned selective-rebuild boundary")
    fingerprint = require_sha(report.get("selection_fingerprint_sha256"), "stock ABI selection fingerprint")
    fingerprint_payload = dict(report)
    fingerprint_payload.pop("selection_fingerprint_sha256", None)
    actual_fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if actual_fingerprint != fingerprint:
        raise InputError("stock ABI preflight selection fingerprint mismatch")
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "status": "fail",
        "violation_count": violation_count,
        "violating_packages": violating_packages,
        "selection_fingerprint_sha256": fingerprint,
        "source_rebuilds_authorized": False,
    }


def run(tool: Path | str, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run([str(tool), *args], cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise InputError(f"command failed ({result.returncode}): {tool} {' '.join(args)}\n{result.stdout}")
    return result


def inspect_elf(path: Path, readelf: Path, *, require_relative_rpath: bool = True) -> dict:
    if path.open("rb").read(4) != b"\x7fELF":
        raise InputError(f"expected ELF output: {path}")
    dynamic = run(readelf, "-dW", str(path)).stdout
    versions = run(readelf, "--version-info", "-W", str(path)).stdout
    needed = sorted(set(re.findall(r"Shared library: \[([^\]]+)\]", dynamic)))
    sonames = re.findall(r"Library soname: \[([^\]]+)\]", dynamic)
    rpaths = re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]*)\]", dynamic)
    required = sorted(set(re.findall(r"\bName: ((?:GLIBC|GLIBCXX|CXXABI|GCC)_[A-Za-z0-9_.]+)", versions)))
    private_versions = sorted(set(re.findall(r"\bName: (FENICS_CXXRT_[A-Za-z0-9_.]+)", versions)))
    maxima: dict[str, str | None] = {}
    for namespace, ceiling in ABI_CEILINGS.items():
        numeric = []
        for token in required:
            prefix = namespace + "_"
            suffix = token[len(prefix):] if token.startswith(prefix) else ""
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", suffix):
                numeric.append(tuple(int(part) for part in suffix.split(".")))
        maximum = max(numeric, default=None)
        if maximum is not None and maximum > ceiling:
            raise InputError(f"ELF ABI ceiling exceeded for {path}: {namespace}_{'.'.join(map(str, maximum))}")
        maxima[namespace] = None if maximum is None else ".".join(map(str, maximum))
    if require_relative_rpath:
        for entry in rpaths:
            for part in entry.split(":"):
                if part and not part.startswith("$ORIGIN"):
                    raise InputError(f"non-relative ELF RPATH/RUNPATH in {path}: {part}")
    return {
        "path": str(path),
        "sha256": sha256(path),
        "soname": sonames[0] if len(sonames) == 1 else None,
        "needed": needed,
        "rpath": rpaths,
        "required_versions": required,
        "private_versions": private_versions,
        "version_max": maxima,
    }


def validate_provider(path: Path, tools: dict[str, Path]) -> tuple[dict, Path, dict]:
    manifest = load_json(path)
    if manifest.get("format_version") != 1 or manifest.get("kind") != "fenics-private-cxx-runtime":
        raise InputError("unsupported private-provider manifest identity")
    library = manifest.get("library")
    if not isinstance(library, dict):
        raise InputError("private-provider manifest has no library record")
    provider = Path(str(library.get("path", "")))
    verify_file(provider, library.get("sha256"), "private C++ provider")
    audit = inspect_elf(provider, tools["readelf"])
    if audit["soname"] != "libfenics_cxxrt.so.1":
        raise InputError(f"private provider SONAME mismatch: {audit['soname']!r}")
    if "libstdc++.so.6" in audit["needed"]:
        raise InputError("private provider must not depend on libstdc++.so.6")
    if library.get("symbol_version") != "FENICS_CXXRT_1.0":
        raise InputError("private provider manifest must require FENICS_CXXRT_1.0")
    if "FENICS_CXXRT_1.0" not in audit["private_versions"]:
        raise InputError("private provider does not define FENICS_CXXRT_1.0")
    return manifest, provider, audit


def verify_locked_prefix(lock: Path, source_explicit: Path, prefix: Path, package_cache: Path, evidence: Path) -> dict:
    repo = Path(__file__).resolve().parents[2]
    verifier = repo / "scripts" / "linux" / "package_lock.py"
    rendered = evidence.with_name("rendered-fenics-explicit.txt")
    run(
        sys.executable,
        str(verifier),
        "explicit",
        "--lock", str(lock),
        "--source-explicit", str(source_explicit),
        "--output", str(rendered),
        "--arch", "x86_64",
    )
    run(
        sys.executable,
        str(verifier),
        "verify-prefix",
        "--lock", str(lock),
        "--prefix", str(prefix),
        "--package-cache", str(package_cache),
        "--explicit", str(rendered),
        "--evidence", str(evidence),
        "--arch", "x86_64",
    )
    if not evidence.is_file():
        raise InputError("package-lock verifier did not write prefix evidence")
    return {
        "source_explicit": {"path": str(source_explicit.resolve()), "sha256": sha256(source_explicit)},
        "rendered_explicit": {"path": str(rendered), "sha256": sha256(rendered)},
        "verification": {"path": str(evidence), "sha256": sha256(evidence)},
    }


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as bundle:
        root = destination.resolve()
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise InputError(f"source archive escapes extraction root: {archive}: {member.name}")
            if member.isdev() or member.isfifo():
                raise InputError(f"source archive contains unsupported special file: {archive}: {member.name}")
        bundle.extractall(destination, filter="data")


def apply_spdlog_patch(source: Path) -> dict:
    target = source / "include" / "spdlog" / "tweakme.h"
    old = b"// #define SPDLOG_FMT_EXTERNAL\n"
    new = b"#ifndef SPDLOG_FMT_EXTERNAL\n#define SPDLOG_FMT_EXTERNAL\n#endif\n"
    data = target.read_bytes()
    if data.count(old) != 1:
        raise InputError("spdlog source does not contain the exact locked patch context")
    before = sha256(target)
    target.write_bytes(data.replace(old, new, 1))
    return {"path": str(target), "input_sha256": before, "output_sha256": sha256(target)}


def prepare_scratch(scratch: Path, source_evidence: list[dict], patch: Path, provider: Path) -> dict[str, Path]:
    if scratch.exists() and any(scratch.iterdir()):
        raise InputError(f"scratch root must be absent or empty: {scratch}")
    roots = {name: scratch / name for name in ("inputs", "src", "build", "prefix", "wheels", "audit", "evidence")}
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    for record in source_evidence:
        shutil.copy2(Path(record["path"]), roots["inputs"] / record["filename"])
    shutil.copy2(patch, roots["inputs"] / "spdlog-fix.patch")
    shutil.copy2(provider, roots["inputs"] / "libfenics_cxxrt.so.1")
    for record in source_evidence:
        safe_extract(roots["inputs"] / record["filename"], roots["src"])
    patch_application = apply_spdlog_patch(roots["src"] / "spdlog-1.17.0")
    (roots["evidence"] / "spdlog-patch-application.json").write_text(
        json.dumps(patch_application, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return roots


def prepare_default_compiler_specs(roots: dict[str, Path], tools: dict[str, Path]) -> tuple[Path, Path, dict]:
    build_tool_prefix = tools["cc"].parent.parent.resolve()
    records: dict[str, dict] = {}
    paths: dict[str, Path] = {}
    for language, tool_name in (("c", "cc"), ("cxx", "cxx")):
        text = run(tools[tool_name], "-dumpspecs").stdout
        if str(build_tool_prefix) in text:
            raise InputError(f"default {language} compiler specs unexpectedly retain build-tool prefix")
        if "*link_command:" not in text:
            raise InputError(f"default {language} compiler specs have no link_command")
        path = roots["build"] / f"{language}-default.specs"
        path.write_text(text, encoding="utf-8")
        paths[language] = path
        records[language] = {
            "path": str(path),
            "sha256": sha256(path),
            "derived_from": str(tools[tool_name]),
            "derived_from_sha256": sha256(tools[tool_name]),
        }
    evidence = {"format_version": 1, "purpose": "suppress-build-tool-prefix-rpath", "specs": records}
    evidence_path = roots["evidence"] / "compiler-default-specs.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return paths["c"], paths["cxx"], evidence


def build_candidate(
    roots: dict[str, Path],
    stock: Path,
    tools: dict[str, Path],
    python_builder: Path,
    python_builder_site: Path,
    libxml2_include: Path,
    zlib_library: Path,
    lzma_library: Path,
) -> tuple[list[Path], list[Path]]:
    prefix, src, build, wheels, audit = roots["prefix"], roots["src"], roots["build"], roots["wheels"], roots["audit"]
    lib = prefix / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    shutil.copy2(roots["inputs"] / "libfenics_cxxrt.so.1", lib / "libfenics_cxxrt.so.1")
    link_root = roots["build"] / "provider-link"
    link_root.mkdir()
    (link_root / "libstdc++.so").symlink_to(lib / "libfenics_cxxrt.so.1")
    c_specs, cxx_specs, _ = prepare_default_compiler_specs(roots, tools)

    tool_dirs = list(dict.fromkeys(str(path.parent) for path in tools.values()))
    env = os.environ.copy()
    env.update({
        "PATH": os.pathsep.join([*tool_dirs, str(stock / "bin"), "/usr/bin", "/bin"]),
        "CC": str(tools["cc"]), "CXX": str(tools["cxx"]),
        "MPICH_CC": str(tools["cc"]), "MPICH_CXX": str(tools["cxx"]),
        "PKG_CONFIG_PATH": f"{stock}/lib/pkgconfig:{stock}/share/pkgconfig",
        "LD_LIBRARY_PATH": f"{lib}:{stock}/lib",
        "PYTHONHASHSEED": "0",
        "SOURCE_DATE_EPOCH": "315532800",
    })
    common = [
        "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_C_COMPILER={tools['cc']}",
        f"-DCMAKE_CXX_COMPILER={tools['cxx']}",
        f"-DCMAKE_C_FLAGS=-specs={c_specs}",
        f"-DCMAKE_CXX_FLAGS=-specs={cxx_specs} -fvisibility-inlines-hidden",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
        "-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON",
        "-DCMAKE_INSTALL_RPATH=$ORIGIN",
        "-DCMAKE_INSTALL_RPATH_USE_LINK_PATH=FALSE",
    ]
    link_flags = f"-L{link_root} -Wl,-rpath,'$ORIGIN'"

    def cmake_build(name: str, source: Path, extra: list[str]) -> None:
        bdir = build / name
        run(tools["cmake"], *common, "-S", str(source), "-B", str(bdir), f"-DCMAKE_INSTALL_PREFIX={prefix}", *extra, env=env)
        run(tools["cmake"], "--build", str(bdir), "--parallel", "4", env=env)
        run(tools["cmake"], "--install", str(bdir), env=env)

    cmake_build("basix", src / "basix-0.11.0" / "cpp", [
        f"-DCMAKE_PREFIX_PATH={stock}", f"-DCMAKE_LIBRARY_PATH={stock}/lib",
        f"-DBLAS_LIBRARIES={stock}/lib/libblas.so;{stock}/lib/libcblas.so", f"-DLAPACK_LIBRARIES={stock}/lib/liblapack.so",
        "-DBLA_VENDOR=Generic", "-DBUILD_SHARED_LIBS=ON", f"-DCMAKE_SHARED_LINKER_FLAGS={link_flags}", f"-DCMAKE_MODULE_LINKER_FLAGS={link_flags}", f"-DCMAKE_EXE_LINKER_FLAGS={link_flags}",
    ])
    cmake_build("fmt", src / "fmt-12.1.0", ["-DCMAKE_INSTALL_LIBDIR=lib", "-DBUILD_SHARED_LIBS=ON", f"-DCMAKE_SHARED_LINKER_FLAGS={link_flags}", "-DFMT_TEST=OFF", "-DFMT_DOC=OFF", "-DFMT_INSTALL=ON"])
    cmake_build("spdlog", src / "spdlog-1.17.0", [f"-DCMAKE_PREFIX_PATH={prefix};{stock}", "-DCMAKE_INSTALL_LIBDIR=lib", "-DSPDLOG_BUILD_SHARED=ON", f"-DCMAKE_SHARED_LINKER_FLAGS={link_flags}", "-DSPDLOG_FMT_EXTERNAL=ON", "-DSPDLOG_BUILD_TESTS=OFF", "-DSPDLOG_BUILD_EXAMPLE=OFF"])
    cmake_build("pugixml", src / "pugixml-1.15", ["-DCMAKE_INSTALL_LIBDIR=lib", "-DBUILD_SHARED_LIBS=ON", f"-DCMAKE_SHARED_LINKER_FLAGS={link_flags}", "-DBUILD_TESTING=OFF"])
    cmake_build("libxml2", src / "libxml2-v2.15.4", [
        f"-DCMAKE_PREFIX_PATH={stock}", f"-DCMAKE_LIBRARY_PATH={stock}/lib",
        f"-DZLIB_INCLUDE_DIR={libxml2_include}", f"-DZLIB_LIBRARY={zlib_library}",
        f"-DLIBLZMA_INCLUDE_DIR={libxml2_include}", f"-DLIBLZMA_LIBRARY={lzma_library}",
        "-DBUILD_SHARED_LIBS=ON", "-DLIBXML2_WITH_ICU=OFF", "-DLIBXML2_WITH_PYTHON=OFF",
        "-DLIBXML2_WITH_TESTS=OFF", "-DLIBXML2_WITH_PROGRAMS=OFF", "-DLIBXML2_WITH_DOCS=OFF",
        "-DLIBXML2_WITH_LEGACY=ON", "-DLIBXML2_WITH_TLS=ON", f"-DCMAKE_SHARED_LINKER_FLAGS={link_flags}",
    ])
    cmake_build("dolfinx", src / "dolfinx-0.11.0" / "cpp", [
        f"-DCMAKE_PREFIX_PATH={prefix};{stock}", f"-DCMAKE_LIBRARY_PATH={prefix}/lib;{stock}/lib", f"-DMPI_C_COMPILER={stock}/bin/mpicc", f"-DMPI_CXX_COMPILER={stock}/bin/mpicxx",
        "-DMPI_CXX_SKIP_MPICXX=ON", "-DMPI_CXX_LIB_NAMES=mpi", "-DDOLFINX_BASIX_PYTHON=OFF", "-DDOLFINX_UFCX_PYTHON=OFF", "-DDOLFINX_ENABLE_ADIOS2=OFF", "-DDOLFINX_ENABLE_KAHIP=OFF", "-DDOLFINX_ENABLE_PETSC=ON", "-DDOLFINX_ENABLE_SLEPC=ON", "-DDOLFINX_ENABLE_SUPERLU_DIST=ON", "-DDOLFINX_ENABLE_PARMETIS=ON", "-DDOLFINX_ENABLE_SCOTCH=ON", f"-DCMAKE_SHARED_LINKER_FLAGS={link_flags}", f"-DCMAKE_MODULE_LINKER_FLAGS={link_flags}",
    ])

    wheel_rpath = r"$ORIGIN/../../../../lib"
    wheel_env = env.copy()
    wheel_env.update({
        "CMAKE_PREFIX_PATH": f"{prefix};{stock}",
        "CFLAGS": f"-specs={c_specs}",
        "CXXFLAGS": f"-specs={cxx_specs} -fvisibility-inlines-hidden",
        "PYTHONPATH": str(python_builder_site),
        "PYTHONNOUSERSITE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
    })
    wheel_env["CMAKE_ARGS"] = (
        f"-DCMAKE_CXX_COMPILER={tools['cxx']} "
        f"-DBasix_DIR={prefix}/lib/cmake/basix "
        f"-DCMAKE_MODULE_LINKER_FLAGS=-L{link_root} "
        "-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON "
        "-DCMAKE_INSTALL_RPATH_USE_LINK_PATH=FALSE "
        f"-DCMAKE_INSTALL_RPATH={wheel_rpath}"
    )
    run(python_builder, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps", "--wheel-dir", str(wheels), str(src / "basix-0.11.0" / "python"), env=wheel_env)
    basix_wheel = next(iter(sorted(wheels.glob("fenics_basix-0.11.0-*.whl"))), None)
    if basix_wheel is None:
        raise InputError("Basix wheel build produced no expected wheel")
    basix_audit = audit / "basix"
    with zipfile.ZipFile(basix_wheel) as zf:
        zf.extractall(basix_audit)
    wheel_env["PYTHONPATH"] = os.pathsep.join([
        str(python_builder_site),
        str(basix_audit),
        str(stock.joinpath(*STOCK_PYTHON_SITE.parts)),
    ])
    wheel_env["CMAKE_ARGS"] = (
        f"-DCMAKE_C_COMPILER={tools['cc']} "
        f"-DCMAKE_CXX_COMPILER={tools['cxx']} "
        f"-DDOLFINX_DIR={prefix}/lib/cmake/dolfinx "
        f"-DMPI_C_COMPILER={stock}/bin/mpicc "
        f"-DMPI_CXX_COMPILER={stock}/bin/mpicxx "
        "-DMPI_CXX_SKIP_MPICXX=ON -DMPI_CXX_LIB_NAMES=mpi "
        f"-DHDF5_C_INCLUDE_DIR={stock}/include "
        f"-DHDF5_hdf5_LIBRARY_RELEASE={stock}/lib/libhdf5.so "
        f"-DHDF5_C_COMPILER_EXECUTABLE={stock}/bin/h5pcc "
        f"-DCMAKE_MODULE_LINKER_FLAGS=-L{link_root} "
        "-DCMAKE_BUILD_RPATH_USE_ORIGIN=ON "
        "-DCMAKE_INSTALL_RPATH_USE_LINK_PATH=FALSE "
        f"-DCMAKE_INSTALL_RPATH={wheel_rpath}"
    )
    run(python_builder, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps", "--wheel-dir", str(wheels), str(src / "dolfinx-0.11.0" / "python"), env=wheel_env)
    dolfinx_wheel = next(iter(sorted(wheels.glob("fenics_dolfinx-0.11.0-*.whl"))), None)
    if dolfinx_wheel is None:
        raise InputError("DOLFINx wheel build produced no expected wheel")
    dolfinx_audit = audit / "dolfinx"
    with zipfile.ZipFile(dolfinx_wheel) as zf:
        zf.extractall(dolfinx_audit)
    return [basix_wheel, dolfinx_wheel], [basix_audit, dolfinx_audit]


def collect_outputs(roots: dict[str, Path], wheels: list[Path], audits: list[Path], tools: dict[str, Path]) -> tuple[list[dict], list[dict]]:
    prefix = roots["prefix"]
    patterns = [
        "lib/libfenics_cxxrt.so.1",
        "lib/libbasix.so",
        "lib/libfmt.so.*",
        "lib/libspdlog.so.*",
        "lib/libpugixml.so.*",
        "lib/libxml2.so.*",
        "lib/libdolfinx.so.0.11.0",
    ]
    elfs: list[Path] = []
    for pattern in patterns:
        matches = [p for p in prefix.glob(pattern) if p.is_file() and not p.is_symlink()]
        if not matches:
            raise InputError(f"expected rebuilt ELF output is missing: {pattern}")
        elfs.append(sorted(matches)[-1])
    for root in audits:
        candidates = sorted(p for p in root.rglob("*.so") if p.is_file())
        if not candidates:
            raise InputError(f"rebuilt wheel contains no extension module: {root}")
        elfs.extend(candidates)
    elf_records = [inspect_elf(path, tools["readelf"]) for path in dict.fromkeys(elfs)]
    for record in elf_records:
        if record["path"].endswith("libfenics_cxxrt.so.1"):
            continue
        if any(token.startswith(("GLIBCXX_", "CXXABI_")) for token in record["required_versions"]):
            raise InputError(f"rebuilt output retains host C++ runtime symbol versions: {record['path']}")
    wheel_records = [{"path": str(path), "sha256": sha256(path), "size": path.stat().st_size} for path in wheels]
    return elf_records, wheel_records


def candidate_output_binding(
    roots: dict[str, Path],
    replacements: dict[str, dict],
    elf_records: list[dict],
    wheel_records: list[dict],
) -> dict:
    def logical_path(raw: object, root_names: tuple[str, ...], label: str) -> str:
        if not isinstance(raw, str) or not raw:
            raise InputError(f"{label} path is missing")
        candidate = Path(raw).resolve()
        for root_name in root_names:
            root = roots[root_name].resolve()
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            return (PurePosixPath(root_name) / PurePosixPath(relative.as_posix())).as_posix()
        raise InputError(f"{label} path is outside the candidate roots: {candidate}")

    replacement_records = [
        {
            "destination": key,
            "owner": str(replacements[key]["owner"]),
            "sha256": require_sha(replacements[key].get("sha256"), f"candidate replacement {key} sha256"),
            "bytes": replacements[key].get("bytes"),
        }
        for key in sorted(replacements)
    ]
    elf = sorted(
        (
            {
                "path": logical_path(record.get("path"), ("prefix", "audit"), "candidate ELF"),
                "sha256": require_sha(record.get("sha256"), "candidate ELF sha256"),
            }
            for record in elf_records
        ),
        key=lambda record: record["path"],
    )
    wheels = sorted(
        (
            {
                "path": logical_path(record.get("path"), ("wheels",), "candidate wheel"),
                "sha256": require_sha(record.get("sha256"), "candidate wheel sha256"),
                "bytes": record.get("size"),
            }
            for record in wheel_records
        ),
        key=lambda record: record["path"],
    )
    return normalize_candidate_output_binding(
        {"replacements": replacement_records, "elf": elf, "wheels": wheels}
    )


def verify_native_closure_candidate_binding(native_closure: dict, actual_binding: dict) -> str:
    expected = normalize_candidate_output_binding(
        native_closure.get("candidate_output_binding"), "native closure candidate output binding"
    )
    expected_sha = require_sha(
        native_closure.get("candidate_output_binding_sha256"), "native closure candidate output binding sha256"
    )
    if canonical_json_sha(expected) != expected_sha:
        raise InputError("native closure candidate output binding SHA-256 mismatch")
    actual = normalize_candidate_output_binding(actual_binding, "actual candidate output binding")
    actual_sha = canonical_json_sha(actual)
    if actual != expected or actual_sha != expected_sha:
        raise InputError(
            "native closure proof does not match exact candidate outputs: "
            f"actual={actual_sha} expected={expected_sha}"
        )
    return actual_sha


def installed_package_records(prefix: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for path in sorted((prefix / "conda-meta").glob("*.json")):
        record = load_json(path)
        name = record.get("name")
        if not isinstance(name, str) or not name or name in records:
            raise InputError(f"ambiguous installed package metadata while writing handoff: {path}")
        records[name] = record
    return records


def installed_owned_paths(record: dict) -> set[str]:
    paths_data = record.get("paths_data")
    raw_paths = paths_data.get("paths") if isinstance(paths_data, dict) else None
    if isinstance(raw_paths, list) and raw_paths:
        values = [item.get("_path") if isinstance(item, dict) else None for item in raw_paths]
    else:
        files = record.get("files")
        if not isinstance(files, list):
            raise InputError(f"installed package has no owned path inventory: {record.get('name')}")
        values = files
    result: set[str] = set()
    for raw in values:
        relative = safe_handoff_relative(raw, f"installed path for {record.get('name')}")
        text = relative.as_posix()
        if text in result:
            raise InputError(f"installed package repeats owned path: {record.get('name')}: {text}")
        result.add(text)
    return result


def handoff_runtime_prefix_path(relative: PurePosixPath) -> bool:
    text = relative.as_posix()
    if text.startswith("include/"):
        return True
    return re.fullmatch(r"lib/[^/]+\.so(?:\..*)?", text) is not None


def ignored_build_install_path(relative: PurePosixPath) -> bool:
    text = relative.as_posix()
    return text.startswith(("lib/cmake/", "lib/pkgconfig/", "share/")) or relative.suffix == ".a"


def collect_install_owners(roots: dict[str, Path]) -> dict[str, str]:
    """Recover candidate ownership after the native build without changing its API."""

    prefix = roots["prefix"].resolve()
    owners: dict[str, str] = {"lib/libfenics_cxxrt.so.1": HANDOFF_PROVIDER_OWNER}
    for component, owner in REBUILD_COMPONENT_OWNERS.items():
        manifest = roots["build"] / component / "install_manifest.txt"
        if not manifest.is_file() or manifest.is_symlink():
            raise InputError(f"CMake install manifest is missing or unsafe for {component}: {manifest}")
        entries = manifest.read_text(encoding="utf-8").splitlines()
        if not entries:
            raise InputError(f"CMake install manifest is empty for {component}: {manifest}")
        for raw in entries:
            installed = Path(raw)
            if not installed.is_absolute():
                raise InputError(f"CMake install path is not absolute for {component}: {installed}")
            try:
                relative = installed.resolve(strict=False).relative_to(prefix).as_posix()
            except ValueError as exc:
                raise InputError(f"CMake install path escapes rebuild prefix for {component}: {installed}") from exc
            previous = owners.get(relative)
            if previous is not None and previous != owner:
                raise InputError(f"rebuild install ownership collision: {relative}: {previous} vs {owner}")
            owners[relative] = owner
    return owners


def collect_candidate_replacements(
    roots: dict[str, Path], locked_prefix: Path, wheels: list[Path]
) -> dict[str, dict]:
    """Collect exact candidate bytes that are eligible for package handoff."""

    installed = installed_package_records(locked_prefix)
    expected_owners = set(REBUILD_COMPONENT_OWNERS.values()) | set(WHEEL_OWNERS.values())
    owned_by_package = {
        name: installed_owned_paths(installed[name])
        for name in expected_owners
        if name in installed
    }
    if set(owned_by_package) != expected_owners:
        missing = sorted(expected_owners - set(owned_by_package))
        raise InputError(f"locked prefix is missing rebuilt package ownership metadata: {missing}")
    install_owners = collect_install_owners(roots)
    replacements: dict[str, dict] = {}

    def add_candidate(
        destination_relative: PurePosixPath,
        owner: str,
        data: bytes,
        mode: int,
        *,
        cp314_wheel_payload: bool = False,
    ) -> None:
        text = destination_relative.as_posix()
        if text in replacements:
            raise InputError(f"native rebuild handoff has duplicate replacement destination: {text}")
        if owner == HANDOFF_PROVIDER_OWNER:
            if text != "lib/libfenics_cxxrt.so.1":
                raise InputError(f"private provider has unexpected handoff destination: {text}")
            replaces_sha = None
        elif cp314_wheel_payload:
            roots_for_owner = WHEEL_MEMBER_ROOTS.get(owner)
            if roots_for_owner is None:
                raise InputError(f"cp314 wheel payload has no fixed owner roots: {owner}")
            try:
                target_member = destination_relative.relative_to(TARGET_PYTHON_SITE)
            except ValueError as exc:
                raise InputError(f"cp314 wheel payload is outside the cp314 site-packages root: {text}") from exc
            if not target_member.parts or target_member.parts[0] not in roots_for_owner:
                raise InputError(f"cp314 wheel payload escapes the fixed package roots for {owner}: {text}")
            stock_paths = owned_by_package.get(owner, set())
            if not any(
                PurePosixPath(stock_path).is_relative_to(STOCK_PYTHON_SITE)
                and PurePosixPath(stock_path).relative_to(STOCK_PYTHON_SITE).parts
                and PurePosixPath(stock_path).relative_to(STOCK_PYTHON_SITE).parts[0] in roots_for_owner
                for stock_path in stock_paths
            ):
                raise InputError(f"cp314 wheel payload owner has no bound cp312 stock Python payload: {owner}")
            replaces_sha = None
        else:
            if owner not in owned_by_package or text not in owned_by_package[owner]:
                raise InputError(f"candidate replacement is not owned by locked package {owner}: {text}")
            stock = locked_prefix / Path(*destination_relative.parts)
            if not stock.is_file() or stock.is_symlink():
                raise InputError(f"candidate replacement stock destination is not a regular file: {owner}: {text}")
            replaces_sha = sha256(stock)
        replacements[text] = {
            "destination": text,
            "owner": owner,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "mode": mode & 0o777,
            "replaces_sha256": replaces_sha,
            "data": data,
        }

    for raw_relative, owner in sorted(install_owners.items()):
        relative = safe_handoff_relative(raw_relative, "candidate install destination")
        source = roots["prefix"] / Path(*relative.parts)
        if source.is_symlink():
            stock = locked_prefix / Path(*relative.parts)
            if owner != HANDOFF_PROVIDER_OWNER and stock.is_symlink():
                continue
            raise InputError(f"candidate handoff does not accept symlink replacements: {relative}")
        if not source.is_file():
            raise InputError(f"candidate install manifest path is not a regular file: {relative}")
        if owner != HANDOFF_PROVIDER_OWNER and not handoff_runtime_prefix_path(relative):
            if ignored_build_install_path(relative):
                continue
            raise InputError(f"candidate install path is outside the runtime handoff boundary: {relative}")
        add_candidate(relative, owner, source.read_bytes(), stat.S_IMODE(source.stat().st_mode))

    for wheel in sorted(wheels, key=lambda path: path.name):
        wheel_owner = next((owner for stem, owner in WHEEL_OWNERS.items() if wheel.name.startswith(stem + "-")), None)
        if wheel_owner is None:
            raise InputError(f"candidate wheel has no fixed handoff owner: {wheel.name}")
        with zipfile.ZipFile(wheel) as bundle:
            for info in sorted(bundle.infolist(), key=lambda item: item.filename):
                if info.is_dir():
                    continue
                member = safe_handoff_relative(info.filename, f"wheel member in {wheel.name}")
                zip_mode = (info.external_attr >> 16) & 0o177777
                if stat.S_ISLNK(zip_mode):
                    raise InputError(f"candidate wheel contains a symlink: {wheel.name}: {member}")
                destination_relative = TARGET_PYTHON_SITE / member
                mode = zip_mode & 0o777 or 0o644
                add_candidate(
                    destination_relative,
                    wheel_owner,
                    bundle.read(info),
                    mode,
                    cp314_wheel_payload=True,
                )

    if not replacements:
        raise InputError("native rebuild produced no package handoff replacements")
    return replacements


def regular_file_identity(path: Path, label: str) -> dict:
    if not path.is_file() or path.is_symlink():
        raise InputError(f"{label} is missing or is not a regular file: {path}")
    return {"path": str(path.resolve()), "sha256": sha256(path)}


def write_candidate_build_handoff(
    destination: Path,
    *,
    roots: dict[str, Path],
    locked_prefix: Path,
    package_lock: Path,
    prefix_verification: dict,
    stock_abi_preflight: dict,
    source_manifest: Path,
    source_evidence: list[dict],
    build_tool_explicit: Path,
    python_builder_manifest: Path,
    libxml2_build_deps_manifest: Path,
    private_provider_manifest: Path,
    wheels: list[Path],
    elf_records: list[dict],
    wheel_records: list[dict],
) -> dict:
    """Publish the exact built candidate for an independent closure audit."""

    if destination.exists() or destination.is_symlink():
        raise InputError(f"candidate build handoff destination already exists: {destination}")
    candidate_root = roots["evidence"].parent.resolve()
    if destination.resolve(strict=False) != roots["evidence"].resolve() / "native-build-handoff.json":
        raise InputError("candidate build handoff must be written to evidence/native-build-handoff.json")
    locked_prefix = locked_prefix.resolve()
    if not locked_prefix.is_dir() or locked_prefix.is_symlink():
        raise InputError(f"locked prefix is missing or unsafe: {locked_prefix}")

    replacements = collect_candidate_replacements(roots, locked_prefix, wheels)
    output_binding = candidate_output_binding(roots, replacements, elf_records, wheel_records)
    output_binding_sha = canonical_json_sha(output_binding)
    prefix_verification_path = Path(str(prefix_verification.get("verification", {}).get("path", "")))
    prefix_verification_sha = require_sha(
        prefix_verification.get("verification", {}).get("sha256"), "prefix verification sha256"
    )
    if regular_file_identity(prefix_verification_path, "prefix verification")["sha256"] != prefix_verification_sha:
        raise InputError("prefix verification evidence changed before candidate handoff")
    stock_preflight_path = Path(str(stock_abi_preflight.get("path", "")))
    stock_preflight_sha = require_sha(stock_abi_preflight.get("sha256"), "stock ABI preflight sha256")
    if regular_file_identity(stock_preflight_path, "stock ABI preflight")["sha256"] != stock_preflight_sha:
        raise InputError("stock ABI preflight evidence changed before candidate handoff")

    source_hashes_unsorted = {
        str(record["component"]): require_sha(record.get("sha256"), f"source {record.get('component')} sha256")
        for record in source_evidence
    }
    if set(source_hashes_unsorted) != set(REQUIRED_SOURCES):
        raise InputError("candidate build source evidence differs from the fixed source set")
    source_hashes = {name: source_hashes_unsorted[name] for name in sorted(REQUIRED_SOURCES)}

    payload = {
        "format_version": CANDIDATE_BUILD_HANDOFF_FORMAT_VERSION,
        "kind": CANDIDATE_BUILD_HANDOFF_KIND,
        "status": CANDIDATE_BUILD_HANDOFF_STATUS,
        "source_rebuild_provenance_eligible": False,
        "candidate_root": str(candidate_root),
        "inputs": {
            "package_lock": regular_file_identity(package_lock, "package lock"),
            "locked_prefix": {"path": str(locked_prefix)},
            "prefix_verification": regular_file_identity(prefix_verification_path, "prefix verification"),
            "stock_abi_preflight": {
                **regular_file_identity(stock_preflight_path, "stock ABI preflight"),
                "selection_fingerprint_sha256": require_sha(
                    stock_abi_preflight.get("selection_fingerprint_sha256"), "stock ABI selection fingerprint"
                ),
            },
            "source_manifest": regular_file_identity(source_manifest, "source manifest"),
            "source_sha256": source_hashes,
            "build_tool_explicit": regular_file_identity(build_tool_explicit, "build-tool explicit lock"),
            "python_builder_manifest": regular_file_identity(python_builder_manifest, "Python builder manifest"),
            "libxml2_build_deps_manifest": regular_file_identity(
                libxml2_build_deps_manifest, "libxml2 build-deps manifest"
            ),
            "private_provider_manifest": regular_file_identity(
                private_provider_manifest, "private-provider manifest"
            ),
        },
        "candidate_output_binding": output_binding,
        "candidate_output_binding_sha256": output_binding_sha,
        "qualification": {
            "full_native_closure": "not-run",
            "hosted_linux_glibc217": "not-run",
            "jit": "not-run",
            "poisson": "not-run",
            "relocation": "not-run",
        },
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def validate_candidate_build_handoff(path: Path) -> dict:
    """Re-hash a built candidate before accepting post-build closure evidence."""

    payload = load_json(path)
    expected_top = {
        "format_version",
        "kind",
        "status",
        "source_rebuild_provenance_eligible",
        "candidate_root",
        "inputs",
        "candidate_output_binding",
        "candidate_output_binding_sha256",
        "qualification",
    }
    if set(payload) != expected_top:
        raise InputError("candidate build handoff has unexpected or missing top-level fields")
    if (
        payload.get("format_version") != CANDIDATE_BUILD_HANDOFF_FORMAT_VERSION
        or payload.get("kind") != CANDIDATE_BUILD_HANDOFF_KIND
        or payload.get("status") != CANDIDATE_BUILD_HANDOFF_STATUS
        or payload.get("source_rebuild_provenance_eligible") is not False
    ):
        raise InputError("unsupported candidate build handoff identity")
    if payload.get("qualification") != {
        "full_native_closure": "not-run",
        "hosted_linux_glibc217": "not-run",
        "jit": "not-run",
        "poisson": "not-run",
        "relocation": "not-run",
    }:
        raise InputError("candidate build handoff must remain explicitly unqualified")

    candidate_root_raw = payload.get("candidate_root")
    if not isinstance(candidate_root_raw, str) or not candidate_root_raw:
        raise InputError("candidate build handoff has no candidate root")
    candidate_root = Path(candidate_root_raw).resolve()
    expected_handoff = candidate_root / "evidence" / "native-build-handoff.json"
    if path.resolve() != expected_handoff:
        raise InputError("candidate build handoff path is not bound to its candidate root")
    roots = {
        name: candidate_root / name
        for name in ("inputs", "src", "build", "prefix", "wheels", "audit", "evidence")
    }
    for name in ("build", "prefix", "wheels", "audit", "evidence"):
        if not roots[name].is_dir() or roots[name].is_symlink():
            raise InputError(f"candidate build root is missing or unsafe: {name}: {roots[name]}")

    inputs = payload.get("inputs")
    expected_inputs = {
        "package_lock",
        "locked_prefix",
        "prefix_verification",
        "stock_abi_preflight",
        "source_manifest",
        "source_sha256",
        "build_tool_explicit",
        "python_builder_manifest",
        "libxml2_build_deps_manifest",
        "private_provider_manifest",
    }
    if not isinstance(inputs, dict) or set(inputs) != expected_inputs:
        raise InputError("candidate build handoff has unexpected or missing inputs")

    def checked_input(name: str) -> Path:
        record = inputs.get(name)
        if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
            raise InputError(f"candidate build input identity is invalid: {name}")
        raw = record.get("path")
        if not isinstance(raw, str) or not raw:
            raise InputError(f"candidate build input path is invalid: {name}")
        candidate = Path(raw)
        actual = verify_file(candidate, record.get("sha256"), f"candidate build input {name}")
        return Path(actual["path"])

    package_lock = checked_input("package_lock")
    prefix_verification_path = checked_input("prefix_verification")
    source_manifest = checked_input("source_manifest")
    build_tool_explicit = checked_input("build_tool_explicit")
    python_builder_manifest = checked_input("python_builder_manifest")
    libxml2_build_deps_manifest = checked_input("libxml2_build_deps_manifest")
    private_provider_manifest = checked_input("private_provider_manifest")

    locked_prefix_record = inputs.get("locked_prefix")
    if not isinstance(locked_prefix_record, dict) or set(locked_prefix_record) != {"path"}:
        raise InputError("candidate build locked-prefix identity is invalid")
    locked_prefix = Path(str(locked_prefix_record.get("path", ""))).resolve()
    if not locked_prefix.is_dir() or locked_prefix.is_symlink():
        raise InputError(f"candidate build locked prefix is missing or unsafe: {locked_prefix}")

    stock_preflight = inputs.get("stock_abi_preflight")
    if not isinstance(stock_preflight, dict) or set(stock_preflight) != {
        "path", "sha256", "selection_fingerprint_sha256"
    }:
        raise InputError("candidate build stock ABI preflight identity is invalid")
    stock_preflight_path = Path(str(stock_preflight.get("path", "")))
    stock_preflight_identity = verify_file(
        stock_preflight_path, stock_preflight.get("sha256"), "candidate build stock ABI preflight"
    )
    stock_selection_fingerprint = require_sha(
        stock_preflight.get("selection_fingerprint_sha256"), "candidate build stock ABI selection fingerprint"
    )
    verified_preflight = validate_stock_abi_preflight(stock_preflight_path, package_lock)
    if verified_preflight["selection_fingerprint_sha256"] != stock_selection_fingerprint:
        raise InputError("candidate build stock ABI selection fingerprint differs from referenced evidence")
    stock_preflight_payload = load_json(stock_preflight_path)
    if stock_preflight_payload.get("prefix_verification_sha256") != sha256(prefix_verification_path):
        raise InputError("candidate build stock ABI preflight does not bind the referenced prefix verification")

    source_hashes = inputs.get("source_sha256")
    if not isinstance(source_hashes, dict) or set(source_hashes) != set(REQUIRED_SOURCES):
        raise InputError("candidate build source hash set differs from the fixed source set")
    normalized_source_hashes = {
        name: require_sha(source_hashes.get(name), f"candidate build source {name} sha256")
        for name in sorted(REQUIRED_SOURCES)
    }
    source_manifest_payload = load_json(source_manifest)
    manifest_sources = source_manifest_payload.get("sources")
    if not isinstance(manifest_sources, dict):
        raise InputError("candidate build source manifest has no sources object")
    for name, digest in normalized_source_hashes.items():
        record = manifest_sources.get(name)
        if not isinstance(record, dict) or require_sha(
            record.get("sha256"), f"candidate build source manifest {name} sha256"
        ) != digest:
            raise InputError(f"candidate build source hash mismatch for {name}")

    expected_binding = normalize_candidate_output_binding(
        payload.get("candidate_output_binding"), "candidate build output binding"
    )
    expected_binding_sha = require_sha(
        payload.get("candidate_output_binding_sha256"), "candidate build output binding sha256"
    )
    if canonical_json_sha(expected_binding) != expected_binding_sha:
        raise InputError("candidate build output binding SHA-256 mismatch")

    def binding_file(logical: str, allowed_roots: tuple[str, ...], label: str) -> Path:
        relative = safe_handoff_relative(logical, label)
        if len(relative.parts) < 2 or relative.parts[0] not in allowed_roots:
            raise InputError(f"{label} is outside the candidate roots: {logical}")
        root_name = relative.parts[0]
        candidate = roots[root_name].joinpath(*relative.parts[1:])
        return candidate

    elf_records = []
    for record in expected_binding["elf"]:
        candidate = binding_file(record["path"], ("prefix", "audit"), "candidate build ELF path")
        verify_file(candidate, record["sha256"], "candidate build ELF")
        elf_records.append({"path": str(candidate.resolve()), "sha256": record["sha256"]})
    wheels = []
    wheel_records = []
    for record in expected_binding["wheels"]:
        candidate = binding_file(record["path"], ("wheels",), "candidate build wheel path")
        evidence = verify_file(candidate, record["sha256"], "candidate build wheel")
        if evidence["size"] != record["bytes"]:
            raise InputError(f"candidate build wheel byte count changed: {record['path']}")
        wheels.append(candidate.resolve())
        wheel_records.append({"path": str(candidate.resolve()), "sha256": record["sha256"], "size": record["bytes"]})

    replacements = collect_candidate_replacements(roots, locked_prefix, wheels)
    actual_binding = candidate_output_binding(roots, replacements, elf_records, wheel_records)
    actual_binding_sha = canonical_json_sha(actual_binding)
    if actual_binding != expected_binding or actual_binding_sha != expected_binding_sha:
        raise InputError(
            "candidate build handoff does not match exact live candidate outputs: "
            f"actual={actual_binding_sha} expected={expected_binding_sha}"
        )

    return {
        "payload": payload,
        "sha256": sha256(path),
        "roots": roots,
        "locked_prefix": locked_prefix,
        "package_lock": package_lock,
        "prefix_verification": {
            "verification": {
                "path": str(prefix_verification_path),
                "sha256": sha256(prefix_verification_path),
            }
        },
        "stock_abi_preflight": {
            "path": str(stock_preflight_path.resolve()),
            "sha256": stock_preflight_identity["sha256"],
            "selection_fingerprint_sha256": stock_selection_fingerprint,
        },
        "source_manifest": source_manifest,
        "source_evidence": [
            {"component": name, "sha256": normalized_source_hashes[name]}
            for name in sorted(REQUIRED_SOURCES)
        ],
        "build_tool_explicit": build_tool_explicit,
        "python_builder_manifest": python_builder_manifest,
        "libxml2_build_deps_manifest": libxml2_build_deps_manifest,
        "private_provider_manifest": private_provider_manifest,
        "wheels": wheels,
        "elf_records": elf_records,
        "wheel_records": wheel_records,
        "candidate_output_binding": actual_binding,
        "candidate_output_binding_sha256": actual_binding_sha,
    }


def write_package_handoff(
    destination: Path,
    *,
    roots: dict[str, Path],
    locked_prefix: Path,
    package_lock: Path,
    prefix_verification: dict,
    stock_abi_preflight: dict,
    source_manifest: Path,
    source_evidence: list[dict],
    build_tool_explicit: Path,
    python_builder_manifest: Path,
    libxml2_build_deps_manifest: Path,
    private_provider_manifest: Path,
    native_closure: dict,
    wheels: list[Path],
    elf_records: list[dict],
    wheel_records: list[dict],
) -> dict:
    """Write a deterministic, unqualified packaging handoff for candidate bytes."""

    if destination.exists():
        raise InputError(f"native rebuild handoff destination already exists: {destination}")
    handoff_root = destination.parent
    payload_root = handoff_root / "payload"
    payload_root.mkdir(parents=True, exist_ok=False)
    candidate_replacements = collect_candidate_replacements(roots, locked_prefix, wheels)
    replacements: dict[str, dict] = {}
    for text, candidate in sorted(candidate_replacements.items()):
        destination_relative = safe_handoff_relative(text, "candidate replacement destination")
        payload_relative = PurePosixPath("payload") / destination_relative
        payload = handoff_root / Path(*payload_relative.parts)
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(candidate["data"])
        payload.chmod(int(candidate["mode"]))
        replacements[text] = {
            "destination": text,
            "payload": payload_relative.as_posix(),
            "owner": candidate["owner"],
            "sha256": sha256(payload),
            "bytes": payload.stat().st_size,
            "mode": int(candidate["mode"]),
            "replaces_sha256": candidate["replaces_sha256"],
        }

    output_binding = candidate_output_binding(roots, replacements, elf_records, wheel_records)
    output_binding_sha = verify_native_closure_candidate_binding(native_closure, output_binding)
    native_closure_sha = require_sha(native_closure.get("sha256"), "native closure report sha256")
    omissions = []
    conditional = native_closure.get("conditional_omissions")
    if not isinstance(conditional, dict) or tuple(conditional) != HANDOFF_OMISSION_PACKAGES:
        raise InputError("native closure evidence does not contain the fixed conditional omission set")
    for name in HANDOFF_OMISSION_PACKAGES:
        evidence = conditional[name].get("evidence")
        evidence_sha = require_sha(conditional[name].get("evidence_sha256"), f"{name} omission evidence sha256")
        if canonical_json_sha(evidence) != evidence_sha:
            raise InputError(f"conditional omission evidence hash mismatch for {name}")
        omissions.append({
            "package": name,
            "native_closure_report_sha256": native_closure_sha,
            "candidate_output_binding_sha256": output_binding_sha,
            "evidence": evidence,
            "evidence_sha256": evidence_sha,
        })

    inputs = {
        "package_lock_sha256": sha256(package_lock),
        "prefix_verification_sha256": require_sha(
            prefix_verification.get("verification", {}).get("sha256"), "prefix verification sha256"
        ),
        "stock_abi_preflight_sha256": require_sha(stock_abi_preflight.get("sha256"), "stock ABI preflight sha256"),
        "stock_abi_selection_fingerprint_sha256": require_sha(
            stock_abi_preflight.get("selection_fingerprint_sha256"), "stock ABI selection fingerprint"
        ),
        "source_manifest_sha256": sha256(source_manifest),
        "source_sha256": {
            str(record["component"]): require_sha(record.get("sha256"), f"source {record.get('component')} sha256")
            for record in sorted(source_evidence, key=lambda item: str(item["component"]))
        },
        "build_tool_explicit_sha256": sha256(build_tool_explicit),
        "python_builder_manifest_sha256": sha256(python_builder_manifest),
        "libxml2_build_deps_manifest_sha256": sha256(libxml2_build_deps_manifest),
        "private_provider_manifest_sha256": sha256(private_provider_manifest),
        "private_provider_library_sha256": replacements["lib/libfenics_cxxrt.so.1"]["sha256"],
        "native_closure_report_sha256": native_closure_sha,
        "native_closure": {
            "dolfinx_build_flags": native_closure.get("dolfinx_build_flags"),
            "closure_elf_count": native_closure.get("closure_elf_count"),
            "icu_reached_paths": native_closure.get("icu_reached_paths"),
            "icu_resolution_required": native_closure.get("icu_resolution_required"),
            "candidate_output_binding_sha256": output_binding_sha,
        },
    }
    payload = {
        "format_version": HANDOFF_FORMAT_VERSION,
        "kind": HANDOFF_KIND,
        "status": HANDOFF_STATUS,
        "source_rebuild_provenance_eligible": False,
        "inputs": inputs,
        "conditional_omissions": omissions,
        "replacements": [replacements[key] for key in sorted(replacements)],
        "qualification": {
            "full_native_closure": "not-run",
            "hosted_linux_glibc217": "not-run",
            "jit": "not-run",
            "poisson": "not-run",
            "relocation": "not-run",
        },
    }
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def finalize_candidate_build_handoff(
    candidate_handoff_path: Path,
    native_closure_report: Path,
    destination: Path | None = None,
) -> tuple[Path, dict]:
    """Finalize a previously built candidate after an independent closure audit."""

    candidate = validate_candidate_build_handoff(candidate_handoff_path)
    native_closure = validate_native_closure_report(native_closure_report)
    verify_native_closure_candidate_binding(native_closure, candidate["candidate_output_binding"])
    if destination is None:
        destination = candidate["roots"]["evidence"] / "package-handoff" / "native-rebuild-handoff.json"
    payload = write_package_handoff(
        destination,
        roots=candidate["roots"],
        locked_prefix=candidate["locked_prefix"],
        package_lock=candidate["package_lock"],
        prefix_verification=candidate["prefix_verification"],
        stock_abi_preflight=candidate["stock_abi_preflight"],
        source_manifest=candidate["source_manifest"],
        source_evidence=candidate["source_evidence"],
        build_tool_explicit=candidate["build_tool_explicit"],
        python_builder_manifest=candidate["python_builder_manifest"],
        libxml2_build_deps_manifest=candidate["libxml2_build_deps_manifest"],
        private_provider_manifest=candidate["private_provider_manifest"],
        native_closure=native_closure,
        wheels=candidate["wheels"],
        elf_records=candidate["elf_records"],
        wheel_records=candidate["wheel_records"],
    )
    return destination, payload


def write_provenance(path: Path, *, inputs: dict, outputs: dict, executed: bool) -> None:
    payload = {
        "format_version": 1,
        "kind": "linux-fenics-selective-native-rebuild-candidate",
        "status": "candidate-unqualified" if executed else "validated-input-skeleton",
        "source_rebuild_provenance_eligible": False,
        "producer_claim": "No source_rebuilds claim is valid until a complete rebuild and hosted qualification pass.",
        "inputs": inputs,
        "outputs": outputs,
        "qualification": {"hosted_linux_glibc217": "not-run", "poisson": "not-run"},
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--package-lock", type=Path, required=True)
    parser.add_argument("--source-explicit", type=Path, required=True)
    parser.add_argument("--package-cache", type=Path, required=True)
    parser.add_argument("--locked-prefix", type=Path, required=True)
    parser.add_argument("--build-tool-explicit", type=Path, required=True)
    parser.add_argument("--build-tool-prefix", type=Path, required=True)
    parser.add_argument("--build-tool-package-cache", type=Path, required=True)
    parser.add_argument("--python-builder-manifest", type=Path)
    parser.add_argument("--libxml2-build-deps-manifest", type=Path)
    parser.add_argument("--private-provider-manifest", type=Path)
    parser.add_argument("--native-closure-report", type=Path)
    parser.add_argument("--stock-abi-preflight", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument(
        "--validate-public-inputs-only",
        action="store_true",
        help="validate the pinned public rebuild inputs without requiring experimental provider/runtime evidence",
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    try:
        source_manifest, source_evidence = validate_sources(args.source_manifest, args.source_dir)
        package_evidence = validate_package_closure(args.package_lock, args.package_cache, source_manifest)
        recipe_bindings, patch_path, patch_evidence = validate_recipe_bindings(source_manifest, args.package_lock, args.package_cache)
        stock_abi_evidence = validate_stock_abi_preflight(args.stock_abi_preflight, args.package_lock)
        if args.validate_public_inputs_only and args.execute:
            raise InputError("--validate-public-inputs-only cannot be combined with --execute")
        if not args.locked_prefix.is_dir():
            raise InputError(f"locked FEniCS prefix is missing: {args.locked_prefix}")
        if not args.build_tool_prefix.is_dir():
            raise InputError(f"frozen build-tool prefix is missing: {args.build_tool_prefix}")
        if args.scratch_root.exists() and any(args.scratch_root.iterdir()):
            raise InputError(f"scratch root must be absent or empty: {args.scratch_root}")
        (args.scratch_root / "evidence").mkdir(parents=True, exist_ok=True)
        build_tool_evidence, tools = verify_build_toolchain(
            args.build_tool_explicit,
            args.build_tool_prefix,
            args.build_tool_package_cache,
            args.scratch_root / "evidence" / "build-tool-verification.json",
        )
        python_builder_evidence: dict | None = None
        python_builder: Path | None = None
        python_builder_site: Path | None = None
        if args.python_builder_manifest is not None:
            python_builder_evidence, python_builder, python_builder_site = validate_python_builder(
                args.python_builder_manifest
            )
        libxml2_build_evidence: dict | None = None
        libxml2_include: Path | None = None
        zlib_library: Path | None = None
        lzma_library: Path | None = None
        if args.libxml2_build_deps_manifest is not None:
            libxml2_build_evidence, libxml2_include, zlib_library, lzma_library = validate_libxml2_build_deps(
                args.libxml2_build_deps_manifest,
                args.package_lock,
            )
        if args.validate_public_inputs_only:
            if args.python_builder_manifest is None or python_builder_evidence is None:
                raise InputError("--validate-public-inputs-only requires --python-builder-manifest")
            if args.libxml2_build_deps_manifest is None or libxml2_build_evidence is None:
                raise InputError("--validate-public-inputs-only requires --libxml2-build-deps-manifest")
            prefix_evidence = verify_locked_prefix(
                args.package_lock,
                args.source_explicit,
                args.locked_prefix,
                args.package_cache,
                args.scratch_root / "evidence" / "locked-prefix-verification.json",
            )
            build_tool_verification = args.scratch_root / "evidence" / "build-tool-verification.json"
            provenance = args.scratch_root / "evidence" / "native-rebuild-provenance.json"
            write_provenance(
                provenance,
                inputs={
                    "source_manifest": {"path": str(args.source_manifest.resolve()), "sha256": sha256(args.source_manifest)},
                    "package_lock": {"path": str(args.package_lock.resolve()), "sha256": sha256(args.package_lock)},
                    "build_toolchain": {
                        "explicit": {"path": str(args.build_tool_explicit.resolve()), "sha256": sha256(args.build_tool_explicit)},
                        "verification": {"path": str(build_tool_verification), "sha256": sha256(build_tool_verification)},
                        "identity": build_tool_evidence.get("roots"),
                        "executables": build_tool_evidence.get("executables"),
                    },
                    "python_builder_manifest": {
                        "path": str(args.python_builder_manifest.resolve()),
                        "sha256": sha256(args.python_builder_manifest),
                        "identity": python_builder_evidence,
                    },
                    "libxml2_build_deps_manifest": {
                        "path": str(args.libxml2_build_deps_manifest.resolve()),
                        "sha256": sha256(args.libxml2_build_deps_manifest),
                        "identity": libxml2_build_evidence,
                    },
                    "sources": source_evidence,
                    "recipe_source_bindings": recipe_bindings,
                    "spdlog_patch": patch_evidence,
                    "locked_packages": package_evidence,
                    "locked_prefix": {"path": str(args.locked_prefix.resolve()), "verification": prefix_evidence},
                    "stock_abi_preflight": stock_abi_evidence,
                    "deferred_full_rebuild_inputs": [
                        "private_provider_manifest",
                        "native_closure_report",
                    ],
                },
                outputs={"elf": [], "wheels": [], "public_input_validation_only": True},
                executed=False,
            )
            print("rebuild_mode=validate-public-inputs-only")
            print(f"provenance={provenance}")
            print("source_rebuild_provenance_eligible=false")
            return 0

        required_external = {
            "--python-builder-manifest": args.python_builder_manifest,
            "--libxml2-build-deps-manifest": args.libxml2_build_deps_manifest,
            "--private-provider-manifest": args.private_provider_manifest,
        }
        missing_external = [flag for flag, value in required_external.items() if value is None]
        if missing_external:
            raise InputError("full native rebuild requires: " + ", ".join(missing_external))
        assert args.python_builder_manifest is not None
        assert args.libxml2_build_deps_manifest is not None
        assert args.private_provider_manifest is not None
        assert python_builder_evidence is not None
        assert python_builder is not None
        assert python_builder_site is not None
        assert libxml2_build_evidence is not None
        assert libxml2_include is not None
        assert zlib_library is not None
        assert lzma_library is not None
        closure_evidence = (
            validate_native_closure_report(args.native_closure_report)
            if args.native_closure_report is not None
            else None
        )
        provider_manifest, provider, provider_audit = validate_provider(args.private_provider_manifest, tools)
        evidence_seed = args.scratch_root / "evidence"
        build_tool_verification = evidence_seed / "build-tool-verification.json"
        build_tool_verification_bytes = build_tool_verification.read_bytes()
        shutil.rmtree(args.scratch_root)
        roots = prepare_scratch(args.scratch_root, source_evidence, patch_path, provider)
        build_tool_verification = roots["evidence"] / "build-tool-verification.json"
        build_tool_verification.write_bytes(build_tool_verification_bytes)
        prefix_evidence = verify_locked_prefix(
            args.package_lock,
            args.source_explicit,
            args.locked_prefix,
            args.package_cache,
            roots["evidence"] / "locked-prefix-verification.json",
        )
        wheels: list[Path] = []
        elf_records: list[dict] = []
        wheel_records: list[dict] = []
        candidate_build_record: dict | None = None
        handoff_record: dict | None = None
        if args.execute:
            wheels, audits = build_candidate(
                roots,
                args.locked_prefix.resolve(),
                tools,
                python_builder,
                python_builder_site,
                libxml2_include,
                zlib_library,
                lzma_library,
            )
            elf_records, wheel_records = collect_outputs(roots, wheels, audits, tools)
            candidate_build_path = roots["evidence"] / "native-build-handoff.json"
            candidate_build_payload = write_candidate_build_handoff(
                candidate_build_path,
                roots=roots,
                locked_prefix=args.locked_prefix.resolve(),
                package_lock=args.package_lock.resolve(),
                prefix_verification=prefix_evidence,
                stock_abi_preflight=stock_abi_evidence,
                source_manifest=args.source_manifest.resolve(),
                source_evidence=source_evidence,
                build_tool_explicit=args.build_tool_explicit.resolve(),
                python_builder_manifest=args.python_builder_manifest.resolve(),
                libxml2_build_deps_manifest=args.libxml2_build_deps_manifest.resolve(),
                private_provider_manifest=args.private_provider_manifest.resolve(),
                wheels=wheels,
                elf_records=elf_records,
                wheel_records=wheel_records,
            )
            candidate_build_record = {
                "path": str(candidate_build_path),
                "sha256": sha256(candidate_build_path),
                "status": candidate_build_payload["status"],
                "candidate_output_binding_sha256": candidate_build_payload["candidate_output_binding_sha256"],
            }
            if closure_evidence is not None:
                handoff_path = roots["evidence"] / "package-handoff" / "native-rebuild-handoff.json"
                handoff_payload = write_package_handoff(
                    handoff_path,
                    roots=roots,
                    locked_prefix=args.locked_prefix.resolve(),
                    package_lock=args.package_lock.resolve(),
                    prefix_verification=prefix_evidence,
                    stock_abi_preflight=stock_abi_evidence,
                    source_manifest=args.source_manifest.resolve(),
                    source_evidence=source_evidence,
                    build_tool_explicit=args.build_tool_explicit.resolve(),
                    python_builder_manifest=args.python_builder_manifest.resolve(),
                    libxml2_build_deps_manifest=args.libxml2_build_deps_manifest.resolve(),
                    private_provider_manifest=args.private_provider_manifest.resolve(),
                    native_closure=closure_evidence,
                    wheels=wheels,
                    elf_records=elf_records,
                    wheel_records=wheel_records,
                )
                handoff_record = {
                    "path": str(handoff_path),
                    "sha256": sha256(handoff_path),
                    "status": handoff_payload["status"],
                    "replacement_count": len(handoff_payload["replacements"]),
                    "conditional_omissions": [item["package"] for item in handoff_payload["conditional_omissions"]],
                }
        provenance_outputs: dict[str, object] = {"elf": elf_records, "wheels": wheel_records}
        if candidate_build_record is not None:
            provenance_outputs["candidate_build_handoff"] = candidate_build_record
        if handoff_record is not None:
            provenance_outputs["package_handoff"] = handoff_record
        provenance = roots["evidence"] / "native-rebuild-provenance.json"
        write_provenance(
            provenance,
            inputs={
                "source_manifest": {"path": str(args.source_manifest.resolve()), "sha256": sha256(args.source_manifest)},
                "package_lock": {"path": str(args.package_lock.resolve()), "sha256": sha256(args.package_lock)},
                "build_toolchain": {
                    "explicit": {"path": str(args.build_tool_explicit.resolve()), "sha256": sha256(args.build_tool_explicit)},
                    "verification": {"path": str(build_tool_verification), "sha256": sha256(build_tool_verification)},
                    "identity": build_tool_evidence.get("roots"),
                    "executables": build_tool_evidence.get("executables"),
                },
                "python_builder_manifest": {
                    "path": str(args.python_builder_manifest.resolve()),
                    "sha256": sha256(args.python_builder_manifest),
                    "identity": python_builder_evidence,
                },
                "libxml2_build_deps_manifest": {
                    "path": str(args.libxml2_build_deps_manifest.resolve()),
                    "sha256": sha256(args.libxml2_build_deps_manifest),
                    "identity": libxml2_build_evidence,
                },
                "private_provider_manifest": {"path": str(args.private_provider_manifest.resolve()), "sha256": sha256(args.private_provider_manifest), "library": provider_audit},
                "sources": source_evidence,
                "recipe_source_bindings": recipe_bindings,
                "spdlog_patch": patch_evidence,
                "locked_packages": package_evidence,
                "locked_prefix": {"path": str(args.locked_prefix.resolve()), "verification": prefix_evidence},
                "native_closure": closure_evidence,
                "stock_abi_preflight": stock_abi_evidence,
            },
            outputs=provenance_outputs,
            executed=args.execute,
        )
        print(f"rebuild_mode={'execute' if args.execute else 'validate-only'}")
        print(f"provenance={provenance}")
        if candidate_build_record is not None:
            print(f"candidate_build_handoff={candidate_build_record['path']}")
        if handoff_record is not None:
            print(f"package_handoff={handoff_record['path']}")
        elif args.execute:
            print("native_closure_required_for_package_handoff=true")
        print("source_rebuild_provenance_eligible=false")
        return 0
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
