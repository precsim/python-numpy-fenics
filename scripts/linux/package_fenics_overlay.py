#!/usr/bin/env python3
"""Package a relocatable Linux FEniCS overlay from a verified exact prefix."""

from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse


REQUIRED_FIELDS = (
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
EXPECTED_RECORD_COUNT = 128
ALWAYS_EXCLUDED_PACKAGES = {
    "_python_abi3_support",
    "cpython",
    "libpython",
    "numpy",
    "pip",
    "python",
    "python-gil",
    "python_abi",
    "wheel",
}
BASE_CONDITIONAL_EXCLUDES = {"setuptools"}
FORBIDDEN_PROVIDER_PACKAGES = {
    "openmpi",
    "mvapich",
    "mvapich2",
    "cuda-version",
    "cuda-cudart",
    "rocm-core",
    "rocm-smi",
}
NUMPY_PREFIXES = (
    "lib/python3.12/site-packages/numpy/",
    "lib/python3.12/site-packages/numpy.libs/",
)
GCC_SPECS_RELATIVE = PurePosixPath("lib/gcc/x86_64-conda-linux-gnu/16.2.0/conda.specs")
GCC_SPECS_PREFIX_TOKEN = "@FENICS_PRIVATE_PREFIX@"
REQUIRED_SYMBOL_CEILINGS = {
    "GLIBC": (2, 17),
    "GLIBCXX": (3, 4, 19),
    "CXXABI": (1, 3, 7),
    "GCC": (4, 8, 0),
}
ALLOWED_NONNUMERIC_VERSION_TOKENS = {"CXXABI_TM_1"}
PROTECTED_JIT_BIN_PATHS = {
    "bin/x86_64-conda-linux-gnu-gcc",
    "bin/x86_64-conda-linux-gnu-ld",
    "bin/x86_64-conda-linux-gnu-as",
    "bin/x86_64-conda-linux-gnu-ar",
    "bin/x86_64-conda-linux-gnu-ranlib",
    "bin/gcc",
    "bin/cc",
}


def protected_jit_bin_path(relative: str) -> bool:
    return relative in PROTECTED_JIT_BIN_PATHS or relative.startswith(
        "bin/x86_64-conda-linux-gnu-"
    )


def run(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_relative(raw: str) -> PurePosixPath:
    value = raw.replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "" in path.parts:
        raise SystemExit(f"unsafe package-owned path: {raw!r}")
    return path


def under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def load_lock(path: Path) -> dict[str, Any]:
    lock = read_json(path)
    packages = lock.get("packages")
    if (
        lock.get("format_version") != 1
        or lock.get("architecture") != "x86_64"
        or not isinstance(packages, list)
        or lock.get("record_count") != len(packages)
        or len(packages) != EXPECTED_RECORD_COUNT
    ):
        raise SystemExit(f"invalid Linux FEniCS package lock: {path}")
    seen: set[str] = set()
    for record in packages:
        if not isinstance(record, dict):
            raise SystemExit("package lock contains a non-object record")
        missing = [field for field in REQUIRED_FIELDS if record.get(field) in (None, "")]
        if missing:
            raise SystemExit(f"package lock record is missing fields {missing!r}: {record!r}")
        name = str(record["name"])
        if name in seen:
            raise SystemExit(f"package lock repeats package name: {name}")
        seen.add(name)
        if record["subdir"] not in {"linux-64", "noarch"}:
            raise SystemExit(f"package lock contains cross-target package: {name}: {record['subdir']}")
        if record["channel"] != "conda-forge":
            raise SystemExit(f"package lock contains non-conda-forge package: {name}")
    by_name = {str(record["name"]): record for record in packages}
    required = {
        "fenics-dolfinx": "0.11.0",
        "petsc": "3.25.6",
        "petsc4py": "3.25.6",
        "mpi4py": "4.1.2",
        "mpich": "5.0.1",
        "fenics-basix": "0.11.0",
        "fenics-ffcx": "0.11.0",
        "fenics-ufl": "2026.1.0",
        "numpy": "2.5.2",
        "sysroot_linux-64": "2.17",
    }
    for name, version in required.items():
        if str(by_name.get(name, {}).get("version")) != version:
            raise SystemExit(f"package lock has unexpected {name} version")
    forbidden = sorted(name for name in by_name if name in FORBIDDEN_PROVIDER_PACKAGES)
    forbidden += sorted(
        name for name in by_name if name.startswith(("openmpi-", "mvapich-", "cuda-", "rocm-"))
    )
    if forbidden:
        raise SystemExit(f"package lock contains forbidden runtime providers: {sorted(set(forbidden))}")
    return lock


def installed_records(prefix: Path) -> dict[str, dict[str, Any]]:
    conda_meta = prefix / "conda-meta"
    if not conda_meta.is_dir():
        raise SystemExit(f"verified locked prefix has no conda metadata: {conda_meta}")
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(conda_meta.glob("*.json")):
        record = read_json(path)
        name = str(record.get("name", ""))
        if not name or name in records:
            raise SystemExit(f"ambiguous installed package metadata: {path}")
        records[name] = record
    return records


def normalized_record(record: dict[str, Any]) -> dict[str, Any]:
    return {field: record.get(field) for field in REQUIRED_FIELDS}


def verify_external_prefix_evidence(
    lock: dict[str, Any], lock_path: Path, evidence_path: Path, records: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    evidence = read_json(evidence_path)
    archive_records = evidence.get("verified_package_archives")
    if (
        evidence.get("format_version") != 1
        or evidence.get("architecture") != "x86_64"
        or evidence.get("solver_used") is not False
        or evidence.get("package_hash_verification")
        != "cached-package-sha256-plus-installed-record-match"
        or evidence.get("replay_input_format") != "@EXPLICIT-url-plus-sha256"
        or evidence.get("verified_package_archive_count") != len(lock["packages"])
        or evidence.get("lock_sha256") != sha256_file(lock_path)
        or evidence.get("lock_record_count") != len(lock["packages"])
        or evidence.get("installed_record_count") != len(lock["packages"])
        or not isinstance(archive_records, list)
        or len(archive_records) != len(lock["packages"])
    ):
        raise SystemExit(f"invalid external locked-prefix verification evidence: {evidence_path}")
    locked = {str(record["name"]): normalized_record(record) for record in lock["packages"]}
    installed = {name: normalized_record(record) for name, record in records.items()}
    evidence_records = evidence.get("packages")
    if not isinstance(evidence_records, list):
        raise SystemExit("locked-prefix verification evidence has no package records")
    verified = {str(record.get("name")): normalized_record(record) for record in evidence_records}
    if installed != locked or verified != locked:
        raise SystemExit("verified prefix, external evidence and checked-in package lock differ")
    archives_by_name = {
        str(record.get("name")): record
        for record in archive_records
        if isinstance(record, dict) and record.get("name")
    }
    if len(archives_by_name) != len(lock["packages"]):
        raise SystemExit("locked-prefix evidence has duplicate or missing package archive records")
    for record in lock["packages"]:
        name = str(record["name"])
        archive = archives_by_name.get(name)
        if (
            not isinstance(archive, dict)
            or str(archive.get("sha256", "")).lower() != str(record["sha256"]).lower()
            or not isinstance(archive.get("cache_paths"), list)
            or not archive["cache_paths"]
        ):
            raise SystemExit(f"locked-prefix evidence does not bind cached package bytes: {name}")
    return evidence


def validate_runtime_manifest_tree(root: Path, manifest_path: Path, manifest: dict[str, Any]) -> None:
    """Bind the qualified base manifest to every inherited runtime byte/symlink."""

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise SystemExit(f"public NumPy runtime manifest has no file inventory: {manifest_path}")
    expected: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("path"):
            raise SystemExit(f"invalid public NumPy runtime manifest entry: {entry!r}")
        relative = safe_relative(str(entry["path"]))
        rel = relative.as_posix()
        if rel == "runtime-manifest.json" or rel in expected:
            raise SystemExit(f"ambiguous public NumPy runtime manifest path: {rel}")
        expected[rel] = entry

    actual: set[str] = set()
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == "runtime-manifest.json":
            continue
        actual.add(rel)
    missing = sorted(set(expected) - actual)
    extra = sorted(actual - set(expected))
    if missing or extra:
        raise SystemExit(
            f"public NumPy runtime tree differs from its manifest: missing={missing!r} extra={extra!r}"
        )

    for rel, entry in expected.items():
        path = root / Path(*PurePosixPath(rel).parts)
        kind = entry.get("kind")
        if kind == "symlink":
            if not path.is_symlink() or os.readlink(path) != entry.get("target"):
                raise SystemExit(f"public NumPy runtime symlink identity mismatch: {rel}")
        elif kind == "file":
            if not path.is_file() or path.is_symlink():
                raise SystemExit(f"public NumPy runtime file identity mismatch: {rel}")
            if path.stat().st_size != entry.get("bytes") or sha256_file(path) != entry.get("sha256"):
                raise SystemExit(f"public NumPy runtime file hash mismatch: {rel}")
        else:
            raise SystemExit(f"unsupported public NumPy runtime manifest kind for {rel}: {kind!r}")


def validate_base_runtime(base: Path, qualification_path: Path) -> dict[str, Any]:
    manifest_path = base / "runtime-manifest.json"
    core_provenance_path = base / "build-provenance-linux.json"
    provenance_path = base / "build-provenance-linux-numpy.json"
    numpy_payload = base / "numpy-payload-manifest-linux.json"
    for required in (
        manifest_path,
        core_provenance_path,
        provenance_path,
        numpy_payload,
        base / "bin" / "python3.12",
    ):
        if not required.exists():
            raise SystemExit(f"qualified public NumPy base input is missing: {required}")
    manifest = read_json(manifest_path)
    if (
        manifest.get("format_version") != 1
        or manifest.get("platform") != "linux-x64"
        or manifest.get("profile") != "numpy"
        or manifest.get("python_abi") != "cp312"
        or manifest.get("producer", {}).get("repository") != "precsim/python-numpy-fenics"
    ):
        raise SystemExit(f"invalid public NumPy base runtime manifest: {manifest_path}")
    validate_runtime_manifest_tree(base, manifest_path, manifest)
    qualification = read_json(qualification_path)
    checks = qualification.get("checks")
    required_checks = {
        "pinned_manylinux",
        "glibc_floor",
        "inherited_core_identity",
        "numpy_source_build",
        "private_openblas",
        "blas_lapack_functional",
        "native_closure",
    }
    if (
        qualification.get("format_version") != 1
        or qualification.get("producer_repository") != "precsim/python-numpy-fenics"
        or qualification.get("platform") != "linux-x64"
        or qualification.get("profile") != "numpy"
        or qualification.get("producer_revision") != manifest.get("producer", {}).get("revision")
        or not isinstance(checks, dict)
        or any(checks.get(name) != "pass" for name in required_checks)
    ):
        raise SystemExit(f"invalid public NumPy qualification evidence: {qualification_path}")
    core_provenance = read_json(core_provenance_path)
    base_provenance = read_json(provenance_path)
    baseline = base_provenance.get("baseline", {})
    if baseline.get("architecture") != "x86_64" or str(baseline.get("glibc")) != "2.17":
        raise SystemExit("public NumPy base does not carry the qualified Linux glibc-2.17 baseline")
    if base_provenance.get("numpy", {}).get("version") != "2.5.2":
        raise SystemExit("public NumPy base provenance does not identify NumPy 2.5.2")
    if core_provenance.get("python", {}).get("version") != "3.12.10":
        raise SystemExit("public NumPy base provenance does not identify CPython 3.12.10")
    site = base / "lib" / "python3.12" / "site-packages"
    if not (site / "numpy").is_dir():
        raise SystemExit("public NumPy base is missing NumPy")
    return {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "core_provenance": core_provenance,
        "core_provenance_path": core_provenance_path,
        "provenance": base_provenance,
        "provenance_path": provenance_path,
        "numpy_payload_path": numpy_payload,
        "qualification": qualification,
        "qualification_path": qualification_path,
    }


def package_owned_paths(record: dict[str, Any]) -> list[dict[str, Any]]:
    paths_data = record.get("paths_data")
    raw_paths = paths_data.get("paths") if isinstance(paths_data, dict) else None
    entries: list[dict[str, Any]] = []
    if isinstance(raw_paths, list) and raw_paths:
        for item in raw_paths:
            if not isinstance(item, dict) or not item.get("_path"):
                raise SystemExit(f"invalid paths_data entry for {record.get('name')}: {item!r}")
            entries.append(dict(item) | {"path": str(item["_path"])})
    else:
        files = record.get("files")
        if not isinstance(files, list):
            raise SystemExit(f"package metadata has neither paths_data nor files: {record.get('name')}")
        entries = [{"path": str(item), "path_type": None} for item in files]
    seen: set[str] = set()
    result = []
    for entry in entries:
        relative = safe_relative(entry["path"])
        normalized = relative.as_posix()
        if normalized in seen:
            raise SystemExit(f"package metadata repeats an owned path: {record.get('name')}: {normalized}")
        seen.add(normalized)
        result.append(entry | {"relative": relative})
    return result


def numpy_owned_path(relative: PurePosixPath) -> bool:
    text = relative.as_posix()
    return text.startswith(NUMPY_PREFIXES) or bool(
        re.match(r"^lib/python3\.12/site-packages/numpy-[^/]+\.dist-info(?:/|$)", text)
    )


def path_exclusion_reason(relative: PurePosixPath) -> str | None:
    text = relative.as_posix()
    if "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}:
        return "generated-python-bytecode"
    if text.startswith("etc/conda/test-files/"):
        return "conda-package-test-fixture"
    if text == "lib/python3.12/site-packages/petsc4py/lib/petsc.cfg":
        return "runtime-derived-petsc4py-config"
    if text.startswith(("lib/pkgconfig/", "lib/cmake/", "share/cmake/", "share/pkgconfig/")):
        return "developer-metadata"
    if text.startswith(("lib/petsc/conf/", "lib/petsc/bin/")):
        return "petsc-build-metadata"
    if relative.suffix == ".la":
        return "libtool-metadata"
    return None


def base_owns_package(base_site: Path, name: str) -> bool:
    normalized = name.replace("-", "_")
    if (base_site / normalized).exists():
        return True
    patterns = (f"{name}-*.dist-info", f"{normalized}-*.dist-info")
    return any(any(base_site.glob(pattern)) for pattern in patterns)


def package_excluded(name: str, base_site: Path) -> bool:
    return name in ALWAYS_EXCLUDED_PACKAGES or (
        name in BASE_CONDITIONAL_EXCLUDES and base_owns_package(base_site, name)
    )


def copy_path(source: Path, destination: Path, source_prefix: Path, private_prefix: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        raw_target = os.readlink(source)
        target_path = Path(raw_target)
        resolved = (
            target_path.resolve(strict=False)
            if target_path.is_absolute()
            else (source.parent / target_path).resolve(strict=False)
        )
        try:
            relative_target = resolved.relative_to(source_prefix)
        except ValueError as exc:
            raise SystemExit(f"package symlink escapes producer prefix: {source} -> {raw_target}") from exc
        target = os.path.relpath(private_prefix / relative_target, destination.parent)
        if destination.exists() or destination.is_symlink():
            if not destination.is_symlink() or os.readlink(destination) != target:
                raise SystemExit(f"conflicting package ownership at symlink: {destination}")
            return
        os.symlink(target, destination)
        return
    if not source.is_file():
        raise SystemExit(f"package-owned path is not a file/symlink: {source}")
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or sha256_file(source) != sha256_file(destination):
            raise SystemExit(f"conflicting package ownership at file: {destination}")
        return
    shutil.copy2(source, destination, follow_symlinks=False)


def find_cache_record(package_cache: Path, record: dict[str, Any]) -> Path | None:
    identity = (str(record["name"]), str(record["version"]), str(record["build"]))
    dist = "-".join(identity)
    candidates = [package_cache / dist]
    parsed = urlparse(str(record.get("url") or ""))
    if parsed.scheme and parsed.netloc:
        hierarchy = package_cache / parsed.scheme / parsed.netloc
        for part in PurePosixPath(parsed.path).parent.parts:
            if part not in {"", "/"}:
                hierarchy /= part
        candidates.append(hierarchy / dist)
    candidates.extend(path.parent.parent for path in package_cache.rglob("info/index.json"))
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        index = candidate / "info" / "index.json"
        if not index.is_file():
            continue
        try:
            meta = read_json(index)
        except Exception:
            continue
        actual = (str(meta.get("name")), str(meta.get("version")), str(meta.get("build")))
        if actual == identity:
            return candidate
    return None


def collect_licenses(
    stage_root: Path,
    package_cache: Path,
    records: dict[str, dict[str, Any]],
    retained_packages: list[str],
    owners: dict[str, list[str]],
) -> dict[str, Any]:
    inventory = []
    license_root = stage_root / "fenics-runtime" / "licenses"
    for name in retained_packages:
        record = records[name]
        cache_record = find_cache_record(package_cache, record)
        if cache_record is None:
            raise SystemExit(f"package cache is missing retained package metadata: {name}")
        info = cache_record / "info"
        about = read_json(info / "about.json") if (info / "about.json").is_file() else {}
        copied: list[str] = []
        sources: list[tuple[Path, Path]] = []
        source_root = info / "licenses"
        if source_root.is_dir():
            for source in sorted(source_root.rglob("*")):
                if source.is_file() and not source.is_symlink():
                    sources.append((source, Path("licenses") / source.relative_to(source_root)))
        for source in sorted(info.glob("license*")):
            if source.is_file() and not source.is_symlink():
                sources.append((source, Path(source.name)))
        destination_root = license_root / f"{name}-{record['version']}-{record['build']}"
        seen: set[Path] = set()
        for source, relative in sources:
            resolved = source.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            rel = destination.relative_to(stage_root).as_posix()
            owners[rel] = [name]
            copied.append(rel)
        inventory.append(
            {
                "name": name,
                "version": record.get("version"),
                "build": record.get("build"),
                "license": about.get("license") or record.get("license"),
                "license_family": about.get("license_family") or record.get("license_family"),
                "license_files": copied,
                "evidence_kind": "package-license-files" if copied else "package-metadata",
            }
        )
    return {"format_version": 1, "packages": inventory}


def is_elf(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink() and path.open("rb").read(4) == b"\x7fELF"
    except OSError:
        return False


def dynamic(path: Path) -> tuple[list[str], str | None, list[str], bool]:
    text = run("readelf", "-dW", str(path)).stdout
    needed = re.findall(r"Shared library: \[([^\]]+)\]", text)
    soname_match = re.search(r"Library soname: \[([^\]]+)\]", text)
    runpaths = re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]*)\]", text)
    return needed, soname_match.group(1) if soname_match else None, runpaths, "Dynamic section" in text


def version_needs(path: Path) -> list[str]:
    text = run("readelf", "--version-info", "-W", str(path)).stdout
    marker = "Version needs section"
    if marker not in text:
        return []
    needs = text.split(marker, 1)[1]
    return sorted(set(re.findall(r"\bName: ((?:GLIBC|CXXABI|GLIBCXX|GCC)_[A-Za-z0-9_.]+)", needs)))


def numeric_version(token: str, namespace: str) -> tuple[int, ...] | None:
    prefix = namespace + "_"
    if not token.startswith(prefix):
        return None
    suffix = token[len(prefix) :]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", suffix):
        return None
    return tuple(int(part) for part in suffix.split("."))


def version_text(value: tuple[int, ...] | None) -> str | None:
    return None if value is None else ".".join(str(part) for part in value)


def set_relative_runpaths(stage_root: Path, private_prefix: Path) -> list[Path]:
    sysroot = private_prefix / "x86_64-conda-linux-gnu" / "sysroot"
    dynamic_elfs: list[Path] = []
    provider_dirs: set[Path] = {private_prefix / "lib"}
    for path in sorted(stage_root.rglob("*")):
        if not is_elf(path) or under(path, sysroot):
            continue
        _, soname, _, has_dynamic = dynamic(path)
        if not has_dynamic:
            continue
        dynamic_elfs.append(path)
        if soname or ".so" in path.name:
            provider_dirs.add(path.parent.resolve())
    if not dynamic_elfs:
        raise SystemExit("FEniCS overlay has no dynamic ELF objects")
    for path in dynamic_elfs:
        entries = ["$ORIGIN"]
        for directory in sorted(provider_dirs, key=lambda item: str(item)):
            relative = os.path.relpath(directory, path.parent.resolve()).replace(os.sep, "/")
            entry = "$ORIGIN" if relative == "." else f"$ORIGIN/{relative}"
            if entry not in entries:
                entries.append(entry)
        # DT_RPATH intentionally precedes hostile LD_LIBRARY_PATH lookup.  The
        # bootstrap also removes LD_LIBRARY_PATH before imports, but glibc may
        # cache the process-start search path; force-rpath makes the overlay's
        # bundled providers authoritative regardless.
        run("patchelf", "--force-rpath", "--set-rpath", ":".join(entries), str(path))
    return dynamic_elfs


def audit_elf(
    stage_root: Path,
    private_prefix: Path,
    base_provenance: dict[str, Any],
    owners: dict[str, list[str]],
) -> dict[str, Any]:
    sysroot = private_prefix / "x86_64-conda-linux-gnu" / "sysroot"
    all_elfs = [path for path in sorted(stage_root.rglob("*")) if is_elf(path)]
    runtime_elfs = [path for path in all_elfs if not under(path, sysroot)]
    provider_index: dict[str, list[Path]] = defaultdict(list)
    dynamic_map: dict[Path, tuple[list[str], str | None, list[str], bool]] = {}
    for path in runtime_elfs:
        item = dynamic(path)
        dynamic_map[path] = item
        _, soname, _, has_dynamic = item
        if not has_dynamic:
            continue
        provider_index[path.name].append(path)
        if soname:
            provider_index[soname].append(path)
    runtime_real_elfs = {path.resolve() for path in runtime_elfs}
    for path in sorted(stage_root.rglob("*")):
        if not path.is_symlink():
            continue
        target = path.resolve(strict=False)
        if target in runtime_real_elfs:
            provider_index[path.name].append(target)

    baseline = base_provenance.get("baseline", {})
    external = set(baseline.get("pep599_external_allowlist", []))
    external.add("ld-linux-x86-64.so.2")
    maxima: dict[str, tuple[int, ...] | None] = {
        "GLIBC": None,
        "GLIBCXX": None,
        "CXXABI": None,
        "GCC": None,
    }
    version_tokens: set[str] = set()
    nonnumeric_tokens: set[str] = set()
    ceiling_violations: list[dict[str, Any]] = []
    for path in all_elfs:
        header = run("readelf", "-hW", str(path)).stdout
        if "Advanced Micro Devices X86-64" not in header or "ELF64" not in header:
            raise SystemExit(f"overlay contains non-x86_64 ELF: {path.relative_to(stage_root)}")
        for token in version_needs(path):
            version_tokens.add(token)
            matched_numeric = False
            for namespace in maxima:
                value = numeric_version(token, namespace)
                if value is not None:
                    matched_numeric = True
                    if maxima[namespace] is None or value > maxima[namespace]:
                        maxima[namespace] = value
                ceiling = REQUIRED_SYMBOL_CEILINGS[namespace]
                if value is not None and value > ceiling:
                    rel = path.relative_to(stage_root).as_posix()
                    ceiling_violations.append(
                        {
                            "path": rel,
                            "owners": owners.get(rel, []),
                            "required": token,
                            "ceiling": f"{namespace}_{version_text(ceiling)}",
                        }
                    )
            if not matched_numeric:
                nonnumeric_tokens.add(token)
    unexpected_nonnumeric = sorted(nonnumeric_tokens - ALLOWED_NONNUMERIC_VERSION_TOKENS)
    if unexpected_nonnumeric:
        raise SystemExit(
            "stock FEniCS package closure requires unsupported nonnumeric ABI symbol versions: "
            f"{unexpected_nonnumeric}"
        )
    if ceiling_violations:
        raise SystemExit(
            "stock FEniCS package closure exceeds required native symbol ceilings; "
            "selective rebuild decisions require this executable evidence:\n"
            + json.dumps(ceiling_violations, indent=2, sort_keys=True)
        )

    census = []
    for path in runtime_elfs:
        needed, soname, runpaths, has_dynamic = dynamic_map[path]
        if not has_dynamic:
            continue
        rel = path.relative_to(stage_root).as_posix()
        search_dirs: set[Path] = set()
        for packed in runpaths:
            for entry in packed.split(":"):
                if not entry.startswith("$ORIGIN"):
                    raise SystemExit(f"non-relative ELF RUNPATH/RPATH remains: {rel}: {entry!r}")
                suffix = entry[len("$ORIGIN") :]
                if suffix and not suffix.startswith("/"):
                    raise SystemExit(f"malformed ELF RUNPATH/RPATH remains: {rel}: {entry!r}")
                target = (path.parent / suffix.lstrip("/")).resolve(strict=False)
                if not under(target, stage_root):
                    raise SystemExit(f"ELF RUNPATH/RPATH escapes overlay: {rel}: {entry!r}")
                search_dirs.add(target)
        dependencies = []
        for name in needed:
            candidates = {candidate.resolve() for candidate in provider_index.get(name, [])}
            if candidates:
                if len(candidates) != 1:
                    raise SystemExit(f"ambiguous bundled ELF provider: {rel} -> {name}: {sorted(map(str, candidates))}")
                provider = next(iter(candidates))
                if provider.parent.resolve() not in search_dirs:
                    raise SystemExit(f"RUNPATH does not cover bundled dependency: {rel} -> {name} -> {provider}")
                dependencies.append(
                    {
                        "name": name,
                        "resolution": "bundled",
                        "provider": provider.relative_to(stage_root).as_posix(),
                        "provider_sha256": sha256_file(provider),
                    }
                )
            elif name in external:
                dependencies.append({"name": name, "resolution": "manylinux2014-external"})
            else:
                raise SystemExit(f"unresolved non-system ELF dependency: {rel} -> {name}")
        census.append(
            {
                "path": rel,
                "sha256": sha256_file(path),
                "soname": soname,
                "needed": needed,
                "runpath": runpaths,
                "dependencies": dependencies,
                "required_versions": version_needs(path),
            }
        )

    mpi_candidates = sorted(
        {
            path.resolve()
            for path in runtime_elfs
            if re.fullmatch(r"libmpi(?:\.so(?:\..*)?)?", path.name)
        },
        key=lambda item: str(item),
    )
    if len(mpi_candidates) != 1:
        raise SystemExit(f"expected exactly one MPICH libmpi provider, found: {mpi_candidates}")
    mpi_provider = mpi_candidates[0]
    mpi_rel = mpi_provider.relative_to(stage_root).as_posix()
    if "mpich" not in owners.get(mpi_rel, []):
        raise SystemExit(
            f"unique libmpi provider is not owned by the locked MPICH package: {mpi_rel}: {owners.get(mpi_rel)}"
        )

    forbidden_paths = [
        path.relative_to(stage_root).as_posix()
        for path in runtime_elfs
        if re.search(r"(?:openmpi|mvapich|/cuda(?:/|-)|/rocm(?:/|-))", path.as_posix(), re.IGNORECASE)
    ]
    if forbidden_paths:
        raise SystemExit(f"forbidden MPI/GPU runtime provider files are staged: {forbidden_paths}")

    return {
        "format_version": 1,
        "elf_count": len(all_elfs),
        "runtime_dynamic_elf_count": len(census),
        "glibc_ceiling": "2.17",
        "glibcxx_ceiling": "3.4.19",
        "cxxabi_ceiling": "1.3.7",
        "gcc_ceiling": "4.8.0",
        "measured_max_glibc": version_text(maxima["GLIBC"]),
        "measured_max_glibcxx": version_text(maxima["GLIBCXX"]),
        "measured_max_cxxabi": version_text(maxima["CXXABI"]),
        "measured_max_gcc": version_text(maxima["GCC"]),
        "measured_required_versions": sorted(version_tokens),
        "allowed_nonnumeric_required_versions": sorted(
            nonnumeric_tokens & ALLOWED_NONNUMERIC_VERSION_TOKENS
        ),
        "mpi": {
            "implementation": "mpich",
            "provider": mpi_rel,
            "provider_sha256": sha256_file(mpi_provider),
            "provider_count": 1,
        },
        "census": census,
    }


def verify_symlink_closure(stage_root: Path) -> None:
    for path in sorted(stage_root.rglob("*")):
        if not path.is_symlink():
            continue
        target = Path(os.readlink(path))
        resolved = target.resolve(strict=False) if target.is_absolute() else (path.parent / target).resolve(strict=False)
        if not under(resolved, stage_root):
            raise SystemExit(f"staged overlay symlink escapes payload: {path} -> {target}")
        if not resolved.exists():
            raise SystemExit(f"staged overlay contains dangling symlink: {path} -> {target}")


def drop_prefix_bound_bin_scripts(
    stage_root: Path,
    private_prefix: Path,
    source_prefix: Path,
    owners: dict[str, list[str]],
    excluded_owned_paths: list[dict[str, str]],
) -> None:
    needle = str(source_prefix).encode()
    bin_root = private_prefix / "bin"
    if not bin_root.is_dir():
        return
    for path in sorted(bin_root.rglob("*")):
        if not path.is_file() or path.is_symlink() or is_elf(path):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if needle not in data:
            continue
        rel = path.relative_to(stage_root).as_posix()
        private_rel = path.relative_to(private_prefix).as_posix()
        if protected_jit_bin_path(private_rel):
            raise SystemExit(f"required JIT tool contains an unresolved producer prefix: {rel}")
        path.unlink()
        owner_names = owners.pop(rel, ["unknown"])
        for owner in owner_names:
            excluded_owned_paths.append(
                {"package": owner, "path": rel, "reason": "producer-prefix-bound-command-script"}
            )
    for path in sorted(bin_root.rglob("*")):
        if not path.is_symlink():
            continue
        target = Path(os.readlink(path))
        resolved = target.resolve(strict=False) if target.is_absolute() else (path.parent / target).resolve(strict=False)
        if resolved.exists():
            continue
        rel = path.relative_to(stage_root).as_posix()
        private_rel = path.relative_to(private_prefix).as_posix()
        if protected_jit_bin_path(private_rel):
            raise SystemExit(f"required JIT tool symlink became dangling: {rel} -> {os.readlink(path)}")
        owner_names = owners.pop(rel, ["unknown"])
        path.unlink()
        for owner in owner_names:
            excluded_owned_paths.append(
                {"package": owner, "path": rel, "reason": "dangling-prefix-bound-command-symlink"}
            )


def relocate_conda_gcc_specs(
    private_prefix: Path,
    source_prefix: Path,
    record: dict[str, Any],
    owners: dict[str, list[str]],
    stage_root: Path,
) -> dict[str, Any]:
    specs = private_prefix / Path(*GCC_SPECS_RELATIVE.parts)
    if not specs.is_file():
        raise SystemExit(f"locked conda-gcc-specs template is missing: {specs}")
    rel = specs.relative_to(stage_root).as_posix()
    if "conda-gcc-specs" not in owners.get(rel, []):
        raise SystemExit(f"GCC specs template is not owned by conda-gcc-specs: {rel}")
    entry = next(
        (item for item in package_owned_paths(record) if item["relative"] == GCC_SPECS_RELATIVE),
        None,
    )
    if entry is None:
        raise SystemExit("conda-gcc-specs metadata does not own the expected conda.specs path")
    text = specs.read_text(encoding="utf-8")
    replacements = 0
    candidates = [str(source_prefix), str(entry.get("prefix_placeholder") or "")]
    for candidate in candidates:
        if not candidate or candidate == GCC_SPECS_PREFIX_TOKEN:
            continue
        count = text.count(candidate)
        if count:
            text = text.replace(candidate, GCC_SPECS_PREFIX_TOKEN)
            replacements += count
    if replacements == 0 and GCC_SPECS_PREFIX_TOKEN not in text:
        raise SystemExit("conda-gcc-specs template has no relocatable prefix marker to rewrite")
    specs.write_text(text, encoding="utf-8", newline="\n")
    return {
        "path": rel,
        "sha256": sha256_file(specs),
        "prefix_token": GCC_SPECS_PREFIX_TOKEN,
        "prefix_replacement_count": replacements,
        "runtime_policy": (
            "retained relocation template; fresh-JIT wrapper supplies equivalent private include/lib/sysroot "
            "search explicitly and omits the template's absolute runtime rpath"
        ),
    }


def file_contains_bytes(path: Path, needle: bytes) -> bool:
    if not needle:
        return False
    overlap = max(0, len(needle) - 1)
    tail = b""
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            data = tail + chunk
            if needle in data:
                return True
            tail = data[-overlap:] if overlap else b""
    return False


def reject_absolute_prefix_references(stage_root: Path, prefixes: list[Path]) -> None:
    needles = [(str(prefix), str(prefix).encode()) for prefix in prefixes]
    offenders = []
    for path in sorted(stage_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            matched = [label for label, needle in needles if file_contains_bytes(path, needle)]
            if matched:
                offenders.append(
                    {"path": path.relative_to(stage_root).as_posix(), "prefixes": matched}
                )
        except OSError:
            continue
    if offenders:
        raise SystemExit(
            "staged overlay retains non-relocatable absolute staging/producer-prefix references: "
            + json.dumps(offenders[:50], sort_keys=True)
        )


def numpy_tree_identity(base: Path) -> dict[str, Any]:
    site = base / "lib" / "python3.12" / "site-packages"
    roots = [site / "numpy", site / "numpy.libs"] + sorted(site.glob("numpy-*.dist-info"))
    entries: dict[str, dict[str, Any]] = {}
    aggregate = hashlib.sha256()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(site).as_posix()
            if path.is_symlink():
                target = os.readlink(path)
                entries[rel] = {"kind": "symlink", "target": target}
            elif path.is_file():
                digest = sha256_file(path)
                entries[rel] = {"kind": "file", "sha256": digest, "size": path.stat().st_size}
    if not entries:
        raise SystemExit("qualified NumPy base contains no NumPy payload")
    for rel, identity in sorted(entries.items()):
        if identity["kind"] == "symlink":
            aggregate.update(b"L\0" + rel.encode() + b"\0" + str(identity["target"]).encode() + b"\n")
        else:
            aggregate.update(b"F\0" + rel.encode() + b"\0" + str(identity["sha256"]).encode() + b"\n")
    return {"tree_sha256": aggregate.hexdigest(), "entry_count": len(entries)}


def file_manifest(stage_root: Path, owners: dict[str, list[str]], omit: set[Path] | None = None) -> list[dict[str, Any]]:
    omit = {path.resolve() for path in (omit or set())}
    entries = []
    for path in sorted(stage_root.rglob("*"), key=lambda item: item.relative_to(stage_root).as_posix()):
        if path.resolve(strict=False) in omit or path.is_dir():
            continue
        rel = path.relative_to(stage_root).as_posix()
        owner_names = sorted(set(owners.get(rel, ["python-numpy-fenics"])))
        if path.is_symlink():
            entries.append(
                {
                    "path": rel,
                    "kind": "symlink",
                    "target": os.readlink(path),
                    "owners": owner_names,
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
                    "owners": owner_names,
                }
            )
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", required=True, help="externally replayed and verified exact conda prefix")
    parser.add_argument("--prefix-evidence", required=True, help="package_lock.py verify-prefix evidence JSON")
    parser.add_argument("--package-cache", required=True)
    parser.add_argument("--lock", required=True)
    parser.add_argument("--base-runtime", required=True, help="qualified public linux-x64 NumPy runtime")
    parser.add_argument("--base-qualification", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--producer-revision", default=os.environ.get("PRODUCER_REVISION", os.environ.get("GITHUB_SHA", "local")))
    args = parser.parse_args()

    for tool in ("patchelf", "readelf"):
        if shutil.which(tool) is None:
            raise SystemExit(f"required Linux packaging tool is unavailable: {tool}")

    prefix = Path(args.prefix).resolve()
    evidence_path = Path(args.prefix_evidence).resolve()
    package_cache = Path(args.package_cache).resolve()
    lock_path = Path(args.lock).resolve()
    base = Path(args.base_runtime).resolve()
    base_qualification = Path(args.base_qualification).resolve()
    output = Path(args.output).resolve()
    bootstrap_source = Path(__file__).resolve().parents[2] / "runtime" / "linux" / "fenics_embed_runtime.py"
    archive_helper = Path(__file__).resolve().parents[1] / "common" / "archive_runtime.py"
    for required in (prefix, evidence_path, package_cache, lock_path, base, base_qualification, bootstrap_source, archive_helper):
        if not required.exists():
            raise SystemExit(f"required producer input is missing: {required}")

    lock = load_lock(lock_path)
    records = installed_records(prefix)
    prefix_evidence = verify_external_prefix_evidence(lock, lock_path, evidence_path, records)
    base_info = validate_base_runtime(base, base_qualification)
    base_site = base / "lib" / "python3.12" / "site-packages"
    numpy_identity = numpy_tree_identity(base)

    output.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="fenics-linux-overlay-"))
    stage_root = work / "stage"
    private_prefix = stage_root / "fenics-runtime" / "conda"
    stage_root.mkdir(parents=True)
    owners: dict[str, list[str]] = defaultdict(list)
    retained_packages: list[str] = []
    excluded_packages: list[str] = []
    not_staged_packages: list[str] = []
    excluded_owned_paths: list[dict[str, str]] = []

    try:
        for lock_record in lock["packages"]:
            name = str(lock_record["name"])
            if package_excluded(name, base_site):
                excluded_packages.append(name)
                continue
            record = records[name]
            contributed = False
            for owned in package_owned_paths(record):
                relative: PurePosixPath = owned["relative"]
                if numpy_owned_path(relative):
                    raise SystemExit(f"non-NumPy package unexpectedly owns NumPy payload: {name}: {relative}")
                reason = path_exclusion_reason(relative)
                if reason:
                    excluded_owned_paths.append(
                        {"package": name, "path": relative.as_posix(), "reason": reason}
                    )
                    continue
                source = prefix / Path(*relative.parts)
                if not source.exists() and not source.is_symlink():
                    raise SystemExit(f"locked package-owned path is missing: {name}: {source}")
                expected_prefix_sha = owned.get("sha256_in_prefix")
                if expected_prefix_sha and source.is_file() and not source.is_symlink():
                    actual_prefix_sha = sha256_file(source)
                    if actual_prefix_sha != str(expected_prefix_sha).lower():
                        raise SystemExit(
                            f"installed package-owned file hash mismatch: {name}: {relative}: "
                            f"{actual_prefix_sha} != {expected_prefix_sha}"
                        )
                destination = private_prefix / Path(*relative.parts)
                copy_path(source, destination, prefix, private_prefix)
                rel = destination.relative_to(stage_root).as_posix()
                if name not in owners[rel]:
                    owners[rel].append(name)
                contributed = True
            if contributed:
                retained_packages.append(name)
            else:
                not_staged_packages.append(name)

        bootstrap = stage_root / "lib" / "python3.12" / "site-packages" / "fenics_embed_runtime.py"
        bootstrap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bootstrap_source, bootstrap)
        owners[bootstrap.relative_to(stage_root).as_posix()] = ["python-numpy-fenics"]

        compiler = private_prefix / "bin" / "x86_64-conda-linux-gnu-gcc"
        sysroot = private_prefix / "x86_64-conda-linux-gnu" / "sysroot"
        if not compiler.is_file() or not sysroot.is_dir():
            raise SystemExit(
                f"locked stock compiler/sysroot closure is incomplete: compiler={compiler.exists()} sysroot={sysroot.exists()}"
            )
        if records["sysroot_linux-64"].get("version") != "2.17":
            raise SystemExit("bundled GCC sysroot is not the locked glibc-2.17 sysroot")

        required_jit_owners = {
            "bin/x86_64-conda-linux-gnu-gcc": "gcc_impl_linux-64",
            "bin/x86_64-conda-linux-gnu-ld": "ld_impl_linux-64",
            "bin/x86_64-conda-linux-gnu-as": "binutils_impl_linux-64",
            "bin/x86_64-conda-linux-gnu-ar": "binutils_impl_linux-64",
            "bin/x86_64-conda-linux-gnu-ranlib": "binutils_impl_linux-64",
            GCC_SPECS_RELATIVE.as_posix(): "conda-gcc-specs",
        }
        for private_rel, expected_owner in required_jit_owners.items():
            staged_rel = (Path("fenics-runtime") / "conda" / Path(private_rel)).as_posix()
            if expected_owner not in owners.get(staged_rel, []):
                raise SystemExit(
                    f"required JIT tool path has unexpected package ownership: {staged_rel}: "
                    f"expected {expected_owner}, got {owners.get(staged_rel)}"
                )

        gcc_specs = relocate_conda_gcc_specs(
            private_prefix,
            prefix,
            records["conda-gcc-specs"],
            owners,
            stage_root,
        )

        drop_prefix_bound_bin_scripts(stage_root, private_prefix, prefix, owners, excluded_owned_paths)
        set_relative_runpaths(stage_root, private_prefix)
        verify_symlink_closure(stage_root)
        reject_absolute_prefix_references(stage_root, [prefix, stage_root])

        census = audit_elf(stage_root, private_prefix, base_info["provenance"], owners)
        provenance_root = stage_root / "fenics-runtime" / "provenance"
        provenance_root.mkdir(parents=True, exist_ok=True)
        lock_copy = provenance_root / lock_path.name
        shutil.copy2(lock_path, lock_copy)
        owners[lock_copy.relative_to(stage_root).as_posix()] = ["python-numpy-fenics"]
        prefix_evidence_copy = provenance_root / "locked-prefix-verification.json"
        shutil.copy2(evidence_path, prefix_evidence_copy)
        owners[prefix_evidence_copy.relative_to(stage_root).as_posix()] = ["python-numpy-fenics"]

        package_summary = []
        retained_set = set(retained_packages)
        excluded_set = set(excluded_packages)
        for record in lock["packages"]:
            name = str(record["name"])
            ownership = "retained" if name in retained_set else "excluded" if name in excluded_set else "not-staged"
            package_summary.append(
                {key: record.get(key) for key in REQUIRED_FIELDS} | {"overlay_ownership": ownership}
            )
        package_provenance = {
            "format_version": 1,
            "source": "conda-forge-exact-binary-lock",
            "solver_used_by_packager": False,
            "source_rebuilds": [],
            "package_lock_sha256": sha256_file(lock_path),
            "external_prefix_verification_sha256": sha256_file(evidence_path),
            "packages": package_summary,
            "excluded_owned_paths": excluded_owned_paths,
        }
        package_provenance_path = provenance_root / "package-provenance.json"
        package_provenance_path.write_text(
            json.dumps(package_provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        owners[package_provenance_path.relative_to(stage_root).as_posix()] = ["python-numpy-fenics"]

        source_provenance = {
            "format_version": 1,
            "binary_source_policy": "reuse-locked-conda-forge-packages-first",
            "source_rebuilds": [],
            "package_binary_sources": [
                {
                    "name": record["name"],
                    "version": record["version"],
                    "build": record["build"],
                    "url": record["url"],
                    "sha256": record["sha256"],
                }
                for record in lock["packages"]
            ],
            "package_lock": {"path": lock_copy.relative_to(stage_root).as_posix(), "sha256": sha256_file(lock_path)},
            "locked_prefix_verification": {
                "path": prefix_evidence_copy.relative_to(stage_root).as_posix(),
                "sha256": sha256_file(evidence_path),
                "package_hash_verification": prefix_evidence.get("package_hash_verification"),
            },
            "numpy_base": {
                "manifest_sha256": sha256_file(base_info["manifest_path"]),
                "core_provenance_sha256": sha256_file(base_info["core_provenance_path"]),
                "provenance_sha256": sha256_file(base_info["provenance_path"]),
                "numpy_payload_manifest_sha256": sha256_file(base_info["numpy_payload_path"]),
                "qualification_sha256": sha256_file(base_info["qualification_path"]),
                "producer_revision": base_info["manifest"].get("producer", {}).get("revision"),
                "numpy_tree_sha256": numpy_identity["tree_sha256"],
            },
        }
        source_provenance_path = provenance_root / "source-provenance.json"
        source_provenance_path.write_text(
            json.dumps(source_provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        owners[source_provenance_path.relative_to(stage_root).as_posix()] = ["python-numpy-fenics"]

        census_path = provenance_root / "native-dependency-census.json"
        census_path.write_text(json.dumps(census, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        owners[census_path.relative_to(stage_root).as_posix()] = ["python-numpy-fenics"]

        licenses = collect_licenses(stage_root, package_cache, records, retained_packages, owners)
        licenses_path = provenance_root / "license-inventory.json"
        licenses_path.write_text(json.dumps(licenses, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        owners[licenses_path.relative_to(stage_root).as_posix()] = ["python-numpy-fenics"]

        reject_absolute_prefix_references(stage_root, [prefix, stage_root])
        manifest_path = stage_root / "fenics-embed-manifest.json"
        files = file_manifest(stage_root, owners, omit={manifest_path})
        asset_name = f"fenics-embed-runtime-v{args.version}-linux-x64-cp312.tar.gz"
        manifest = {
            "format_version": 1,
            "artifact": asset_name.removesuffix(".tar.gz"),
            "version": args.version,
            "platform": "linux-x64",
            "profile": "fenics",
            "architecture": "x86_64",
            "python_abi": "cp312",
            "consumer_python": "3.12.10",
            "producer_python": records["python"].get("version"),
            "numpy": {
                "version": "2.5.2",
                "ownership": "consumer-numpy-profile",
                "producer_payload_excluded": True,
                "base_tree_sha256": numpy_identity["tree_sha256"],
                "base_entry_count": numpy_identity["entry_count"],
            },
            "producer": {
                "repository": "precsim/python-numpy-fenics",
                "revision": args.producer_revision,
                "package_lock": lock_copy.relative_to(stage_root).as_posix(),
                "package_lock_sha256": sha256_file(lock_path),
                "package_count": len(lock["packages"]),
                "package_hash_verification": prefix_evidence.get("package_hash_verification"),
                "dependency_solver": "external-lock-replay-only",
                "source_rebuilds": [],
                "retained_packages": retained_packages,
                "excluded_packages": excluded_packages,
                "not_staged_packages": not_staged_packages,
            },
            "layout": {
                "private_prefix": "fenics-runtime/conda",
                "private_site_packages": "fenics-runtime/conda/lib/python3.12/site-packages",
                "bootstrap": "lib/python3.12/site-packages/fenics_embed_runtime.py",
                "gcc": "fenics-runtime/conda/bin/x86_64-conda-linux-gnu-gcc",
                "sysroot": "fenics-runtime/conda/x86_64-conda-linux-gnu/sysroot",
            },
            "jit_toolchain": {
                "packages": {
                    name: {
                        "version": records[name]["version"],
                        "build": records[name]["build"],
                        "sha256": records[name]["sha256"],
                        "url": records[name]["url"],
                    }
                    for name in (
                        "gcc",
                        "gcc_impl_linux-64",
                        "conda-gcc-specs",
                        "ld_impl_linux-64",
                        "binutils_impl_linux-64",
                        "sysroot_linux-64",
                        "kernel-headers_linux-64",
                    )
                },
                "conda_specs_template": gcc_specs,
            },
            "native": {
                "census": census_path.relative_to(stage_root).as_posix(),
                "census_sha256": sha256_file(census_path),
                "elf_count": census["elf_count"],
                "runtime_dynamic_elf_count": census["runtime_dynamic_elf_count"],
                "glibc_ceiling": "2.17",
                "measured_max_glibc": census["measured_max_glibc"],
                "measured_max_glibcxx": census["measured_max_glibcxx"],
                "measured_max_cxxabi": census["measured_max_cxxabi"],
                "measured_max_gcc": census["measured_max_gcc"],
                "mpi": census["mpi"],
            },
            "provenance": {
                "packages": package_provenance_path.relative_to(stage_root).as_posix(),
                "packages_sha256": sha256_file(package_provenance_path),
                "sources": source_provenance_path.relative_to(stage_root).as_posix(),
                "sources_sha256": sha256_file(source_provenance_path),
            },
            "licenses": {
                "inventory": licenses_path.relative_to(stage_root).as_posix(),
                "inventory_sha256": sha256_file(licenses_path),
            },
            "files": files,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        archive = output / asset_name
        run(sys.executable, str(archive_helper), "--root", str(stage_root), "--output", str(archive), "--format", "tar.gz")
        archive_sha = sha256_file(archive)
        summary = {
            "format_version": 1,
            "artifact": asset_name,
            "artifact_bytes": archive.stat().st_size,
            "sha256": archive_sha,
            "manifest_sha256": sha256_file(manifest_path),
            "package_lock_sha256": sha256_file(lock_path),
            "overlay_file_count": len(files) + 1,
            "elf_count": census["elf_count"],
            "measured_max_glibc": census["measured_max_glibc"],
            "measured_max_glibcxx": census["measured_max_glibcxx"],
            "measured_max_cxxabi": census["measured_max_cxxabi"],
            "measured_max_gcc": census["measured_max_gcc"],
        }
        (output / f"{asset_name}.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (output / f"{asset_name}.sha256").write_text(f"{archive_sha}  {asset_name}\n", encoding="utf-8")
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
