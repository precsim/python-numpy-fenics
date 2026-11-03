#!/usr/bin/env python3
"""Reproduce the pinned Linux FEniCS private C++ runtime provider.

The provider is a link product from the immutable public FEniCS runtime lock,
followed by the separately audited GNU_UNIQUE-to-GLOBAL byte transform. It is
not a source rebuild and does not qualify the surrounding FEniCS overlay.
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
import tempfile
from typing import Any
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.linux.elf_gnu_unique import (  # noqa: E402
    ElfContractError,
    TransformContract,
    VERSION_NAMESPACES,
    _audit_json,
    transform_bytes,
    write_output,
)


DEFAULT_RECIPE = REPO_ROOT / "locks" / "linux" / "linux-x86_64-fenics-cxx-provider.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_RECIPE_KIND = "fenics-private-cxx-runtime-build-recipe"
EXPECTED_MANIFEST_KIND = "fenics-private-cxx-runtime"


class ProviderBuildError(ValueError):
    """Raised when a provider build input violates the pinned contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_text_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig")
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_sha256(value: object, label: str) -> str:
    text = str(value or "").lower()
    if not SHA256_RE.fullmatch(text):
        raise ProviderBuildError(f"{label} must be a 64-digit SHA-256")
    return text


def require_repo_path(raw: object, label: str) -> Path:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        raise ProviderBuildError(f"{label} must be a non-empty repository-relative path")
    root = REPO_ROOT.resolve()
    path = (root / raw).resolve()
    if root not in path.parents:
        raise ProviderBuildError(f"{label} escapes the repository: {raw!r}")
    return path


def load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ProviderBuildError(f"{label} is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderBuildError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProviderBuildError(f"{label} root must be a JSON object")
    return payload


def load_recipe(path: Path) -> dict[str, Any]:
    recipe = load_json(path, "provider recipe")
    if recipe.get("format_version") != 1 or recipe.get("kind") != EXPECTED_RECIPE_KIND:
        raise ProviderBuildError("unsupported provider recipe identity")
    if recipe.get("architecture") != "x86_64" or recipe.get("source_build") is not False:
        raise ProviderBuildError("provider recipe must describe the x86_64 non-source-build proof")
    if recipe.get("evidence_scope") != "reproduction-contract-unqualified":
        raise ProviderBuildError("provider recipe must remain explicitly unqualified")
    return recipe


def validate_checked_in_locks(recipe: dict[str, Any]) -> dict[str, Any]:
    runtime_record = recipe.get("runtime_package_lock")
    runtime_explicit_record = recipe.get("runtime_source_explicit")
    build_tool_record = recipe.get("native_build_tool_explicit")
    if not all(isinstance(item, dict) for item in (runtime_record, runtime_explicit_record, build_tool_record)):
        raise ProviderBuildError("provider recipe lock records are malformed")
    assert isinstance(runtime_record, dict)
    assert isinstance(runtime_explicit_record, dict)
    assert isinstance(build_tool_record, dict)

    runtime_lock_path = require_repo_path(runtime_record.get("path"), "runtime package lock path")
    runtime_explicit_path = require_repo_path(runtime_explicit_record.get("path"), "runtime explicit path")
    build_tool_explicit_path = require_repo_path(build_tool_record.get("path"), "build-tool explicit path")

    expected_runtime_sha = require_sha256(runtime_record.get("canonical_sha256"), "runtime package lock canonical SHA-256")
    actual_runtime_sha = canonical_text_sha256(runtime_lock_path)
    if actual_runtime_sha != expected_runtime_sha:
        raise ProviderBuildError(
            f"runtime package lock SHA-256 mismatch: {actual_runtime_sha} != {expected_runtime_sha}"
        )
    expected_runtime_explicit_sha = require_sha256(
        runtime_explicit_record.get("canonical_sha256"), "runtime explicit SHA-256"
    )
    actual_runtime_explicit_sha = canonical_text_sha256(runtime_explicit_path)
    if actual_runtime_explicit_sha != expected_runtime_explicit_sha:
        raise ProviderBuildError(
            "runtime source explicit SHA-256 mismatch: "
            f"{actual_runtime_explicit_sha} != {expected_runtime_explicit_sha}"
        )
    expected_build_tool_sha = require_sha256(
        build_tool_record.get("canonical_sha256"), "build-tool explicit SHA-256"
    )
    actual_build_tool_sha = canonical_text_sha256(build_tool_explicit_path)
    if actual_build_tool_sha != expected_build_tool_sha:
        raise ProviderBuildError(
            f"build-tool explicit SHA-256 mismatch: {actual_build_tool_sha} != {expected_build_tool_sha}"
        )
    if build_tool_record.get("used_for_provider_link") is not False:
        raise ProviderBuildError("build-tool lock must be provenance-only for the GCC 16.2 provider link")

    runtime_lock = load_json(runtime_lock_path, "runtime package lock")
    if runtime_lock.get("architecture") != "x86_64" or runtime_lock.get("format_version") != 1:
        raise ProviderBuildError("runtime package lock identity does not match Linux x86_64")
    packages = runtime_lock.get("packages")
    if not isinstance(packages, list) or len(packages) != 124:
        raise ProviderBuildError("runtime package lock must contain exactly 124 package records")
    by_name: dict[str, dict[str, Any]] = {}
    for item in packages:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ProviderBuildError("runtime package lock contains an invalid package record")
        name = str(item["name"])
        if name in by_name:
            raise ProviderBuildError(f"runtime package lock contains duplicate package name: {name}")
        by_name[name] = item

    link_packages = recipe.get("link_packages")
    if not isinstance(link_packages, list) or not link_packages:
        raise ProviderBuildError("provider recipe has no link package records")
    verified_packages: list[dict[str, Any]] = []
    for expected in link_packages:
        if not isinstance(expected, dict):
            raise ProviderBuildError("provider recipe contains an invalid link package record")
        name = str(expected.get("name") or "")
        actual = by_name.get(name)
        if actual is None:
            raise ProviderBuildError(f"runtime package lock is missing provider link package: {name}")
        fields = ("name", "version", "build", "url", "sha256")
        mismatch = {
            field: {"expected": expected.get(field), "actual": actual.get(field)}
            for field in fields
            if expected.get(field) != actual.get(field)
        }
        if mismatch:
            raise ProviderBuildError(
                f"runtime lock identity mismatch for provider package {name}: "
                + json.dumps(mismatch, sort_keys=True)
            )
        require_sha256(expected.get("sha256"), f"{name} archive SHA-256")
        verified_packages.append({field: actual[field] for field in fields})

    return {
        "runtime_package_lock": {
            "path": str(runtime_lock_path),
            "sha256": actual_runtime_sha,
            "record_count": len(packages),
        },
        "runtime_source_explicit": {
            "path": str(runtime_explicit_path),
            "canonical_sha256": actual_runtime_explicit_sha,
        },
        "native_build_tool_explicit": {
            "path": str(build_tool_explicit_path),
            "canonical_sha256": actual_build_tool_sha,
            "used_for_provider_link": False,
        },
        "packages": verified_packages,
    }


def validate_prefix_evidence(path: Path, runtime_lock_sha256: str) -> dict[str, Any]:
    evidence = load_json(path, "runtime prefix verification evidence")
    if evidence.get("format_version") != 1 or evidence.get("architecture") != "x86_64":
        raise ProviderBuildError("runtime prefix evidence identity mismatch")
    if evidence.get("lock_sha256") != runtime_lock_sha256:
        raise ProviderBuildError("runtime prefix evidence was not produced from the pinned runtime lock")
    if evidence.get("lock_record_count") != 124 or evidence.get("installed_record_count") != 124:
        raise ProviderBuildError("runtime prefix evidence does not cover the exact 124-record lock")
    if evidence.get("verified_package_archive_count") != 124:
        raise ProviderBuildError("runtime prefix evidence did not independently hash all 124 archives")
    if evidence.get("package_hash_verification") != "cached-package-sha256-plus-installed-record-match":
        raise ProviderBuildError("runtime prefix evidence has an unsupported archive verification mode")
    if evidence.get("solver_used") is not False:
        raise ProviderBuildError("runtime prefix evidence must describe solver-free replay")
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "lock_sha256": runtime_lock_sha256,
        "lock_record_count": 124,
        "verified_package_archive_count": 124,
        "solver_used": False,
    }


def locate_and_verify_archives(package_cache: Path, packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not package_cache.is_dir():
        raise ProviderBuildError(f"package cache is missing: {package_cache}")
    records: list[dict[str, Any]] = []
    for package in packages:
        filename = Path(urlparse(str(package["url"])).path).name
        if not filename:
            raise ProviderBuildError(f"package URL has no filename: {package['url']!r}")
        candidates = sorted(path for path in package_cache.rglob(filename) if path.is_file())
        if not candidates:
            raise ProviderBuildError(f"package cache has no archive for {package['name']}: {filename}")
        expected_sha = require_sha256(package["sha256"], f"{package['name']} archive SHA-256")
        observed: list[str] = []
        for candidate in candidates:
            actual_sha = sha256_file(candidate)
            if actual_sha != expected_sha:
                raise ProviderBuildError(
                    f"cached package archive SHA-256 mismatch for {package['name']}: "
                    f"{candidate}: {actual_sha} != {expected_sha}"
                )
            observed.append(candidate.relative_to(package_cache).as_posix())
        records.append(
            {
                "name": package["name"],
                "version": package["version"],
                "build": package["build"],
                "url": package["url"],
                "sha256": expected_sha,
                "cache_paths": observed,
            }
        )
    return records


def verify_link_inputs(recipe: dict[str, Any], runtime_prefix: Path) -> dict[str, Any]:
    if not runtime_prefix.is_dir():
        raise ProviderBuildError(f"runtime prefix is missing: {runtime_prefix}")
    inputs = recipe.get("link_inputs")
    if not isinstance(inputs, dict):
        raise ProviderBuildError("provider recipe link_inputs are malformed")
    records: dict[str, Any] = {}
    for name in ("gcc_driver", "libstdcxx_static"):
        item = inputs.get(name)
        if not isinstance(item, dict):
            raise ProviderBuildError(f"provider recipe is missing {name}")
        relative = item.get("relative_path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ProviderBuildError(f"{name} relative_path is invalid")
        path = (runtime_prefix / relative).resolve()
        if runtime_prefix.resolve() not in path.parents:
            raise ProviderBuildError(f"{name} escapes the runtime prefix")
        if not path.is_file():
            raise ProviderBuildError(f"{name} is missing from runtime prefix: {path}")
        expected_sha = require_sha256(item.get("sha256"), f"{name} SHA-256")
        actual_sha = sha256_file(path)
        if actual_sha != expected_sha:
            raise ProviderBuildError(f"{name} SHA-256 mismatch: {actual_sha} != {expected_sha}")
        records[name] = {
            "package": item.get("package"),
            "path": str(path),
            "relative_path": relative,
            "sha256": actual_sha,
        }

    version_item = inputs.get("version_script")
    if not isinstance(version_item, dict):
        raise ProviderBuildError("provider recipe is missing version_script")
    version_path = require_repo_path(version_item.get("path"), "version script path")
    expected_version_sha = require_sha256(version_item.get("sha256"), "version script SHA-256")
    actual_version_sha = sha256_file(version_path)
    if actual_version_sha != expected_version_sha:
        raise ProviderBuildError(
            f"version script SHA-256 mismatch: {actual_version_sha} != {expected_version_sha}"
        )
    records["version_script"] = {
        "path": str(version_path),
        "sha256": actual_version_sha,
    }
    return records


def render_link_argv(recipe: dict[str, Any], inputs: dict[str, Any], link_output: Path) -> list[str]:
    link = recipe.get("link")
    if not isinstance(link, dict) or not isinstance(link.get("argv"), list):
        raise ProviderBuildError("provider recipe link argv is malformed")
    values = {
        "gcc_driver": inputs["gcc_driver"]["path"],
        "libstdcxx_static": inputs["libstdcxx_static"]["path"],
        "version_script": inputs["version_script"]["path"],
        "link_output": str(link_output),
    }
    rendered: list[str] = []
    for raw in link["argv"]:
        if not isinstance(raw, str) or not raw:
            raise ProviderBuildError("provider recipe link argv contains an invalid token")
        try:
            token = raw.format_map(values)
        except KeyError as exc:
            raise ProviderBuildError(f"unknown provider link argv placeholder: {exc.args[0]}") from exc
        if "{" in token or "}" in token:
            raise ProviderBuildError(f"unexpanded provider link argv token: {token!r}")
        rendered.append(token)
    if not rendered or Path(rendered[0]).resolve() != Path(inputs["gcc_driver"]["path"]).resolve():
        raise ProviderBuildError("provider link must execute the pinned GCC driver directly")
    if rendered[-2:] != ["-o", str(link_output)]:
        raise ProviderBuildError("provider link output must be the final exact argv pair")
    return rendered


def deterministic_link_environment(recipe: dict[str, Any], runtime_prefix: Path) -> dict[str, str]:
    link = recipe.get("link")
    if not isinstance(link, dict) or not isinstance(link.get("environment"), dict):
        raise ProviderBuildError("provider recipe link environment is malformed")
    allowed = {"LANG", "LC_ALL", "SOURCE_DATE_EPOCH", "TZ"}
    requested = link["environment"]
    if set(requested) != allowed or not all(isinstance(requested[key], str) for key in allowed):
        raise ProviderBuildError("provider recipe link environment must pin LANG/LC_ALL/SOURCE_DATE_EPOCH/TZ")
    environment = {key: str(requested[key]) for key in sorted(allowed)}
    environment["PATH"] = str((runtime_prefix / "bin").resolve())
    return environment


def _run_probe(argv: list[str], environment: dict[str, str], label: str) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
    except OSError as exc:
        raise ProviderBuildError(f"cannot execute GCC provenance probe {label}: {exc}") from exc
    if completed.returncode != 0:
        raise ProviderBuildError(
            f"GCC provenance probe failed ({label}, exit {completed.returncode}): {completed.stderr.strip()}"
        )
    value = completed.stdout.strip()
    if not value:
        raise ProviderBuildError(f"GCC provenance probe returned no value: {label}")
    return value


def probe_toolchain(gcc_driver: Path, environment: dict[str, str], runtime_prefix: Path) -> dict[str, Any]:
    probes = {
        "gcc_version": [str(gcc_driver), "--version"],
        "target": [str(gcc_driver), "-dumpmachine"],
        "sysroot": [str(gcc_driver), "-print-sysroot"],
        "linker": [str(gcc_driver), "-print-prog-name=ld"],
        "libgcc_s": [str(gcc_driver), "-print-file-name=libgcc_s.so.1"],
        "crtbeginS": [str(gcc_driver), "-print-file-name=crtbeginS.o"],
        "crtendS": [str(gcc_driver), "-print-file-name=crtendS.o"],
    }
    results = {name: _run_probe(argv, environment, name) for name, argv in probes.items()}
    if results["target"] != "x86_64-conda-linux-gnu":
        raise ProviderBuildError(f"unexpected GCC target: {results['target']!r}")
    prefix = runtime_prefix.resolve()
    file_records: dict[str, Any] = {}
    for name in ("libgcc_s", "crtbeginS", "crtendS"):
        candidate = Path(results[name]).resolve()
        if prefix not in candidate.parents or not candidate.is_file():
            raise ProviderBuildError(f"GCC probe {name} escaped the locked runtime prefix: {candidate}")
        file_records[name] = {"path": str(candidate), "sha256": sha256_file(candidate)}
    linker_raw = results["linker"]
    linker = Path(linker_raw)
    if not linker.is_absolute():
        found = shutil.which(linker_raw, path=environment["PATH"])
        if found is None:
            raise ProviderBuildError(f"GCC linker probe is not resolvable inside runtime prefix: {linker_raw!r}")
        linker = Path(found)
    linker = linker.resolve()
    if prefix not in linker.parents or not linker.is_file():
        raise ProviderBuildError(f"GCC linker escaped the locked runtime prefix: {linker}")
    file_records["linker"] = {"path": str(linker), "sha256": sha256_file(linker)}
    sysroot = Path(results["sysroot"]).resolve()
    if prefix not in sysroot.parents or not sysroot.is_dir():
        raise ProviderBuildError(f"GCC sysroot escaped the locked runtime prefix: {sysroot}")
    return {
        "target": results["target"],
        "gcc_version_banner": results["gcc_version"].splitlines()[0],
        "sysroot": str(sysroot),
        "files": file_records,
    }


def link_provider(argv: list[str], environment: dict[str, str], expected_sha256: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
    except OSError as exc:
        raise ProviderBuildError(f"cannot execute pinned GCC provider link: {exc}") from exc
    if completed.returncode != 0:
        raise ProviderBuildError(
            f"provider link failed with exit {completed.returncode}: {completed.stderr.strip()}"
        )
    output = Path(argv[-1])
    if not output.is_file():
        raise ProviderBuildError("provider link succeeded without producing the expected output")
    actual_sha = sha256_file(output)
    if actual_sha != expected_sha256:
        raise ProviderBuildError(
            f"linked provider SHA-256 mismatch: {actual_sha} != {expected_sha256}"
        )
    return {
        "argv": argv,
        "environment": environment,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "sha256": actual_sha,
    }


def transform_contract(recipe: dict[str, Any]) -> TransformContract:
    transform = recipe.get("transform")
    link = recipe.get("link")
    if not isinstance(transform, dict) or not isinstance(link, dict):
        raise ProviderBuildError("provider recipe transform/link records are malformed")
    if transform.get("operation_kind") != "elf-symbol-binding-transform":
        raise ProviderBuildError("provider transform operation kind is unsupported")
    input_sha = require_sha256(transform.get("expected_input_sha256"), "transform input SHA-256")
    output_sha = require_sha256(transform.get("expected_output_sha256"), "transform output SHA-256")
    if input_sha != require_sha256(link.get("expected_sha256"), "linked provider SHA-256"):
        raise ProviderBuildError("link and transform input SHA-256 contracts differ")
    if transform.get("expected_soname") != link.get("soname"):
        raise ProviderBuildError("link and transform SONAME contracts differ")
    unique_count = transform.get("expected_unique_count")
    dynsym_count = transform.get("expected_dynsym_count")
    if not isinstance(unique_count, int) or unique_count <= 0:
        raise ProviderBuildError("expected GNU_UNIQUE count must be positive")
    if not isinstance(dynsym_count, int) or dynsym_count <= unique_count:
        raise ProviderBuildError("expected dynsym count must exceed GNU_UNIQUE count")
    raw_versions = transform.get("expected_version_max")
    if not isinstance(raw_versions, dict) or set(raw_versions) != set(VERSION_NAMESPACES):
        raise ProviderBuildError("transform ABI maximum contract must cover all version namespaces")
    versions: dict[str, tuple[int, ...] | None] = {}
    for namespace in VERSION_NAMESPACES:
        raw = raw_versions[namespace]
        if raw is None:
            versions[namespace] = None
        elif isinstance(raw, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", raw):
            versions[namespace] = tuple(int(part) for part in raw.split("."))
        else:
            raise ProviderBuildError(f"invalid transform ABI maximum for {namespace}: {raw!r}")
    return TransformContract(
        input_sha256=input_sha,
        output_sha256=output_sha,
        expected_unique_count=unique_count,
        expected_dynsym_count=dynsym_count,
        expected_soname=str(transform["expected_soname"]),
        expected_version_max=versions,
    )


def transform_provider(linked: Path, output: Path, contract: TransformContract) -> dict[str, Any]:
    try:
        transformed, before, after = transform_bytes(linked.read_bytes(), contract)
    except (OSError, ElfContractError) as exc:
        raise ProviderBuildError(f"provider symbol transform rejected linked bytes: {exc}") from exc
    try:
        mode = linked.stat().st_mode & 0o777
        write_output(output, transformed, mode)
    except OSError as exc:
        raise ProviderBuildError(f"cannot install transformed provider: {exc}") from exc
    return {
        "operation_kind": "elf-symbol-binding-transform",
        "source_build": False,
        "changed_binding_bytes": contract.expected_unique_count,
        "before": _audit_json(before),
        "after": _audit_json(after),
    }


def build_manifest(
    *,
    recipe_path: Path,
    recipe: dict[str, Any],
    locks: dict[str, Any],
    prefix_evidence: dict[str, Any],
    archives: list[dict[str, Any]],
    inputs: dict[str, Any],
    toolchain: dict[str, Any],
    link_record: dict[str, Any],
    transform_record: dict[str, Any],
    provider: Path,
) -> dict[str, Any]:
    link = recipe["link"]
    final_sha = sha256_file(provider)
    if final_sha != transform_record["after"]["sha256"]:
        raise ProviderBuildError("provider bytes changed after the audited symbol transform")
    return {
        "format_version": 1,
        "kind": EXPECTED_MANIFEST_KIND,
        "architecture": "x86_64",
        "qualification_status": "unqualified-reproduction-only",
        "source_build": False,
        "library": {
            "path": str(provider.resolve()),
            "sha256": final_sha,
            "soname": link["soname"],
            "symbol_version": link["symbol_version"],
        },
        "recipe": {"path": str(recipe_path.resolve()), "sha256": sha256_file(recipe_path)},
        "locks": locks,
        "runtime_prefix_verification": prefix_evidence,
        "verified_public_package_archives": archives,
        "link_inputs": inputs,
        "toolchain_probe": toolchain,
        "link": link_record,
        "transform": transform_record,
        "limitations": {
            "source_rebuild_provenance_eligible": False,
            "hosted_glibc217_poisson_qualified": False,
            "surrounding_fenics_native_rebuild_qualified": False,
        },
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # The provider is built as root in the pinned manylinux container,
        # then inspected by the non-root GitHub Actions host.
        temp.chmod(0o644)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-prefix", type=Path, required=True)
    parser.add_argument("--runtime-prefix-evidence", type=Path, required=True)
    parser.add_argument("--package-cache", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if sys.platform != "linux":
            raise ProviderBuildError("provider linking is supported only on a Linux x86_64 hosted runner")
        if os.uname().machine != "x86_64":
            raise ProviderBuildError(f"provider linking requires x86_64, got {os.uname().machine!r}")
        recipe_path = DEFAULT_RECIPE.resolve()
        recipe = load_recipe(recipe_path)
        locks = validate_checked_in_locks(recipe)
        prefix_evidence = validate_prefix_evidence(
            args.runtime_prefix_evidence.resolve(), locks["runtime_package_lock"]["sha256"]
        )
        archives = locate_and_verify_archives(args.package_cache.resolve(), locks["packages"])
        runtime_prefix = args.runtime_prefix.resolve()
        inputs = verify_link_inputs(recipe, runtime_prefix)
        environment = deterministic_link_environment(recipe, runtime_prefix)
        toolchain = probe_toolchain(Path(inputs["gcc_driver"]["path"]), environment, runtime_prefix)
        contract = transform_contract(recipe)

        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        link_name = recipe["link"].get("intermediate_name")
        output_name = recipe["link"].get("output_name")
        if link_name != "libfenics_cxxrt.so.1.linked" or output_name != "libfenics_cxxrt.so.1":
            raise ProviderBuildError("provider output names differ from the pinned contract")
        linked = output_dir / link_name
        provider = output_dir / output_name
        manifest_path = args.manifest.resolve()
        if linked.exists() or provider.exists() or manifest_path.exists():
            raise ProviderBuildError("provider output or manifest path already exists")
        if manifest_path in {linked, provider}:
            raise ProviderBuildError("provider manifest path collides with provider output")

        link_argv = render_link_argv(recipe, inputs, linked)
        link_record = link_provider(link_argv, environment, contract.input_sha256)
        transform_record = transform_provider(linked, provider, contract)
        manifest = build_manifest(
            recipe_path=recipe_path,
            recipe=recipe,
            locks=locks,
            prefix_evidence=prefix_evidence,
            archives=archives,
            inputs=inputs,
            toolchain=toolchain,
            link_record=link_record,
            transform_record=transform_record,
            provider=provider,
        )
        write_json_atomic(manifest_path, manifest)
        print(f"provider_sha256={manifest['library']['sha256']}")
        print(f"provider_manifest={manifest_path}")
        print("provider_qualification_status=unqualified-reproduction-only")
        return 0
    except ProviderBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
