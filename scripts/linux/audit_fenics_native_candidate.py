#!/usr/bin/env python3
"""Independently audit a built Linux FEniCS native candidate closure."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.linux.rebuild_fenics_native import (  # noqa: E402
    ABI_CEILINGS,
    HANDOFF_OMISSION_PACKAGES,
    InputError,
    canonical_json_sha,
    installed_owned_paths,
    installed_package_records,
    normalize_candidate_output_binding,
    sha256,
    validate_candidate_build_handoff,
)


ROOT_PACKAGES = (
    "petsc",
    "petsc4py",
    "slepc",
    "slepc4py",
    "mpich",
    "mpi4py",
    "hdf5",
)

# Keep this synchronized with the producer-owned manylinux2014 baseline.  A
# bundled provider always wins; these names are accepted only when no locked or
# rebuilt provider exists.
PEP599_EXTERNAL = {
    "libgcc_s.so.1",
    "libstdc++.so.6",
    "libm.so.6",
    "libdl.so.2",
    "librt.so.1",
    "libc.so.6",
    "libnsl.so.1",
    "libutil.so.1",
    "libpthread.so.0",
    "libresolv.so.2",
    "libX11.so.6",
    "libXext.so.6",
    "libXrender.so.1",
    "libICE.so.6",
    "libSM.so.6",
    "libGL.so.1",
    "libgobject-2.0.so.0",
    "libgthread-2.0.so.0",
    "libglib-2.0.so.0",
    "ld-linux-x86-64.so.2",
}

SKIP_PROVIDER_PACKAGES = {"sysroot_linux-64", "kernel-headers_linux-64"}
LOCKED_GCC_DUPLICATE_PROVIDERS = {
    "libgcc_s.so.1": (
        "lib/libgcc_s.so.1",
        "lib/gcc/x86_64-conda-linux-gnu/16.2.0/libgcc_s.so.1",
    ),
    "libstdc++.so.6": (
        "lib/libstdc++.so.6.0.36",
        "lib/gcc/x86_64-conda-linux-gnu/16.2.0/libstdc++.so.6.0.36",
    ),
}
VERSION_NAMESPACES = tuple(ABI_CEILINGS)
ALLOWED_NONNUMERIC_VERSIONS = {"CXXABI_TM_1"}
VERSION_TOKEN = re.compile(r"\bName: ((?:GLIBC|CXXABI|GLIBCXX|GCC)_[A-Za-z0-9_.]+)")
NEEDED = re.compile(r"\(NEEDED\).*Shared library: \[([^\]]+)\]")
SONAME = re.compile(r"\(SONAME\).*Library soname: \[([^\]]+)\]")


class AuditError(InputError):
    pass


class UnresolvedDependency(AuditError):
    pass


def is_elf(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError:
        return False


def _run_readelf(readelf: Path, *args: str, path: Path) -> str:
    result = subprocess.run(
        [str(readelf), *args, str(path)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AuditError(
            f"readelf failed for {path}: exit={result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


def _version_needs(text: str) -> list[str]:
    marker = "Version needs section"
    start = text.find(marker)
    if start < 0:
        return []
    section = text[start:]
    next_section = re.search(r"\nVersion (?:symbols|definition) section", section)
    if next_section is not None:
        section = section[: next_section.start()]
    return sorted(set(VERSION_TOKEN.findall(section)))


def _numeric_version(token: str, namespace: str) -> tuple[int, ...] | None:
    prefix = namespace + "_"
    if not token.startswith(prefix):
        return None
    raw = token[len(prefix) :]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", raw):
        return None
    return tuple(int(part) for part in raw.split("."))


def _version_text(value: tuple[int, ...] | None) -> str | None:
    return None if value is None else ".".join(str(part) for part in value)


def abi_maxima(tokens: list[str]) -> dict[str, str | None]:
    maxima: dict[str, tuple[int, ...] | None] = {name: None for name in VERSION_NAMESPACES}
    for token in tokens:
        matched = False
        for namespace in VERSION_NAMESPACES:
            value = _numeric_version(token, namespace)
            if value is None:
                continue
            matched = True
            if maxima[namespace] is None or value > maxima[namespace]:
                maxima[namespace] = value
        if not matched and token not in ALLOWED_NONNUMERIC_VERSIONS:
            raise AuditError(f"unsupported nonnumeric ELF version requirement: {token}")
    return {name: _version_text(maxima[name]) for name in VERSION_NAMESPACES}


def merge_abi_maxima(nodes: list[dict[str, Any]]) -> dict[str, str | None]:
    tokens: set[str] = set()
    for node in nodes:
        tokens.update(node["required_versions"])
    return abi_maxima(sorted(tokens))


def inspect_elf(path: Path, readelf: Path) -> dict[str, Any]:
    if not is_elf(path):
        raise AuditError(f"expected ELF file is missing or invalid: {path}")
    header = _run_readelf(readelf, "-hW", path=path)
    if "ELF64" not in header or "Advanced Micro Devices X86-64" not in header:
        raise AuditError(f"candidate closure contains non-x86_64 ELF: {path}")
    dynamic = _run_readelf(readelf, "-dW", path=path)
    versions = _run_readelf(readelf, "--version-info", "-W", path=path)
    needed = sorted(set(NEEDED.findall(dynamic)))
    sonames = SONAME.findall(dynamic)
    if len(set(sonames)) > 1:
        raise AuditError(f"ELF reports ambiguous SONAME values: {path}: {sonames}")
    required_versions = _version_needs(versions)
    return {
        "sha256": sha256(path),
        "soname": sonames[0] if sonames else None,
        "needed": needed,
        "required_versions": required_versions,
        "abi_maxima": abi_maxima(required_versions),
    }


def _candidate_path(roots: dict[str, Path], logical: str) -> Path:
    relative = PurePosixPath(logical)
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 2:
        raise AuditError(f"unsafe candidate output path: {logical}")
    root_name = relative.parts[0]
    if root_name not in {"prefix", "audit", "wheels"}:
        raise AuditError(f"candidate output path uses unsupported root: {logical}")
    return roots[root_name].joinpath(*relative.parts[1:])


def _verify_live_binding(candidate: dict[str, Any]) -> tuple[dict, dict[str, Path]]:
    binding = normalize_candidate_output_binding(
        candidate["candidate_output_binding"], "audited candidate output binding"
    )
    expected_sha = candidate["candidate_output_binding_sha256"]
    actual_sha = canonical_json_sha(binding)
    if actual_sha != expected_sha:
        raise AuditError(
            f"candidate output binding changed before closure audit: {actual_sha} != {expected_sha}"
        )
    live_elf: dict[str, Path] = {}
    for record in binding["elf"]:
        path = _candidate_path(candidate["roots"], record["path"])
        if not path.is_file() or path.is_symlink() or sha256(path) != record["sha256"]:
            raise AuditError(f"candidate ELF changed before closure audit: {record['path']}")
        live_elf[record["path"]] = path
    for record in binding["wheels"]:
        path = _candidate_path(candidate["roots"], record["path"])
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != record["bytes"]
            or sha256(path) != record["sha256"]
        ):
            raise AuditError(f"candidate wheel changed before closure audit: {record['path']}")
    return binding, live_elf


def _dolfinx_flags(candidate: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    roots = candidate["roots"]
    bound = candidate.get("dolfinx_build_configuration")
    if not isinstance(bound, dict) or set(bound) != {"cmake_cache", "flags"}:
        raise AuditError("candidate handoff has no bound DOLFINx build configuration")
    cache_record = bound.get("cmake_cache")
    if not isinstance(cache_record, dict) or set(cache_record) != {"path", "sha256"}:
        raise AuditError("candidate handoff has invalid DOLFINx CMake cache identity")
    cache = roots["build"] / "dolfinx" / "CMakeCache.txt"
    if not cache.is_file() or cache.is_symlink():
        raise AuditError(f"DOLFINx CMakeCache build evidence is missing: {cache}")
    if Path(str(cache_record.get("path", ""))).resolve() != cache.resolve():
        raise AuditError("candidate handoff DOLFINx CMake cache path does not match candidate root")
    cache_sha = sha256(cache)
    if cache_sha != cache_record.get("sha256"):
        raise AuditError("candidate handoff DOLFINx CMake cache changed before closure audit")
    values: dict[str, list[str]] = defaultdict(list)
    for line in cache.read_text(encoding="utf-8", errors="strict").splitlines():
        match = re.fullmatch(r"(DOLFINX_ENABLE_(?:ADIOS2|KAHIP)):[^=]+=([^\r\n]+)", line)
        if match:
            values[match.group(1)].append(match.group(2).strip().upper())
    required = ("DOLFINX_ENABLE_ADIOS2", "DOLFINX_ENABLE_KAHIP")
    flags: dict[str, str] = {}
    for name in required:
        observed = values.get(name, [])
        if observed != ["OFF"]:
            raise AuditError(f"DOLFINx build evidence does not prove {name}=OFF: {observed!r}")
        flags[name] = "OFF"
    if flags != bound.get("flags"):
        raise AuditError("independent DOLFINx build flag reading differs from candidate-bound evidence")
    return flags, {
        "path": str(cache.resolve()),
        "sha256": cache_sha,
        "candidate_build_handoff_bound": True,
    }


def _owned_paths(records: dict[str, dict]) -> tuple[dict[str, str], dict[str, list[str]]]:
    owners: dict[str, str] = {}
    by_package: dict[str, list[str]] = {}
    for name, record in sorted(records.items()):
        package_paths = sorted(installed_owned_paths(record))
        by_package[name] = package_paths
        for relative in package_paths:
            previous = owners.get(relative)
            if previous is not None and previous != name:
                raise AuditError(
                    f"locked prefix has ambiguous package ownership: {relative}: {previous} vs {name}"
                )
            owners[relative] = name
    return owners, by_package


def _node(
    *,
    logical_path: str,
    source: Path,
    owner: str,
    readelf: Path,
    origin: str,
) -> dict[str, Any]:
    inspected = inspect_elf(source, readelf)
    return {
        "path": logical_path,
        "source": source,
        "owner": owner,
        "origin": origin,
        **inspected,
        "aliases": {Path(logical_path).name},
    }


def _build_nodes(
    candidate: dict[str, Any],
    binding: dict,
    live_elf: dict[str, Path],
    readelf: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[str]], dict[str, str]]:
    locked_prefix = candidate["locked_prefix"].resolve()
    records = installed_package_records(locked_prefix)
    owners, by_package = _owned_paths(records)
    replacement_by_path = {record["destination"]: record for record in binding["replacements"]}

    elf_by_sha: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for record in binding["elf"]:
        elf_by_sha[record["sha256"]].append((record["path"], live_elf[record["path"]]))

    retained: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    node_by_path: dict[str, dict[str, Any]] = {}
    used_candidate_elf: set[str] = set()

    for replacement in binding["replacements"]:
        candidates = elf_by_sha.get(replacement["sha256"], [])
        if not candidates:
            continue
        exact = [
            item for item in candidates
            if item[0] == (PurePosixPath("prefix") / PurePosixPath(replacement["destination"])).as_posix()
        ]
        chosen = exact if exact else candidates
        if len(chosen) != 1:
            raise AuditError(
                f"candidate replacement maps ambiguously to rebuilt ELF output: {replacement['destination']}"
            )
        logical, source = chosen[0]
        used_candidate_elf.add(logical)
        node = _node(
            logical_path=replacement["destination"],
            source=source,
            owner=str(replacement["owner"]),
            readelf=readelf,
            origin="candidate-replacement",
        )
        node_by_path[node["path"]] = node
        retained.append(node)

    missing_candidate_elf = sorted(set(live_elf) - used_candidate_elf)
    if missing_candidate_elf:
        raise AuditError(
            "rebuilt ELF output has no replacement destination binding: "
            + ", ".join(missing_candidate_elf)
        )

    symlinks: list[tuple[str, str]] = []
    for relative, owner in sorted(owners.items()):
        if owner in SKIP_PROVIDER_PACKAGES or relative in replacement_by_path:
            continue
        path = locked_prefix.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink():
            symlinks.append((relative, owner))
            continue
        if not is_elf(path):
            continue
        node = _node(
            logical_path=relative,
            source=path,
            owner=owner,
            readelf=readelf,
            origin="locked-prefix",
        )
        node_by_path[relative] = node
        if owner in HANDOFF_OMISSION_PACKAGES:
            omitted.append(node)
        else:
            retained.append(node)

    for relative, owner in symlinks:
        path = locked_prefix.joinpath(*PurePosixPath(relative).parts)
        try:
            target = path.resolve(strict=True)
            target_relative = target.relative_to(locked_prefix).as_posix()
        except (OSError, ValueError) as exc:
            raise AuditError(f"locked-prefix provider symlink escapes or is broken: {relative}") from exc
        target_node = node_by_path.get(target_relative)
        if target_node is not None:
            target_node["aliases"].add(Path(relative).name)

    # Rebuilt prefix symlinks may add aliases that were not present in the stock
    # prefix.  They are evidence only when they resolve to a bound replacement.
    candidate_prefix = candidate["roots"]["prefix"].resolve()
    for path in sorted(candidate_prefix.rglob("*")):
        if not path.is_symlink():
            continue
        relative = path.relative_to(candidate_prefix).as_posix()
        try:
            target = path.resolve(strict=True)
            target_relative = target.relative_to(candidate_prefix).as_posix()
        except (OSError, ValueError) as exc:
            raise AuditError(f"candidate provider symlink escapes or is broken: {relative}") from exc
        target_node = node_by_path.get(target_relative)
        if target_node is not None:
            target_node["aliases"].add(path.name)

    return retained, omitted, by_package, owners


def _provider_index(nodes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        names = set(node["aliases"])
        if node["soname"]:
            names.add(node["soname"])
        for name in names:
            existing = index[name]
            if any(item["path"] == node["path"] for item in existing):
                continue
            identical = [item for item in existing if item["sha256"] == node["sha256"]]
            conflicting = [item for item in existing if item["sha256"] != node["sha256"]]
            if conflicting:
                node_exact = name in node["aliases"]
                existing_exact = [item for item in existing if name in item["aliases"]]
                if node_exact and not existing_exact:
                    index[name] = [node]
                    continue
                if existing_exact and not node_exact:
                    continue
                existing.append(node)
                continue
            if identical:
                # A conda package may install both the SONAME filename and a
                # versioned regular-file alias with identical bytes. Collapse
                # that to one provider identity, preferring the path whose
                # basename exactly matches the lookup name.
                preferred = node if Path(node["path"]).name == name else identical[0]
                index[name] = [preferred]
                continue
            existing.append(node)
    return index


def _resolve_provider(
    name: str,
    index: dict[str, list[dict[str, Any]]],
    consumer: str,
) -> dict[str, Any] | None:
    providers = index.get(name, [])
    duplicate_pair = LOCKED_GCC_DUPLICATE_PROVIDERS.get(name)
    if duplicate_pair is not None and {
        item["path"] for item in providers
    } == set(duplicate_pair):
        # The locked GCC 16.2.0 package ships compiler-private runtime copies
        # in addition to the top-level providers used by rebuilt FEniCS
        # libraries. Keep each exception exact so any new provider is fatal.
        runtime_provider = duplicate_pair[0]
        return next(item for item in providers if item["path"] == runtime_provider)
    if len(providers) > 1:
        raise AuditError(
            f"ambiguous provider for {consumer} -> {name}: "
            + ", ".join(sorted(item["path"] for item in providers))
        )
    if providers:
        return providers[0]
    if name in PEP599_EXTERNAL:
        return None
    raise UnresolvedDependency(f"unresolved non-system dependency: {consumer} -> {name}")


def _closure(
    roots: list[dict[str, Any]],
    index: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    queue = deque(sorted(roots, key=lambda item: item["path"]))
    visited: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, str]] = []
    while queue:
        node = queue.popleft()
        if node["path"] in visited:
            continue
        visited[node["path"]] = node
        for needed in node["needed"]:
            try:
                provider = _resolve_provider(needed, index, node["path"])
            except UnresolvedDependency:
                unresolved.append({"consumer": node["path"], "needed": needed})
                continue
            if provider is not None and provider["path"] not in visited:
                queue.append(provider)
    return [visited[key] for key in sorted(visited)], sorted(
        unresolved, key=lambda item: (item["consumer"], item["needed"])
    )


def _reverse_dependencies(
    candidate_nodes: list[dict[str, Any]],
    all_noncandidate: list[dict[str, Any]],
    closure_paths: set[str],
) -> list[dict[str, Any]]:
    candidate_index = _provider_index(candidate_nodes)
    result: list[dict[str, Any]] = []
    for consumer in sorted(all_noncandidate, key=lambda item: item["path"]):
        for needed in consumer["needed"]:
            providers = candidate_index.get(needed, [])
            for provider in providers:
                result.append(
                    {
                        "consumer": consumer["path"],
                        "needed": needed,
                        "provider": provider["path"],
                        "in_closure": consumer["path"] in closure_paths,
                    }
                )
    return sorted(
        result,
        key=lambda item: (item["consumer"], item["needed"], item["provider"]),
    )


def _branch_report(
    roots: list[dict[str, Any]],
    index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    closure, unresolved = _closure(roots, index)
    if unresolved:
        raise AuditError(f"MPICH branch has unresolved dependencies: {unresolved!r}")
    owners = {node["owner"] for node in closure}
    missing = {"mpich", "libhwloc", "libxml2-16"} - owners
    if missing:
        raise AuditError(
            "MPICH -> hwloc -> libxml2 control branch is incomplete; missing owners: "
            + ", ".join(sorted(missing))
        )
    return {
        "root_paths": sorted(node["path"] for node in roots),
        "closure_paths": sorted(node["path"] for node in closure),
        "reached_owners": sorted(owners),
        "icu_reached_paths": sorted(node["path"] for node in closure if node["owner"] == "icu"),
    }


def build_report(candidate: dict[str, Any], readelf: Path) -> dict[str, Any]:
    binding, live_elf = _verify_live_binding(candidate)
    flags, flag_evidence = _dolfinx_flags(candidate)
    retained, omitted, by_package, _ = _build_nodes(candidate, binding, live_elf, readelf)
    retained_index = _provider_index(retained)

    candidate_paths = {record["destination"] for record in binding["replacements"]}
    roots = [
        node
        for node in retained
        if node["path"] in candidate_paths or node["owner"] in ROOT_PACKAGES
    ]
    if not roots:
        raise AuditError("native candidate closure has no roots")
    for package in ("mpich", "hdf5", "petsc"):
        if not any(node["owner"] == package for node in roots):
            raise AuditError(f"native candidate closure is missing required root package: {package}")

    closure, unresolved = _closure(roots, retained_index)
    if unresolved:
        raise AuditError(f"native candidate has unresolved non-system dependencies: {unresolved!r}")
    closure_paths = {node["path"] for node in closure}

    candidates: dict[str, dict[str, Any]] = {}
    for name in HANDOFF_OMISSION_PACKAGES:
        package_nodes = [node for node in omitted if node["owner"] == name]
        if not package_nodes:
            raise AuditError(f"locked prefix contains no ELF provider evidence for omission candidate: {name}")
        reverse = _reverse_dependencies(package_nodes, retained, closure_paths)
        if any(item["in_closure"] for item in reverse):
            raise AuditError(f"conditional omission is still required by live closure: {name}: {reverse!r}")
        augmented = _provider_index([*retained, *package_nodes])
        reached, augmented_unresolved = _closure(roots, augmented)
        if augmented_unresolved:
            raise AuditError(
                f"omission analysis for {name} has unresolved dependencies: {augmented_unresolved!r}"
            )
        reached_paths = sorted(node["path"] for node in reached if node["owner"] == name)
        if reached_paths:
            raise AuditError(f"conditional omission remains reachable for {name}: {reached_paths}")
        candidates[name] = {
            "reached_paths": [],
            "reverse_direct_noncandidate": reverse,
            "staged_paths": by_package.get(name, []),
            "abi_maxima": merge_abi_maxima(package_nodes),
        }

    icu_nodes = [node for node in retained if node["owner"] == "icu"]
    if not icu_nodes:
        raise AuditError("locked prefix contains no ICU ELF providers")
    icu_reverse = _reverse_dependencies(icu_nodes, [node for node in retained if node["owner"] != "icu"], closure_paths)
    mpich_roots = [node for node in roots if node["owner"] == "mpich"]
    if not mpich_roots:
        raise AuditError("native candidate closure has no MPICH root")
    candidates["icu"] = {
        "reached_paths": sorted(node["path"] for node in closure if node["owner"] == "icu"),
        "reverse_direct_noncandidate": icu_reverse,
        "staged_paths": by_package.get("icu", []),
        "abi_maxima": merge_abi_maxima(icu_nodes),
        "mpich_hwloc_libxml2_branch": _branch_report(mpich_roots, retained_index),
    }

    return {
        "format_version": 1,
        "kind": "linux-fenics-native-candidate-closure",
        "candidate_build_handoff_sha256": candidate["sha256"],
        "candidate_output_binding": binding,
        "candidate_output_binding_sha256": candidate["candidate_output_binding_sha256"],
        "dolfinx_build_flags": flags,
        "dolfinx_build_evidence": flag_evidence,
        "closure_roots": sorted(node["path"] for node in roots),
        "closure_paths": sorted(closure_paths),
        "closure_elf_count": len(closure),
        "closure_abi_maxima": merge_abi_maxima(closure),
        "unresolved_non_system": [],
        "candidates": candidates,
    }


def audit_candidate(candidate_handoff: Path, output: Path, readelf: Path) -> dict[str, Any]:
    if output.exists() or output.is_symlink():
        raise AuditError(f"native closure report output must be absent: {output}")
    candidate = validate_candidate_build_handoff(candidate_handoff)
    report = build_report(candidate, readelf)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _resolve_readelf(raw: str) -> Path:
    candidate = Path(raw)
    if candidate.is_absolute() or os.sep in raw or (os.altsep and os.altsep in raw):
        if not candidate.is_file():
            raise AuditError(f"readelf executable is missing: {candidate}")
        return candidate.resolve()
    found = shutil.which(raw)
    if found is None:
        raise AuditError(f"readelf executable is unavailable: {raw}")
    return Path(found).resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-build-handoff", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--readelf", default="readelf")
    args = parser.parse_args(argv)
    try:
        readelf = _resolve_readelf(args.readelf)
        report = audit_candidate(
            args.candidate_build_handoff.resolve(), args.output.resolve(), readelf
        )
        print(f"native_closure_report={args.output.resolve()}")
        print(f"candidate_output_binding_sha256={report['candidate_output_binding_sha256']}")
        print(f"closure_elf_count={report['closure_elf_count']}")
        print("unresolved_non_system=0")
        return 0
    except (AuditError, InputError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
