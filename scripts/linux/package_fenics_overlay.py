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
STOCK_PYTHON_VERSION = "3.12"
TARGET_PYTHON_VERSION = "3.14"
TARGET_PYTHON_PATCH = "3.14.10"
TARGET_PYTHON_ABI = "cp314"
STOCK_PYTHON_SITE = PurePosixPath(f"lib/python{STOCK_PYTHON_VERSION}/site-packages")
TARGET_PYTHON_SITE = PurePosixPath(f"lib/python{TARGET_PYTHON_VERSION}/site-packages")
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
    f"{STOCK_PYTHON_SITE.as_posix()}/numpy/",
    f"{STOCK_PYTHON_SITE.as_posix()}/numpy.libs/",
    f"{TARGET_PYTHON_SITE.as_posix()}/numpy/",
    f"{TARGET_PYTHON_SITE.as_posix()}/numpy.libs/",
)
GCC_SPECS_RELATIVE = PurePosixPath("lib/gcc/x86_64-conda-linux-gnu/16.2.0/conda.specs")
GCC_SPECS_PREFIX_TOKEN = "@FENICS_PRIVATE_PREFIX@"
RUNTIME_PREFIX_TOKEN = "@FENICS_RUNTIME_PREFIX@"
REQUIRED_SYMBOL_CEILINGS = {
    "GLIBC": (2, 17),
    "GLIBCXX": (3, 4, 19),
    "CXXABI": (1, 3, 7),
    "GCC": (4, 8, 0),
}
ALLOWED_NONNUMERIC_VERSION_TOKENS = {"CXXABI_TM_1"}
SYSROOT_ALLOWED_NONNUMERIC_VERSION_TOKENS = {"GLIBC_PRIVATE"}
PROTECTED_JIT_BIN_PATHS = {
    "bin/x86_64-conda-linux-gnu-gcc",
    "bin/x86_64-conda-linux-gnu-ld",
    "bin/x86_64-conda-linux-gnu-as",
    "bin/x86_64-conda-linux-gnu-ar",
    "bin/x86_64-conda-linux-gnu-ranlib",
    "bin/x86_64-conda-linux-gnu.cfg",
    "bin/gcc",
    "bin/cc",
}
NATIVE_HANDOFF_FORMAT_VERSION = 1
NATIVE_HANDOFF_KIND = "linux-fenics-native-rebuild-handoff"
NATIVE_HANDOFF_STATUS = "candidate-unqualified"
NATIVE_HANDOFF_OMISSIONS = ("kahip", "libadios2", "zeromq")
NATIVE_HANDOFF_PROVIDER_OWNER = "python-numpy-fenics-native-provider"
NATIVE_HANDOFF_REPLACEMENT_OWNERS = {
    "fmt",
    "spdlog",
    "pugixml",
    "libxml2-16",
    "fenics-libbasix",
    "fenics-basix",
    "fenics-libdolfinx",
    "fenics-dolfinx",
    NATIVE_HANDOFF_PROVIDER_OWNER,
}
NATIVE_HANDOFF_WHEEL_ROOTS = {
    "fenics-basix": ("basix", "fenics_basix-0.11.0.dist-info"),
    "fenics-dolfinx": ("dolfinx", "fenics_dolfinx-0.11.0.dist-info"),
}
NATIVE_HANDOFF_SOURCE_COMPONENTS = ("basix", "dolfinx", "fmt", "spdlog", "pugixml", "libxml2")
NATIVE_HANDOFF_SOURCE_MANIFEST = "locks/linux/linux-x86_64-fenics-rebuild-sources.json"
NATIVE_HANDOFF_EXPECTED_STOCK_VIOLATING_PACKAGES = (
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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_relative(raw: str) -> PurePosixPath:
    value = raw.replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "" in path.parts:
        raise SystemExit(f"unsafe package-owned path: {raw!r}")
    return path


def canonical_json_sha(value: object) -> str:
    return sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise SystemExit(f"{label} must be a lowercase SHA-256")
    return value


def safe_handoff_relative(raw: object, label: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise SystemExit(f"{label} must be a canonical POSIX relative path")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != raw:
        raise SystemExit(f"{label} must be a canonical POSIX relative path: {raw!r}")
    return path


def handoff_forbidden_destination(relative: PurePosixPath) -> bool:
    text = relative.as_posix()
    if numpy_owned_path(relative):
        return True
    if re.match(r"^bin/(?:python|pip)(?:3(?:\.(?:12|14))?)?(?:$|[.-])", text):
        return True
    for version in (STOCK_PYTHON_VERSION, TARGET_PYTHON_VERSION):
        site = f"lib/python{version}/site-packages/"
        if text.startswith(f"lib/python{version}/") and not text.startswith(site):
            return True
        if text.startswith(f"include/python{version}"):
            return True
        escaped = re.escape(version)
        if re.match(rf"^lib/libpython{escaped}(?:\..*)?$", text):
            return True
    if text.startswith("conda-meta/"):
        return True
    return False


def staged_runtime_relative(relative: PurePosixPath) -> PurePosixPath:
    """Map immutable cp312 package paths into the cp314 overlay layout."""

    try:
        tail = relative.relative_to(STOCK_PYTHON_SITE)
    except ValueError:
        return relative
    return TARGET_PYTHON_SITE / tail


def staged_locked_package_relative(
    relative: PurePosixPath,
    lock_record: dict[str, Any],
    *,
    native_rebuild_active: bool,
) -> PurePosixPath:
    """Translate only target-independent Python payload into the cp314 layout."""

    translated = staged_runtime_relative(relative)
    if translated == relative or str(lock_record.get("subdir")) == "noarch":
        return translated
    if native_rebuild_active:
        raise SystemExit(
            "cp314 overlay cannot relabel target-specific cp312 Python payload without an "
            f"explicit cp314 replacement: {lock_record.get('name')}: {relative}"
        )
    # Stock-preflight staging preserves the historical path. If its ABI audit
    # unexpectedly passes, main() still rejects the stage before archiving.
    return relative


def target_wheel_owned_path(owner: str, relative: PurePosixPath) -> bool:
    roots = NATIVE_HANDOFF_WHEEL_ROOTS.get(owner)
    if roots is None:
        return False
    try:
        tail = relative.relative_to(TARGET_PYTHON_SITE)
    except ValueError:
        return False
    return bool(tail.parts) and tail.parts[0] in roots


def reject_handoff_symlink_components(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SystemExit(f"native rebuild handoff payload traverses a symlink: {relative}")
    return current


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


def validate_base_runtime(
    base: Path,
    qualification_path: Path,
    expected_producer_revision: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_producer_revision):
        raise SystemExit(
            "expected public NumPy base producer revision must be a full lowercase Git SHA"
        )
    manifest_path = base / "runtime-manifest.json"
    core_provenance_path = base / "build-provenance-linux.json"
    provenance_path = base / "build-provenance-linux-numpy.json"
    numpy_payload = base / "numpy-payload-manifest-linux.json"
    for required in (
        manifest_path,
        core_provenance_path,
        provenance_path,
        numpy_payload,
        base / "bin" / f"python{TARGET_PYTHON_VERSION}",
    ):
        if not required.exists():
            raise SystemExit(f"qualified public NumPy base input is missing: {required}")
    manifest = read_json(manifest_path)
    if (
        manifest.get("format_version") != 1
        or manifest.get("platform") != "linux-x64"
        or manifest.get("profile") != "numpy"
        or manifest.get("python_abi") != TARGET_PYTHON_ABI
        or manifest.get("producer", {}).get("repository") != "precsim/python-numpy-fenics"
        or manifest.get("producer", {}).get("revision") != expected_producer_revision
    ):
        raise SystemExit(f"invalid public NumPy base runtime manifest: {manifest_path}")
    tcl_tk = manifest.get("tcl_tk")
    if (
        not isinstance(tcl_tk, dict)
        or tcl_tk.get("version") != "8.6.18"
        or tcl_tk.get("ownership") != "producer"
        or not isinstance(tcl_tk.get("provenance_path"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(tcl_tk.get("provenance_sha256", "")))
    ):
        raise SystemExit("qualified NumPy base does not prove producer-owned Tcl/Tk 8.6.18")
    tcl_tk_provenance_relative = safe_relative(str(tcl_tk["provenance_path"]))
    tcl_tk_provenance = base / Path(*tcl_tk_provenance_relative.parts)
    if (
        not tcl_tk_provenance.is_file()
        or tcl_tk_provenance.is_symlink()
        or sha256_file(tcl_tk_provenance) != tcl_tk["provenance_sha256"]
    ):
        raise SystemExit("qualified NumPy base Tcl/Tk provenance identity does not match the runtime tree")
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
        or qualification.get("producer_revision") != expected_producer_revision
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
    if core_provenance.get("python", {}).get("version") != TARGET_PYTHON_PATCH:
        raise SystemExit(f"public NumPy base provenance does not identify CPython {TARGET_PYTHON_PATCH}")
    site = base.joinpath(*TARGET_PYTHON_SITE.parts)
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
        "producer_revision": expected_producer_revision,
        "tcl_tk": tcl_tk,
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
        re.match(
            rf"^lib/python(?:{re.escape(STOCK_PYTHON_VERSION)}|{re.escape(TARGET_PYTHON_VERSION)})/"
            r"site-packages/numpy-[^/]+\.dist-info(?:/|$)",
            text,
        )
    )


def path_exclusion_reason(relative: PurePosixPath) -> str | None:
    text = relative.as_posix()
    if "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}:
        return "generated-python-bytecode"
    if text.startswith("etc/conda/test-files/"):
        return "conda-package-test-fixture"
    if text in {
        f"{STOCK_PYTHON_SITE.as_posix()}/petsc4py/lib/petsc.cfg",
        f"{TARGET_PYTHON_SITE.as_posix()}/petsc4py/lib/petsc.cfg",
    }:
        return "runtime-derived-petsc4py-config"
    if text.startswith(("lib/pkgconfig/", "lib/cmake/", "share/cmake/", "share/pkgconfig/")):
        return "developer-metadata"
    if text.startswith(("lib/petsc/conf/", "lib/petsc/bin/")):
        return "petsc-build-metadata"
    # These package extras are outside the qualified serial DOLFINx/PETSc and
    # fresh-FFCx-JIT closure.  Keeping them would expand the shipped C++ ABI
    # surface without providing a runtime capability covered by this profile.
    if re.fullmatch(r"lib/libboost_[^/]+\.so(?:\..*)?", text):
        return "unused-boost-shared-library"
    if re.fullmatch(r"lib/libhdf5_cpp\.so(?:\..*)?", text):
        return "unused-hdf5-cxx-binding"
    if text == "lib/libmpichcxx.so" or re.fullmatch(r"lib/libmpicxx\.so(?:\..*)?", text):
        return "unused-mpich-cxx-binding"
    if text in {"bin/nghttp", "bin/nghttpd", "bin/nghttpx"}:
        return "unused-nghttp2-cli"
    if text == "bin/io_demo" or re.fullmatch(r"lib/ucx/libucx_perftest_cuda\.so(?:\..*)?", text):
        return "unused-ucx-test-tool"
    if re.fullmatch(r"lib/libcfp\.so(?:\..*)?", text):
        return "unused-zfp-cxx-wrapper"
    if re.fullmatch(r"lib/libcc1\.so(?:\..*)?", text) or text == (
        "lib/gcc/x86_64-conda-linux-gnu/16.2.0/libcc1.so"
    ) or re.fullmatch(
        r"lib/gcc/x86_64-conda-linux-gnu/16\.2\.0/plugin/libc(?:c1|p1)plugin\.so(?:\..*)?",
        text,
    ):
        return "unused-gcc-debugger-plugin"
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


def validate_stock_preflight_for_handoff(
    path: Path, lock_sha256: str, prefix_verification_sha256: str
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"native rebuild handoff requires regular stock ABI preflight evidence: {path}")
    report = read_json(path)
    if (
        report.get("format_version") != 1
        or report.get("stage") != "stock-binary-closure-before-selective-rebuild"
        or report.get("status") != "fail"
        or report.get("source_rebuilds_authorized") is not False
        or report.get("package_lock_sha256") != lock_sha256
        or report.get("prefix_verification_sha256") != prefix_verification_sha256
    ):
        raise SystemExit(f"invalid stock ABI preflight evidence for native rebuild handoff: {path}")
    fingerprint = require_sha256(
        report.get("selection_fingerprint_sha256"), "stock ABI selection fingerprint"
    )
    fingerprint_payload = dict(report)
    fingerprint_payload.pop("selection_fingerprint_sha256", None)
    if canonical_json_sha(fingerprint_payload) != fingerprint:
        raise SystemExit("stock ABI preflight selection fingerprint mismatch")
    violating = report.get("violating_packages")
    if violating != list(NATIVE_HANDOFF_EXPECTED_STOCK_VIOLATING_PACKAGES):
        raise SystemExit("stock ABI preflight violating package set differs from the fixed selective-rebuild boundary")
    return report


def load_native_rebuild_handoff(
    path: Path,
    *,
    prefix: Path,
    lock_path: Path,
    prefix_evidence_path: Path,
    stock_abi_preflight_path: Path,
    records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise SystemExit(f"native rebuild handoff is missing or is not a regular file: {path}")
    payload = read_json(path)
    if set(payload) != {
        "format_version",
        "kind",
        "status",
        "source_rebuild_provenance_eligible",
        "inputs",
        "conditional_omissions",
        "replacements",
        "qualification",
    }:
        raise SystemExit("native rebuild handoff has unexpected or missing top-level fields")
    if (
        payload.get("format_version") != NATIVE_HANDOFF_FORMAT_VERSION
        or payload.get("kind") != NATIVE_HANDOFF_KIND
        or payload.get("status") != NATIVE_HANDOFF_STATUS
        or payload.get("source_rebuild_provenance_eligible") is not False
    ):
        raise SystemExit(f"unsupported native rebuild handoff identity: {path}")
    qualification = payload.get("qualification")
    if qualification != {
        "full_native_closure": "not-run",
        "hosted_linux_glibc217": "not-run",
        "jit": "not-run",
        "poisson": "not-run",
        "relocation": "not-run",
    }:
        raise SystemExit("native rebuild handoff must remain explicitly unqualified")

    inputs = payload.get("inputs")
    if not isinstance(inputs, dict):
        raise SystemExit("native rebuild handoff has no inputs object")
    if set(inputs) != {
        "package_lock_sha256",
        "prefix_verification_sha256",
        "stock_abi_preflight_sha256",
        "stock_abi_selection_fingerprint_sha256",
        "source_manifest_sha256",
        "source_sha256",
        "build_tool_explicit_sha256",
        "python_builder_manifest_sha256",
        "libxml2_build_deps_manifest_sha256",
        "private_provider_manifest_sha256",
        "private_provider_library_sha256",
        "native_closure_report_sha256",
        "native_closure",
    }:
        raise SystemExit("native rebuild handoff inputs object has unexpected or missing fields")
    lock_sha = sha256_file(lock_path)
    prefix_evidence_sha = sha256_file(prefix_evidence_path)
    if require_sha256(inputs.get("package_lock_sha256"), "handoff package lock sha256") != lock_sha:
        raise SystemExit("native rebuild handoff package-lock identity mismatch")
    if require_sha256(inputs.get("prefix_verification_sha256"), "handoff prefix verification sha256") != prefix_evidence_sha:
        raise SystemExit("native rebuild handoff prefix-verification identity mismatch")
    stock_preflight = validate_stock_preflight_for_handoff(
        stock_abi_preflight_path, lock_sha, prefix_evidence_sha
    )
    if require_sha256(inputs.get("stock_abi_preflight_sha256"), "handoff stock ABI preflight sha256") != sha256_file(stock_abi_preflight_path):
        raise SystemExit("native rebuild handoff stock ABI preflight identity mismatch")
    if require_sha256(inputs.get("stock_abi_selection_fingerprint_sha256"), "handoff stock ABI selection fingerprint") != stock_preflight["selection_fingerprint_sha256"]:
        raise SystemExit("native rebuild handoff stock ABI selection fingerprint mismatch")

    for key in (
        "build_tool_explicit_sha256",
        "python_builder_manifest_sha256",
        "libxml2_build_deps_manifest_sha256",
        "private_provider_manifest_sha256",
        "private_provider_library_sha256",
        "native_closure_report_sha256",
    ):
        require_sha256(inputs.get(key), f"handoff {key}")
    checked_source_manifest_path = Path(__file__).resolve().parents[2] / NATIVE_HANDOFF_SOURCE_MANIFEST
    checked_source_manifest_sha = sha256_file(checked_source_manifest_path)
    if require_sha256(inputs.get("source_manifest_sha256"), "handoff source manifest sha256") != checked_source_manifest_sha:
        raise SystemExit("native rebuild handoff source-manifest identity mismatch")
    checked_source_manifest = read_json(checked_source_manifest_path)
    checked_sources = checked_source_manifest.get("sources")
    if not isinstance(checked_sources, dict):
        raise SystemExit("checked-in native rebuild source manifest has no sources object")
    source_hashes = inputs.get("source_sha256")
    if not isinstance(source_hashes, dict) or tuple(sorted(source_hashes)) != tuple(sorted(NATIVE_HANDOFF_SOURCE_COMPONENTS)):
        raise SystemExit("native rebuild handoff source hash set differs from the fixed selective-rebuild inputs")
    for component in NATIVE_HANDOFF_SOURCE_COMPONENTS:
        actual = require_sha256(source_hashes.get(component), f"handoff source {component} sha256")
        expected = require_sha256(
            checked_sources.get(component, {}).get("sha256") if isinstance(checked_sources.get(component), dict) else None,
            f"checked-in source {component} sha256",
        )
        if actual != expected:
            raise SystemExit(f"native rebuild handoff source hash mismatch for {component}")
    native_closure = inputs.get("native_closure")
    if not isinstance(native_closure, dict) or set(native_closure) != {
        "dolfinx_build_flags",
        "closure_elf_count",
        "icu_reached_paths",
        "icu_resolution_required",
        "candidate_output_binding_sha256",
    } or native_closure.get("dolfinx_build_flags") != {
        "DOLFINX_ENABLE_ADIOS2": "OFF",
        "DOLFINX_ENABLE_KAHIP": "OFF",
    }:
        raise SystemExit("native rebuild handoff does not bind the required DOLFINx omission flags")
    if not isinstance(native_closure.get("icu_reached_paths"), list) or not isinstance(
        native_closure.get("icu_resolution_required"), bool
    ):
        raise SystemExit("native rebuild handoff has invalid ICU closure evidence")
    candidate_output_binding_sha = require_sha256(
        native_closure.get("candidate_output_binding_sha256"),
        "handoff native closure candidate output binding sha256",
    )

    omissions = payload.get("conditional_omissions")
    if not isinstance(omissions, list) or [item.get("package") if isinstance(item, dict) else None for item in omissions] != list(NATIVE_HANDOFF_OMISSIONS):
        raise SystemExit("native rebuild handoff conditional omission set is not the fixed KaHIP/ADIOS2/ZeroMQ set")
    closure_sha = inputs["native_closure_report_sha256"]
    for item in omissions:
        assert isinstance(item, dict)
        if set(item) != {
            "package",
            "native_closure_report_sha256",
            "candidate_output_binding_sha256",
            "evidence",
            "evidence_sha256",
        }:
            raise SystemExit("native rebuild handoff omission record has unexpected or missing fields")
        name = str(item["package"])
        if item.get("native_closure_report_sha256") != closure_sha:
            raise SystemExit(f"native rebuild handoff omission is not bound to closure evidence: {name}")
        if item.get("candidate_output_binding_sha256") != candidate_output_binding_sha:
            raise SystemExit(f"native rebuild handoff omission is not bound to candidate outputs: {name}")
        evidence = item.get("evidence")
        if not isinstance(evidence, dict) or set(evidence) != {
            "reached_paths",
            "reverse_direct_noncandidate",
            "staged_paths",
            "abi_maxima",
        } or evidence.get("reached_paths") != []:
            raise SystemExit(f"native rebuild handoff omission has reached paths: {name}")
        reverse = evidence.get("reverse_direct_noncandidate")
        staged_paths = evidence.get("staged_paths")
        if (
            not isinstance(reverse, list)
            or any(not isinstance(entry, dict) or entry.get("in_closure") is not False for entry in reverse)
            or not isinstance(staged_paths, list)
            or any(not isinstance(entry, str) for entry in staged_paths)
        ):
            raise SystemExit(f"native rebuild handoff omission evidence is invalid: {name}")
        normalized_staged_paths = [
            safe_handoff_relative(entry, f"handoff omission {name} staged path").as_posix()
            for entry in staged_paths
        ]
        if staged_paths != sorted(normalized_staged_paths):
            raise SystemExit(f"native rebuild handoff omission staged paths are not canonical and sorted: {name}")
        evidence_sha = require_sha256(item.get("evidence_sha256"), f"handoff omission {name} evidence sha256")
        if canonical_json_sha(evidence) != evidence_sha:
            raise SystemExit(f"native rebuild handoff omission evidence hash mismatch: {name}")

    raw_replacements = payload.get("replacements")
    if not isinstance(raw_replacements, list) or not raw_replacements:
        raise SystemExit("native rebuild handoff has no replacements")
    handoff_root = path.parent.resolve()
    replacements: list[dict[str, Any]] = []
    destinations: list[str] = []
    owned_cache: dict[str, set[str]] = {}
    provider_seen = False
    required_keys = {"destination", "payload", "owner", "sha256", "bytes", "mode", "replaces_sha256"}
    for item in raw_replacements:
        if not isinstance(item, dict) or set(item) != required_keys:
            raise SystemExit(f"invalid native rebuild replacement record: {item!r}")
        destination_relative = safe_handoff_relative(item.get("destination"), "handoff replacement destination")
        destination_text = destination_relative.as_posix()
        if handoff_forbidden_destination(destination_relative):
            raise SystemExit(f"native rebuild handoff attempts CPython/NumPy ownership: {destination_text}")
        if path_exclusion_reason(destination_relative) is not None:
            raise SystemExit(f"native rebuild handoff replacement is outside staged runtime policy: {destination_text}")
        owner = item.get("owner")
        if owner not in NATIVE_HANDOFF_REPLACEMENT_OWNERS:
            raise SystemExit(f"native rebuild handoff has unexpected replacement owner: {owner!r}")
        payload_relative = safe_handoff_relative(item.get("payload"), "handoff replacement payload")
        expected_payload = PurePosixPath("payload") / destination_relative
        if payload_relative != expected_payload:
            raise SystemExit(f"native rebuild handoff payload path is not deterministic for {destination_text}")
        source = reject_handoff_symlink_components(handoff_root, payload_relative)
        try:
            source.resolve(strict=True).relative_to(handoff_root)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"native rebuild handoff payload escapes its root: {payload_relative}") from exc
        if not source.is_file() or source.is_symlink():
            raise SystemExit(f"native rebuild handoff payload is not a regular file: {payload_relative}")
        expected_sha = require_sha256(item.get("sha256"), f"handoff replacement {destination_text} sha256")
        if sha256_file(source) != expected_sha or source.stat().st_size != item.get("bytes"):
            raise SystemExit(f"native rebuild handoff payload identity mismatch: {destination_text}")
        mode = item.get("mode")
        if not isinstance(mode, int) or mode < 0 or mode > 0o777 or stat.S_IMODE(source.stat().st_mode) != mode:
            raise SystemExit(f"native rebuild handoff payload mode mismatch: {destination_text}")

        if owner == NATIVE_HANDOFF_PROVIDER_OWNER:
            if destination_text != "lib/libfenics_cxxrt.so.1" or item.get("replaces_sha256") is not None:
                raise SystemExit("native rebuild private provider has an invalid replacement contract")
            provider_seen = True
        else:
            if owner in ALWAYS_EXCLUDED_PACKAGES or owner not in records:
                raise SystemExit(f"native rebuild handoff cannot assign replacement ownership to {owner}")
            if owner not in owned_cache:
                owned_cache[owner] = {entry["relative"].as_posix() for entry in package_owned_paths(records[owner])}
            if destination_text in owned_cache[owner]:
                stock = prefix / Path(*destination_relative.parts)
                if not stock.is_file() or stock.is_symlink():
                    raise SystemExit(f"native rebuild replacement stock destination is not a regular file: {owner}: {destination_text}")
                replaces_sha = require_sha256(
                    item.get("replaces_sha256"), f"handoff replacement {destination_text} stock sha256"
                )
                if sha256_file(stock) != replaces_sha:
                    raise SystemExit(f"native rebuild replacement stock identity mismatch: {owner}: {destination_text}")
            elif target_wheel_owned_path(str(owner), destination_relative):
                if item.get("replaces_sha256") is not None:
                    raise SystemExit(
                        f"cp314 wheel replacement must not claim an exact cp312 stock-file hash: {destination_text}"
                    )
                if not any(
                    target_wheel_owned_path(str(owner), staged_runtime_relative(PurePosixPath(stock_path)))
                    for stock_path in owned_cache[owner]
                ):
                    raise SystemExit(f"cp314 wheel replacement owner has no bound cp312 stock payload: {owner}")
            else:
                raise SystemExit(f"native rebuild replacement is not stock-owned by {owner}: {destination_text}")
        destinations.append(destination_text)
        replacements.append(dict(item) | {"source": source, "relative": destination_relative})

    if destinations != sorted(destinations) or len(destinations) != len(set(destinations)):
        raise SystemExit("native rebuild handoff replacements must be uniquely sorted by destination")
    if not provider_seen:
        raise SystemExit("native rebuild handoff is missing the private C++ provider replacement")
    provider_record = next(item for item in replacements if item["owner"] == NATIVE_HANDOFF_PROVIDER_OWNER)
    if inputs["private_provider_library_sha256"] != provider_record["sha256"]:
        raise SystemExit("native rebuild handoff provider input hash differs from staged provider bytes")

    return {
        "manifest": payload,
        "manifest_sha256": sha256_file(path),
        "replacements": replacements,
        "replacement_destinations": set(destinations),
        "omitted_packages": set(NATIVE_HANDOFF_OMISSIONS),
        "stock_preflight": stock_preflight,
    }


def stage_native_rebuild_replacements(
    stage_root: Path,
    private_prefix: Path,
    handoff: dict[str, Any],
    owners: dict[str, list[str]],
) -> list[dict[str, Any]]:
    staged: list[dict[str, Any]] = []
    for item in handoff["replacements"]:
        relative: PurePosixPath = item["relative"]
        destination = private_prefix / Path(*relative.parts)
        if destination.exists() or destination.is_symlink():
            raise SystemExit(f"native rebuild replacement destination was already staged: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item["source"], destination, follow_symlinks=False)
        destination.chmod(int(item["mode"]))
        if destination.is_symlink() or sha256_file(destination) != item["sha256"]:
            raise SystemExit(f"native rebuild replacement changed while staging: {relative}")
        staged_rel = destination.relative_to(stage_root).as_posix()
        owners[staged_rel] = [str(item["owner"])]
        staged.append({key: item[key] for key in required_handoff_stage_fields()})
    return staged


def required_handoff_stage_fields() -> tuple[str, ...]:
    return ("destination", "owner", "sha256", "bytes", "mode", "replaces_sha256")


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
    seen: set[Path] = set()

    def matching(candidate: Path) -> Path | None:
        try:
            candidate = candidate.resolve()
        except OSError:
            return None
        if candidate in seen:
            return None
        seen.add(candidate)
        index = candidate / "info" / "index.json"
        if not index.is_file():
            return None
        try:
            meta = read_json(index)
        except Exception:
            return None
        actual = (str(meta.get("name")), str(meta.get("version")), str(meta.get("build")))
        if actual == identity:
            return candidate
        return None

    for candidate in candidates:
        match = matching(candidate)
        if match is not None:
            return match
    for index in package_cache.rglob("info/index.json"):
        match = matching(index.parent.parent)
        if match is not None:
            return match
    return None


def cache_path_to_installed_path(cache_relative: PurePosixPath, link_metadata: dict[str, Any]) -> PurePosixPath:
    noarch = link_metadata.get("noarch")
    if not isinstance(noarch, dict) or noarch.get("type") != "python":
        return cache_relative
    text = cache_relative.as_posix()
    if text.startswith("site-packages/"):
        return safe_relative(f"{STOCK_PYTHON_SITE.as_posix()}/" + text.removeprefix("site-packages/"))
    if text.startswith("python-scripts/"):
        return safe_relative("bin/" + text.removeprefix("python-scripts/"))
    return cache_relative


def package_cache_prefix_entries(
    package_cache: Path, record: dict[str, Any]
) -> list[dict[str, Any]]:
    name = str(record["name"])
    cache_record = find_cache_record(package_cache, record)
    if cache_record is None:
        raise SystemExit(f"package cache is missing retained package metadata: {name}")
    paths_path = cache_record / "info" / "paths.json"
    if not paths_path.is_file():
        raise SystemExit(f"package cache is missing paths.json metadata: {name}: {paths_path}")
    paths_metadata = read_json(paths_path)
    raw_paths = paths_metadata.get("paths")
    if paths_metadata.get("paths_version") != 1 or not isinstance(raw_paths, list):
        raise SystemExit(f"unsupported package paths.json metadata: {name}: {paths_path}")
    link_path = cache_record / "info" / "link.json"
    link_metadata = read_json(link_path) if link_path.is_file() else {}
    if not isinstance(link_metadata, dict):
        raise SystemExit(f"invalid package link.json metadata: {name}: {link_path}")

    installed_by_path = {
        item["relative"].as_posix(): item for item in package_owned_paths(record)
    }
    mapped_paths: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in raw_paths:
        if not isinstance(item, dict) or not item.get("_path"):
            raise SystemExit(f"invalid package paths.json entry: {name}: {item!r}")
        cache_relative = safe_relative(str(item["_path"]))
        installed_relative = cache_path_to_installed_path(cache_relative, link_metadata)
        installed_text = installed_relative.as_posix()
        if installed_text in mapped_paths:
            raise SystemExit(
                f"package cache paths map ambiguously after noarch relocation: {name}: {installed_text}"
            )
        mapped_paths.add(installed_text)

        placeholder = item.get("prefix_placeholder")
        file_mode = item.get("file_mode")
        if placeholder is None and file_mode is None:
            continue
        if not isinstance(placeholder, str) or not placeholder or file_mode not in {"text", "binary"}:
            raise SystemExit(
                f"invalid prefix replacement metadata: {name}: {cache_relative}: "
                f"placeholder={placeholder!r} mode={file_mode!r}"
            )
        if "\x00" in placeholder:
            raise SystemExit(f"prefix placeholder contains NUL: {name}: {cache_relative}")
        if item.get("path_type") != "hardlink":
            raise SystemExit(
                f"prefix-rewritten cache payload is not a hardlink file: {name}: {cache_relative}: "
                f"{item.get('path_type')!r}"
            )
        installed = installed_by_path.get(installed_text)
        if installed is None:
            raise SystemExit(
                f"prefix-rewritten cache payload has no installed package ownership: {name}: "
                f"{cache_relative} -> {installed_text}"
            )
        if installed.get("path_type") != "hardlink":
            raise SystemExit(
                f"prefix-rewritten installed payload is not a hardlink file: {name}: {installed_text}: "
                f"{installed.get('path_type')!r}"
            )
        cache_sha = str(item.get("sha256") or "").lower()
        installed_source_sha = str(installed.get("sha256") or "").lower()
        installed_prefix_sha = str(installed.get("sha256_in_prefix") or "").lower()
        for label, value in (
            ("cache sha256", cache_sha),
            ("installed source sha256", installed_source_sha),
            ("installed sha256_in_prefix", installed_prefix_sha),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise SystemExit(
                    f"prefix-rewritten payload has invalid {label}: {name}: {installed_text}: {value!r}"
                )
        if cache_sha != installed_source_sha:
            raise SystemExit(
                f"cache and installed source hashes differ for prefix-rewritten payload: {name}: "
                f"{installed_text}: {cache_sha} != {installed_source_sha}"
            )
        result.append(
            {
                "package": name,
                "cache_record": cache_record,
                "cache_relative": cache_relative,
                "installed_relative": installed_relative,
                "file_mode": file_mode,
                "prefix_placeholder": placeholder,
                "cache_sha256": cache_sha,
                "installed_sha256_in_prefix": installed_prefix_sha,
            }
        )
    return result


def replace_conda_prefix(
    data: bytes, placeholder: bytes, replacement: bytes, file_mode: str
) -> tuple[bytes, int]:
    if not placeholder or b"\x00" in placeholder or b"\x00" in replacement:
        raise SystemExit("conda prefix replacement requires nonempty NUL-free byte strings")
    if file_mode == "text":
        count = data.count(placeholder)
        return data.replace(placeholder, replacement), count
    if file_mode != "binary":
        raise SystemExit(f"unsupported conda prefix replacement mode: {file_mode!r}")
    if len(replacement) > len(placeholder):
        raise SystemExit(
            f"binary conda prefix replacement is longer than its placeholder: "
            f"{len(replacement)} > {len(placeholder)}"
        )

    count = 0
    rewritten = bytearray()
    start = 0
    while True:
        end = data.find(b"\x00", start)
        terminated = end >= 0
        if not terminated:
            end = len(data)
        segment = data[start:end]
        occurrences = segment.count(placeholder)
        if occurrences:
            replaced = segment.replace(placeholder, replacement)
            if len(replaced) > len(segment):
                raise SystemExit("binary conda prefix replacement expanded a fixed-width string")
            rewritten.extend(replaced)
            rewritten.extend(b"\x00" * (len(segment) - len(replaced)))
            count += occurrences
        else:
            rewritten.extend(segment)
        if not terminated:
            break
        rewritten.append(0)
        start = end + 1
    if len(rewritten) != len(data):
        raise SystemExit("binary conda prefix replacement changed payload size")
    return bytes(rewritten), count


def relocate_verified_prefix_file(
    stage_root: Path,
    private_prefix: Path,
    source_prefix: Path,
    metadata: dict[str, Any],
    replacement_token: str,
    staged_relative: PurePosixPath | None = None,
) -> dict[str, Any]:
    name = str(metadata["package"])
    installed_relative: PurePosixPath = metadata["installed_relative"]
    cache_relative: PurePosixPath = metadata["cache_relative"]
    cache_record: Path = metadata["cache_record"]
    file_mode = str(metadata["file_mode"])
    placeholder = str(metadata["prefix_placeholder"]).encode("utf-8")
    replacement = replacement_token.encode("ascii")
    source_replacement = str(source_prefix).encode("utf-8")
    cache_payload = cache_record / Path(*cache_relative.parts)
    if not cache_payload.is_file() or cache_payload.is_symlink():
        raise SystemExit(f"prefix-rewritten cache payload is missing or not regular: {name}: {cache_payload}")
    cache_sha = sha256_file(cache_payload)
    if cache_sha != metadata["cache_sha256"]:
        raise SystemExit(
            f"prefix-rewritten cache payload hash mismatch: {name}: {cache_relative}: "
            f"{cache_sha} != {metadata['cache_sha256']}"
        )

    source = source_prefix / Path(*installed_relative.parts)
    if not source.is_file() or source.is_symlink():
        raise SystemExit(f"installed prefix-rewritten payload is missing or not regular: {name}: {source}")
    source_sha = sha256_file(source)
    if source_sha != metadata["installed_sha256_in_prefix"]:
        raise SystemExit(
            f"installed prefix-rewritten payload hash mismatch: {name}: {installed_relative}: "
            f"{source_sha} != {metadata['installed_sha256_in_prefix']}"
        )

    raw = cache_payload.read_bytes()
    reproduced, source_occurrences = replace_conda_prefix(raw, placeholder, source_replacement, file_mode)
    if source_occurrences == 0:
        raise SystemExit(
            f"declared prefix placeholder does not occur in cache payload: {name}: {cache_relative}"
        )
    reproduced_sha = sha256_bytes(reproduced)
    if reproduced_sha != metadata["installed_sha256_in_prefix"]:
        raise SystemExit(
            f"cache metadata cannot reproduce installed prefix bytes: {name}: {installed_relative}: "
            f"{reproduced_sha} != {metadata['installed_sha256_in_prefix']}"
        )

    staged_relative = staged_relative or staged_runtime_relative(installed_relative)
    destination = private_prefix / Path(*staged_relative.parts)
    if not destination.is_file() or destination.is_symlink():
        raise SystemExit(f"staged prefix-rewritten payload is missing or not regular: {name}: {destination}")
    staged_before_sha = sha256_file(destination)
    if staged_before_sha != metadata["installed_sha256_in_prefix"]:
        raise SystemExit(
            f"staged payload changed before prefix relocation: {name}: {installed_relative}: "
            f"{staged_before_sha} != {metadata['installed_sha256_in_prefix']}"
        )
    relocated, relocated_occurrences = replace_conda_prefix(raw, placeholder, replacement, file_mode)
    if relocated_occurrences != source_occurrences:
        raise SystemExit(
            f"prefix relocation occurrence count changed: {name}: {installed_relative}: "
            f"{relocated_occurrences} != {source_occurrences}"
        )
    mode = stat.S_IMODE(destination.stat().st_mode)
    destination.write_bytes(relocated)
    destination.chmod(mode)
    return {
        "package": name,
        "path": destination.relative_to(stage_root).as_posix(),
        "cache_path": cache_relative.as_posix(),
        "mode": file_mode,
        "replacement": replacement_token,
        "occurrences": relocated_occurrences,
        "placeholder_bytes": len(placeholder),
        "cache_sha256": metadata["cache_sha256"],
        "installed_sha256_in_prefix": metadata["installed_sha256_in_prefix"],
        "prefix_relocated_sha256": sha256_bytes(relocated),
    }


def relocate_metadata_prefixes(
    stage_root: Path,
    private_prefix: Path,
    source_prefix: Path,
    package_cache: Path,
    records: dict[str, dict[str, Any]],
    retained_packages: list[str],
    owners: dict[str, list[str]],
    replacement_destinations: set[str] | None = None,
    preserved_stock_python_paths: set[str] | None = None,
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    replacement_destinations = replacement_destinations or set()
    preserved_stock_python_paths = preserved_stock_python_paths or set()
    for name in retained_packages:
        for metadata in package_cache_prefix_entries(package_cache, records[name]):
            installed_relative: PurePosixPath = metadata["installed_relative"]
            translated_relative = staged_runtime_relative(installed_relative)
            staged_relative = (
                installed_relative
                if installed_relative.as_posix() in preserved_stock_python_paths
                else translated_relative
            )
            if translated_relative.as_posix() in replacement_destinations:
                continue
            if (
                name in NATIVE_HANDOFF_WHEEL_ROOTS
                and translated_relative != installed_relative
                and any(
                    target_wheel_owned_path(name, PurePosixPath(destination))
                    for destination in replacement_destinations
                )
            ):
                continue
            if installed_relative == GCC_SPECS_RELATIVE and name == "conda-gcc-specs":
                continue
            staged = private_prefix / Path(*staged_relative.parts)
            staged_rel = staged.relative_to(stage_root).as_posix()
            if name not in owners.get(staged_rel, []):
                if path_exclusion_reason(installed_relative) is not None:
                    continue
                raise SystemExit(
                    f"prefix-rewritten retained path has no staged ownership: {name}: {installed_relative}"
                )
            private_rel = installed_relative.as_posix()
            if private_rel.startswith("bin/") and not protected_jit_bin_path(private_rel) and not is_elf(staged):
                continue
            evidence.append(
                relocate_verified_prefix_file(
                    stage_root,
                    private_prefix,
                    source_prefix,
                    metadata,
                    RUNTIME_PREFIX_TOKEN,
                    staged_relative,
                )
            )
    return evidence


def finalize_prefix_relocation_evidence(
    stage_root: Path, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    finalized = []
    for entry in sorted(entries, key=lambda item: (str(item["path"]), str(item["package"]))):
        path = stage_root / Path(*PurePosixPath(str(entry["path"])).parts)
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"prefix-relocated staged payload disappeared: {entry['path']}")
        finalized.append(dict(entry) | {"final_sha256": sha256_file(path)})
    return {
        "format_version": 1,
        "generic_marker": RUNTIME_PREFIX_TOKEN,
        "entry_count": len(finalized),
        "text_entry_count": sum(item["mode"] == "text" for item in finalized),
        "binary_entry_count": sum(item["mode"] == "binary" for item in finalized),
        "entries": finalized,
    }


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


def write_stock_abi_preflight(
    destination: Path,
    stage_root: Path,
    package_lock_sha256: str,
    prefix_verification_sha256: str,
    maxima: dict[str, tuple[int, ...] | None],
    ceiling_violations: list[dict[str, Any]],
    unexpected_nonnumeric_requirements: list[dict[str, Any]],
    sysroot_nonnumeric_requirements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Persist exact stock ABI selection evidence, even when packaging fails.

    This report describes the immutable stock closure; it never authorizes a
    replacement, certifies a source rebuild, or changes any acceptance gate.
    Paths are stage-relative so the fingerprint survives scratch relocation.
    """
    affected_paths = sorted(
        {str(item["path"]) for item in ceiling_violations + unexpected_nonnumeric_requirements}
    )
    file_hashes = {
        relative: sha256_file(stage_root / Path(*PurePosixPath(relative).parts))
        for relative in affected_paths
    }
    violations = sorted(
        (
            {
                **item,
                "owners": sorted(set(item.get("owners", []))),
                "file_sha256": file_hashes[str(item["path"])],
            }
            for item in ceiling_violations
        ),
        key=lambda item: (item["path"], item["required"], item["ceiling"]),
    )
    unexpected = sorted(
        (
            {
                **item,
                "owners": sorted(set(item.get("owners", []))),
                "file_sha256": file_hashes[str(item["path"])],
            }
            for item in unexpected_nonnumeric_requirements
        ),
        key=lambda item: (item["path"], item["required"]),
    )
    scoped_sysroot = sorted(
        sysroot_nonnumeric_requirements,
        key=lambda item: (item["path"], item["required"]),
    )
    report: dict[str, Any] = {
        "format_version": 1,
        "stage": "stock-binary-closure-before-selective-rebuild",
        "package_lock_sha256": package_lock_sha256,
        "prefix_verification_sha256": prefix_verification_sha256,
        "symbol_ceilings": {
            namespace: version_text(ceiling)
            for namespace, ceiling in REQUIRED_SYMBOL_CEILINGS.items()
        },
        "measured_maximum_versions": {
            namespace: version_text(maxima[namespace]) for namespace in REQUIRED_SYMBOL_CEILINGS
        },
        "status": "fail" if violations or unexpected else "pass",
        "violation_count": len(violations),
        "violating_file_count": len(affected_paths),
        "violating_packages": sorted(
            {owner for item in violations + unexpected for owner in item["owners"]}
        ),
        "ceiling_violations": violations,
        "unexpected_nonnumeric_requirements": unexpected,
        "sysroot_scoped_nonnumeric_requirements": scoped_sysroot,
        "source_rebuilds_authorized": False,
    }
    report["selection_fingerprint_sha256"] = sha256_bytes(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", prefix=f".{destination.name}.",
        suffix=".tmp", dir=destination.parent, delete=False,
    ) as output:
        temporary = Path(output.name)
        json.dump(report, output, indent=2, sort_keys=True)
        output.write("\n")
    os.replace(temporary, destination)
    return report


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
    abi_preflight_path: Path | None = None,
    package_lock_sha256: str = "",
    prefix_verification_sha256: str = "",
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
    sysroot_nonnumeric_requirements: list[dict[str, Any]] = []
    unexpected_nonnumeric_requirements: list[dict[str, Any]] = []
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
                if token in ALLOWED_NONNUMERIC_VERSION_TOKENS:
                    continue
                rel = path.relative_to(stage_root).as_posix()
                requirement = {
                    "path": rel,
                    "owners": owners.get(rel, []),
                    "required": token,
                }
                if (
                    under(path, sysroot)
                    and token in SYSROOT_ALLOWED_NONNUMERIC_VERSION_TOKENS
                    and set(owners.get(rel, [])) == {"sysroot_linux-64"}
                ):
                    sysroot_nonnumeric_requirements.append(requirement)
                else:
                    unexpected_nonnumeric_requirements.append(requirement)
    if abi_preflight_path is not None:
        write_stock_abi_preflight(
            abi_preflight_path,
            stage_root,
            package_lock_sha256,
            prefix_verification_sha256,
            maxima,
            ceiling_violations,
            unexpected_nonnumeric_requirements,
            sysroot_nonnumeric_requirements,
        )
    if unexpected_nonnumeric_requirements:
        raise SystemExit(
            "stock FEniCS package closure requires unsupported nonnumeric ABI symbol versions: "
            + json.dumps(unexpected_nonnumeric_requirements, indent=2, sort_keys=True)
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
        "sysroot_allowed_nonnumeric_required_versions": sorted(
            {item["required"] for item in sysroot_nonnumeric_requirements}
        ),
        "sysroot_nonnumeric_requirements": sysroot_nonnumeric_requirements,
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
    package_cache: Path,
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
    matches = [
        metadata
        for metadata in package_cache_prefix_entries(package_cache, record)
        if metadata["installed_relative"] == GCC_SPECS_RELATIVE
    ]
    if len(matches) != 1:
        raise SystemExit(
            f"conda-gcc-specs cache metadata does not identify exactly one conda.specs template: {len(matches)}"
        )
    relocation = relocate_verified_prefix_file(
        stage_root,
        private_prefix,
        source_prefix,
        matches[0],
        GCC_SPECS_PREFIX_TOKEN,
    )
    return relocation | {
        "sha256": sha256_file(specs),
        "prefix_token": GCC_SPECS_PREFIX_TOKEN,
        "prefix_replacement_count": relocation["occurrences"],
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
    site = base.joinpath(*TARGET_PYTHON_SITE.parts)
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
    parser.add_argument(
        "--base-producer-revision",
        required=True,
        help="full immutable producer revision of the qualified public NumPy base release",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--abi-preflight-evidence",
        help="write deterministic locked-stock native ABI selection evidence before rejecting violations",
    )
    parser.add_argument(
        "--native-rebuild-handoff",
        help="validated unqualified native-rebuild candidate handoff produced by rebuild_fenics_native.py",
    )
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
    native_rebuild_handoff_path = (
        Path(args.native_rebuild_handoff).resolve() if args.native_rebuild_handoff else None
    )
    bootstrap_source = Path(__file__).resolve().parents[2] / "runtime" / "linux" / "fenics_embed_runtime.py"
    archive_helper = Path(__file__).resolve().parents[1] / "common" / "archive_runtime.py"
    for required in (prefix, evidence_path, package_cache, lock_path, base, base_qualification, bootstrap_source, archive_helper):
        if not required.exists():
            raise SystemExit(f"required producer input is missing: {required}")

    lock = load_lock(lock_path)
    records = installed_records(prefix)
    prefix_evidence = verify_external_prefix_evidence(lock, lock_path, evidence_path, records)
    base_info = validate_base_runtime(base, base_qualification, args.base_producer_revision)
    base_site = base.joinpath(*TARGET_PYTHON_SITE.parts)
    numpy_identity = numpy_tree_identity(base)
    native_rebuild_handoff: dict[str, Any] | None = None
    if native_rebuild_handoff_path is not None:
        if args.abi_preflight_evidence is None:
            raise SystemExit("--native-rebuild-handoff requires --abi-preflight-evidence from the immutable stock failure")
        stock_preflight_path = Path(args.abi_preflight_evidence).resolve()
        native_rebuild_handoff = load_native_rebuild_handoff(
            native_rebuild_handoff_path,
            prefix=prefix,
            lock_path=lock_path,
            prefix_evidence_path=evidence_path,
            stock_abi_preflight_path=stock_preflight_path,
            records=records,
        )

    output.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="fenics-linux-overlay-"))
    stage_root = work / "stage"
    private_prefix = stage_root / "fenics-runtime" / "conda"
    stage_root.mkdir(parents=True)
    owners: dict[str, list[str]] = defaultdict(list)
    retained_packages: list[str] = []
    excluded_packages: list[str] = []
    not_staged_packages: list[str] = []
    omitted_packages: list[str] = []
    excluded_owned_paths: list[dict[str, str]] = []
    replacement_destinations = (
        native_rebuild_handoff["replacement_destinations"] if native_rebuild_handoff is not None else set()
    )
    replacement_by_destination = (
        {item["destination"]: item for item in native_rebuild_handoff["replacements"]}
        if native_rebuild_handoff is not None
        else {}
    )
    replacement_owners = {
        str(item["owner"])
        for item in (native_rebuild_handoff["replacements"] if native_rebuild_handoff is not None else [])
    }
    wheel_rebuild_owners = {
        str(item["owner"])
        for item in (native_rebuild_handoff["replacements"] if native_rebuild_handoff is not None else [])
        if target_wheel_owned_path(str(item["owner"]), item["relative"])
    }
    preserved_stock_python_paths: set[str] = set()
    staged_native_replacements: list[dict[str, Any]] = []

    try:
        for lock_record in lock["packages"]:
            name = str(lock_record["name"])
            if native_rebuild_handoff is not None and name in native_rebuild_handoff["omitted_packages"]:
                omitted_packages.append(name)
                continue
            if package_excluded(name, base_site):
                excluded_packages.append(name)
                continue
            record = records[name]
            contributed = name in replacement_owners
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
                translated_relative = staged_runtime_relative(relative)
                if name in wheel_rebuild_owners and translated_relative != relative:
                    excluded_owned_paths.append(
                        {
                            "package": name,
                            "path": relative.as_posix(),
                            "reason": "replaced-by-cp314-source-built-wheel",
                        }
                    )
                    continue
                staged_relative = staged_locked_package_relative(
                    relative,
                    lock_record,
                    native_rebuild_active=native_rebuild_handoff is not None,
                )
                if translated_relative != relative and staged_relative == relative:
                    preserved_stock_python_paths.add(relative.as_posix())
                replacement = replacement_by_destination.get(staged_relative.as_posix())
                if replacement is not None:
                    if replacement["owner"] != name:
                        raise SystemExit(
                            f"native rebuild replacement owner differs from locked stock owner: "
                            f"{staged_relative}: {replacement['owner']} != {name}"
                        )
                    contributed = True
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
                destination = private_prefix / Path(*staged_relative.parts)
                copy_path(source, destination, prefix, private_prefix)
                rel = destination.relative_to(stage_root).as_posix()
                if name not in owners[rel]:
                    owners[rel].append(name)
                contributed = True
            if contributed:
                retained_packages.append(name)
            else:
                not_staged_packages.append(name)

        if native_rebuild_handoff is not None:
            staged_native_replacements = stage_native_rebuild_replacements(
                stage_root, private_prefix, native_rebuild_handoff, owners
            )
            retained_set = set(retained_packages)
            for item in staged_native_replacements:
                owner = str(item["owner"])
                if owner != NATIVE_HANDOFF_PROVIDER_OWNER and owner not in retained_set:
                    raise SystemExit(f"native rebuild replacement owner was not retained from the lock: {owner}")

        bootstrap = stage_root.joinpath(*TARGET_PYTHON_SITE.parts, "fenics_embed_runtime.py")
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
            "bin/x86_64-conda-linux-gnu.cfg": "sysroot_linux-64",
            GCC_SPECS_RELATIVE.as_posix(): "conda-gcc-specs",
        }
        for private_rel, expected_owner in required_jit_owners.items():
            staged_rel = (Path("fenics-runtime") / "conda" / Path(private_rel)).as_posix()
            if expected_owner not in owners.get(staged_rel, []):
                raise SystemExit(
                    f"required JIT tool path has unexpected package ownership: {staged_rel}: "
                    f"expected {expected_owner}, got {owners.get(staged_rel)}"
                )

        prefix_relocation_entries = relocate_metadata_prefixes(
            stage_root,
            private_prefix,
            prefix,
            package_cache,
            records,
            retained_packages,
            owners,
            replacement_destinations,
            preserved_stock_python_paths,
        )
        gcc_specs = relocate_conda_gcc_specs(
            private_prefix,
            prefix,
            package_cache,
            records["conda-gcc-specs"],
            owners,
            stage_root,
        )
        prefix_relocation_entries.append(gcc_specs)

        drop_prefix_bound_bin_scripts(stage_root, private_prefix, prefix, owners, excluded_owned_paths)
        set_relative_runpaths(stage_root, private_prefix)
        verify_symlink_closure(stage_root)
        reject_absolute_prefix_references(stage_root, [prefix, stage_root])
        prefix_relocation = finalize_prefix_relocation_evidence(stage_root, prefix_relocation_entries)

        census = audit_elf(
            stage_root,
            private_prefix,
            base_info["provenance"],
            owners,
            (
                None
                if native_rebuild_handoff is not None
                else Path(args.abi_preflight_evidence).resolve() if args.abi_preflight_evidence else None
            ),
            sha256_file(lock_path),
            sha256_file(evidence_path),
        )
        if preserved_stock_python_paths:
            raise SystemExit(
                "stock ABI preflight unexpectedly passed while target-specific cp312 Python payload remained; "
                "refusing to archive a cp314 overlay"
            )
        provenance_root = stage_root / "fenics-runtime" / "provenance"
        provenance_root.mkdir(parents=True, exist_ok=True)
        lock_copy = provenance_root / lock_path.name
        shutil.copy2(lock_path, lock_copy)
        owners[lock_copy.relative_to(stage_root).as_posix()] = ["python-numpy-fenics"]
        prefix_evidence_copy = provenance_root / "locked-prefix-verification.json"
        shutil.copy2(evidence_path, prefix_evidence_copy)
        owners[prefix_evidence_copy.relative_to(stage_root).as_posix()] = ["python-numpy-fenics"]

        native_rebuild_candidate: dict[str, Any] | None = None
        if native_rebuild_handoff is not None:
            assert native_rebuild_handoff_path is not None
            assert args.abi_preflight_evidence is not None
            handoff_copy = provenance_root / "native-rebuild-handoff.json"
            shutil.copy2(native_rebuild_handoff_path, handoff_copy)
            owners[handoff_copy.relative_to(stage_root).as_posix()] = ["python-numpy-fenics"]
            stock_preflight_copy = provenance_root / "stock-native-abi-preflight.json"
            shutil.copy2(Path(args.abi_preflight_evidence).resolve(), stock_preflight_copy)
            owners[stock_preflight_copy.relative_to(stage_root).as_posix()] = ["python-numpy-fenics"]
            handoff_manifest = native_rebuild_handoff["manifest"]
            native_rebuild_candidate = {
                "status": NATIVE_HANDOFF_STATUS,
                "source_rebuild_provenance_eligible": False,
                "handoff": {
                    "path": handoff_copy.relative_to(stage_root).as_posix(),
                    "sha256": native_rebuild_handoff["manifest_sha256"],
                },
                "stock_abi_preflight": {
                    "path": stock_preflight_copy.relative_to(stage_root).as_posix(),
                    "sha256": sha256_file(stock_preflight_copy),
                    "selection_fingerprint_sha256": native_rebuild_handoff["stock_preflight"]["selection_fingerprint_sha256"],
                },
                "inputs": handoff_manifest["inputs"],
                "conditional_omissions": [
                    {
                        "package": item["package"],
                        "native_closure_report_sha256": item["native_closure_report_sha256"],
                        "candidate_output_binding_sha256": item["candidate_output_binding_sha256"],
                        "evidence_sha256": item["evidence_sha256"],
                    }
                    for item in handoff_manifest["conditional_omissions"]
                ],
                "replacements": staged_native_replacements,
                "qualification": handoff_manifest["qualification"],
            }

        package_summary = []
        retained_set = set(retained_packages)
        excluded_set = set(excluded_packages)
        omitted_set = set(omitted_packages)
        for record in lock["packages"]:
            name = str(record["name"])
            ownership = (
                "retained"
                if name in retained_set
                else "excluded"
                if name in excluded_set
                else "omitted-by-native-rebuild-candidate"
                if name in omitted_set
                else "not-staged"
            )
            package_summary.append(
                {key: record.get(key) for key in REQUIRED_FIELDS} | {"overlay_ownership": ownership}
            )
        package_provenance = {
            "format_version": 1,
            "source": (
                "conda-forge-exact-binary-lock"
                if native_rebuild_candidate is None
                else "conda-forge-exact-binary-lock-with-unqualified-native-rebuild-candidate"
            ),
            "solver_used_by_packager": False,
            "source_rebuilds": [],
            "package_lock_sha256": sha256_file(lock_path),
            "external_prefix_verification_sha256": sha256_file(evidence_path),
            "packages": package_summary,
            "excluded_owned_paths": excluded_owned_paths,
            "prefix_relocation": prefix_relocation,
        }
        if native_rebuild_candidate is not None:
            package_provenance["native_rebuild_candidate"] = native_rebuild_candidate
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
                "producer_revision": base_info["producer_revision"],
                "numpy_tree_sha256": numpy_identity["tree_sha256"],
            },
        }
        if native_rebuild_candidate is not None:
            source_provenance["native_rebuild_candidate"] = native_rebuild_candidate
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
        asset_name = f"fenics-embed-runtime-v{args.version}-linux-x64-{TARGET_PYTHON_ABI}.tar.gz"
        manifest = {
            "format_version": 1,
            "artifact": asset_name.removesuffix(".tar.gz"),
            "version": args.version,
            "platform": "linux-x64",
            "profile": "fenics",
            "architecture": "x86_64",
            "python_abi": TARGET_PYTHON_ABI,
            "consumer_python": TARGET_PYTHON_PATCH,
            "producer_python": records["python"].get("version"),
            "stock_package_python_abi": "cp312",
            "tcl_tk": base_info["tcl_tk"],
            "numpy": {
                "version": "2.5.2",
                "ownership": "consumer-numpy-profile",
                "producer_payload_excluded": True,
                "base_producer_revision": base_info["producer_revision"],
                "base_manifest_sha256": sha256_file(base_info["manifest_path"]),
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
                "private_site_packages": f"fenics-runtime/conda/{TARGET_PYTHON_SITE.as_posix()}",
                "bootstrap": f"{TARGET_PYTHON_SITE.as_posix()}/fenics_embed_runtime.py",
                "gcc": "fenics-runtime/conda/bin/x86_64-conda-linux-gnu-gcc",
                "sysroot": "fenics-runtime/conda/x86_64-conda-linux-gnu/sysroot",
            },
            "python_path_translation": {
                "stock_prefix_site_packages": STOCK_PYTHON_SITE.as_posix(),
                "overlay_site_packages": TARGET_PYTHON_SITE.as_posix(),
                "target_specific_stock_payload_policy": "requires-explicit-cp314-replacement",
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
        if native_rebuild_candidate is not None:
            manifest["producer"]["native_rebuild_candidate"] = native_rebuild_candidate
            manifest["producer"]["omitted_packages"] = omitted_packages
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
        if native_rebuild_candidate is not None:
            summary["native_rebuild_candidate"] = {
                    "status": native_rebuild_candidate["status"],
                    "handoff_sha256": native_rebuild_candidate["handoff"]["sha256"],
                    "replacement_count": len(native_rebuild_candidate["replacements"]),
                    "conditional_omissions": [
                        item["package"] for item in native_rebuild_candidate["conditional_omissions"]
                    ],
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
