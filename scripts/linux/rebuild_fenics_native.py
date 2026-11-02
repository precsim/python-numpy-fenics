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
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
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
PYTHON_BUILD_MODULES = ("pip", "scikit-build-core", "nanobind", "wheel")
ABI_CEILINGS = {
    "GLIBC": (2, 17),
    "GLIBCXX": (3, 4, 19),
    "CXXABI": (1, 3, 7),
    "GCC": (4, 8, 0),
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


def validate_python_builder(path: Path) -> tuple[dict, Path]:
    manifest = load_json(path)
    if manifest.get("format_version") != 1 or manifest.get("kind") != "fenics-python-wheel-builder":
        raise InputError("unsupported Python wheel-builder manifest identity")
    python_record = manifest.get("python")
    if not isinstance(python_record, dict):
        raise InputError("Python wheel-builder manifest has no python record")
    executable = Path(str(python_record.get("path", "")))
    verify_file(executable, python_record.get("sha256"), "Python wheel-builder executable")
    if not os.access(executable, os.X_OK):
        raise InputError(f"Python wheel-builder executable is not executable: {executable}")
    expected_modules = manifest.get("modules")
    if not isinstance(expected_modules, dict) or tuple(sorted(expected_modules)) != tuple(sorted(PYTHON_BUILD_MODULES)):
        raise InputError(f"Python wheel-builder modules must be exactly {sorted(PYTHON_BUILD_MODULES)!r}")
    code = (
        "import importlib.metadata,json; "
        f"names={list(PYTHON_BUILD_MODULES)!r}; "
        "print(json.dumps({n:importlib.metadata.version(n) for n in names},sort_keys=True))"
    )
    actual_modules = json.loads(run(executable, "-c", code).stdout)
    if actual_modules != expected_modules:
        raise InputError(f"Python wheel-builder module versions differ: {actual_modules!r} != {expected_modules!r}")
    return {**manifest, "python": {**python_record, "path": str(executable.resolve())}}, executable.resolve()


def validate_libxml2_build_deps(path: Path) -> tuple[dict, Path, Path, Path]:
    manifest = load_json(path)
    if manifest.get("format_version") != 1 or manifest.get("kind") != "fenics-libxml2-build-deps":
        raise InputError("unsupported libxml2 build-dependency manifest identity")
    prefix = Path(str(manifest.get("prefix", ""))).resolve()
    if not prefix.is_dir():
        raise InputError(f"libxml2 build-dependency prefix is missing: {prefix}")
    headers = manifest.get("headers")
    if not isinstance(headers, dict):
        raise InputError("libxml2 build-dependency manifest has no headers object")
    required_headers = {"include/zlib.h", "include/zconf.h", "include/lzma.h"}
    actual_lzma_headers = {
        path.relative_to(prefix).as_posix() for path in sorted((prefix / "include" / "lzma").glob("*.h")) if path.is_file()
    }
    expected_headers = required_headers | actual_lzma_headers
    if set(headers) != expected_headers:
        raise InputError("libxml2 build-dependency header set is incomplete or has unexpected files")
    header_evidence = {}
    for relative, expected_sha in sorted(headers.items()):
        header_evidence[relative] = verify_file(prefix / relative, expected_sha, f"libxml2 build header {relative}")
    libraries = manifest.get("libraries")
    if not isinstance(libraries, dict) or set(libraries) != {"zlib", "lzma"}:
        raise InputError("libxml2 build-dependency libraries must be exactly zlib and lzma")
    library_paths = {}
    library_evidence = {}
    for name in ("zlib", "lzma"):
        record = libraries[name]
        if not isinstance(record, dict) or not isinstance(record.get("relative_path"), str):
            raise InputError(f"invalid libxml2 build library record: {name}")
        relative = Path(record["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise InputError(f"invalid libxml2 build library path: {relative}")
        library = prefix / relative
        library_evidence[name] = verify_file(library, record.get("sha256"), f"libxml2 build library {name}")
        library_paths[name] = library.resolve()
    evidence = {
        "format_version": 1,
        "kind": "fenics-libxml2-build-deps",
        "prefix": str(prefix),
        "headers": header_evidence,
        "libraries": library_evidence,
        "production_frozen": manifest.get("production_frozen") is True,
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
    for name in ("kahip", "libadios2", "zeromq"):
        candidate = candidates.get(name)
        if not isinstance(candidate, dict) or candidate.get("reached_paths") != []:
            raise InputError(f"conditional omission is not proven for {name}")
        reverse = candidate.get("reverse_direct_noncandidate")
        if not isinstance(reverse, list) or any(item.get("in_closure") is not False for item in reverse if isinstance(item, dict)):
            raise InputError(f"conditional omission has an in-closure reverse dependency for {name}")
        omitted[name] = {"staged_path_count": len(candidate.get("staged_paths", [])), "abi_maxima": candidate.get("abi_maxima")}
    icu = candidates.get("icu")
    if not isinstance(icu, dict):
        raise InputError("native closure proof has no ICU candidate analysis")
    icu_reached = icu.get("reached_paths")
    if not isinstance(icu_reached, list):
        raise InputError("native closure proof has invalid ICU reached paths")
    return {
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "closure_elf_count": report.get("closure_elf_count"),
        "dolfinx_build_flags": flags,
        "conditional_omissions": omitted,
        "icu_reached_paths": icu_reached,
        "icu_resolution_required": bool(icu_reached),
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
    wheel_env["PYTHONPATH"] = f"{basix_audit}:{stock}/lib/python3.12/site-packages"
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
        if args.validate_public_inputs_only:
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
                    "sources": source_evidence,
                    "recipe_source_bindings": recipe_bindings,
                    "spdlog_patch": patch_evidence,
                    "locked_packages": package_evidence,
                    "locked_prefix": {"path": str(args.locked_prefix.resolve()), "verification": prefix_evidence},
                    "stock_abi_preflight": stock_abi_evidence,
                    "deferred_full_rebuild_inputs": [
                        "python_builder_manifest",
                        "libxml2_build_deps_manifest",
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
            "--native-closure-report": args.native_closure_report,
        }
        missing_external = [flag for flag, value in required_external.items() if value is None]
        if missing_external:
            raise InputError("full native rebuild requires: " + ", ".join(missing_external))
        assert args.python_builder_manifest is not None
        assert args.libxml2_build_deps_manifest is not None
        assert args.private_provider_manifest is not None
        assert args.native_closure_report is not None
        closure_evidence = validate_native_closure_report(args.native_closure_report)
        python_builder_evidence, python_builder = validate_python_builder(args.python_builder_manifest)
        libxml2_build_evidence, libxml2_include, zlib_library, lzma_library = validate_libxml2_build_deps(
            args.libxml2_build_deps_manifest
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
        if args.execute:
            wheels, audits = build_candidate(
                roots,
                args.locked_prefix.resolve(),
                tools,
                python_builder,
                libxml2_include,
                zlib_library,
                lzma_library,
            )
            elf_records, wheel_records = collect_outputs(roots, wheels, audits, tools)
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
            outputs={"elf": elf_records, "wheels": wheel_records},
            executed=args.execute,
        )
        print(f"rebuild_mode={'execute' if args.execute else 'validate-only'}")
        print(f"provenance={provenance}")
        print("source_rebuild_provenance_eligible=false")
        return 0
    except InputError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
