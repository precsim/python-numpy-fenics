#!/usr/bin/env python3
"""Hosted Linux probe for cutting the HDF5 -> curl -> libpsl -> ICU edge.

The probe rebuilds only libcurl 8.23.0 with ``--without-libpsl`` from pinned
public inputs.  It never changes the 128-package runtime lock and never claims
that the resulting library is a releasable overlay input.  Its purpose is to
produce executable closure evidence for one ICU edge while retaining the
independent MPICH -> hwloc -> libxml2 -> ICU path as a negative control.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen


CONTRACT_KIND = "linux-fenics-libcurl-without-libpsl-icu-edge-probe"
EXPECTED_LOCK_RECORD_COUNT = 128
EXPECTED_CURL_VERSION = "8.23.0"
EXPECTED_CURL_SOURCE_SHA256 = "95b22eb32bf6d1fa6e3e9463bdb61cece3efc56d86576a9b3cbd428246a6d854"
EXPECTED_FEEDSTOCK_COMMIT = "d770c46d773d7e2c81555d606720c3d6435b216f"
EXPECTED_FEEDSTOCK_BLOBS = {
    "recipe/meta.yaml": "fdc0ab979397f8ae4f79290da9b30c93eeca4bbe",
    "recipe/build.sh": "0f0bf7555814eb695a07024e39595cee4a210665",
}
ABI_CEILINGS = {
    "GLIBC": (2, 17),
    "GLIBCXX": (3, 4, 19),
    "CXXABI": (1, 3, 7),
    "GCC": (4, 8, 0),
}
ALLOWED_NONNUMERIC_VERSION_TOKENS = {"CXXABI_TM_1"}
PUBLIC_FETCH_HOSTS = {"curl.se", "raw.githubusercontent.com"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class ProbeError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProbeError(f"JSON input must be an object: {path}")
    return value


def require_hash(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ProbeError(f"{label} has invalid hash syntax")
    return value


def load_contract(path: Path) -> dict:
    data = load_json(path)
    if data.get("schema_version") != 1 or data.get("kind") != CONTRACT_KIND:
        raise ProbeError("unsupported ICU-edge probe contract")
    target = data.get("target")
    if target != {"architecture": "x86_64", "glibc_floor": "2.17"}:
        raise ProbeError("ICU-edge probe target must remain x86_64/glibc-2.17")

    runtime_lock = data.get("runtime_lock")
    if not isinstance(runtime_lock, dict) or runtime_lock.get("record_count") != EXPECTED_LOCK_RECORD_COUNT:
        raise ProbeError("ICU-edge contract must preserve the exact 128-record runtime lock")
    packages = runtime_lock.get("packages")
    expected_names = {"hdf5", "libcurl", "libpsl", "icu", "mpich", "libhwloc", "libxml2", "libxml2-16"}
    if not isinstance(packages, dict) or set(packages) != expected_names:
        raise ProbeError("ICU-edge contract has an unexpected locked package identity set")
    for name, record in packages.items():
        if not isinstance(record, dict):
            raise ProbeError(f"invalid locked package identity: {name}")
        require_hash(record.get("sha256"), SHA256_RE, f"locked package {name} sha256")

    source = data.get("curl_source")
    if not isinstance(source, dict):
        raise ProbeError("ICU-edge contract has no curl source record")
    if source.get("name") != "curl" or source.get("version") != EXPECTED_CURL_VERSION:
        raise ProbeError("ICU-edge probe must remain pinned to curl 8.23.0")
    if source.get("sha256") != EXPECTED_CURL_SOURCE_SHA256:
        raise ProbeError("curl source SHA-256 differs from the pinned feedstock source")
    if source.get("filename") != "curl-8.23.0.tar.bz2":
        raise ProbeError("unexpected curl source filename")
    source_url = source.get("url")
    source_parsed = urlparse(str(source_url))
    if not isinstance(source_url, str) or source_parsed.scheme != "https" or source_parsed.hostname != "curl.se":
        raise ProbeError("curl source must use the pinned public curl.se origin")

    feedstock = data.get("feedstock")
    if not isinstance(feedstock, dict) or feedstock.get("repository") != "conda-forge/curl-feedstock":
        raise ProbeError("unexpected curl feedstock identity")
    if feedstock.get("commit") != EXPECTED_FEEDSTOCK_COMMIT:
        raise ProbeError("curl feedstock commit changed without updating the probe contract")
    files = feedstock.get("files")
    if not isinstance(files, dict) or set(files) != set(EXPECTED_FEEDSTOCK_BLOBS):
        raise ProbeError("curl feedstock evidence file set changed")
    for relative, expected_blob in EXPECTED_FEEDSTOCK_BLOBS.items():
        record = files[relative]
        if not isinstance(record, dict) or record.get("git_blob_sha1") != expected_blob:
            raise ProbeError(f"curl feedstock blob identity changed: {relative}")
        require_hash(record.get("git_blob_sha1"), SHA1_RE, f"feedstock blob {relative}")
        raw_url = record.get("url")
        parsed = urlparse(str(raw_url))
        if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com" or EXPECTED_FEEDSTOCK_COMMIT not in parsed.path:
            raise ProbeError(f"feedstock evidence URL is not pinned to the exact commit: {relative}")
    recipe_evidence = feedstock.get("required_recipe_evidence")
    if recipe_evidence != {"source_sha256": EXPECTED_CURL_SOURCE_SHA256, "configure_flag": "--with-libpsl"}:
        raise ProbeError("curl feedstock recipe evidence changed")

    probe = data.get("probe")
    if not isinstance(probe, dict) or probe.get("host_triplet") != "x86_64-conda-linux-gnu":
        raise ProbeError("unexpected ICU-edge probe host triplet")
    flags = probe.get("configure_flags")
    if not isinstance(flags, list) or "--without-libpsl" not in flags or "--with-libpsl" in flags:
        raise ProbeError("probe configure flags must contain only the PSL-disabling variant")
    delta = probe.get("configure_delta_from_feedstock")
    if delta != {"remove": ["--with-libpsl"], "add": ["--without-libpsl"], "other_changes": []}:
        raise ProbeError("ICU-edge experiment must remain a one-flag configure delta from the feedstock")
    ceilings = probe.get("strict_symbol_ceilings")
    expected_ceilings = {name: ".".join(map(str, value)) for name, value in ABI_CEILINGS.items()}
    if ceilings != expected_ceilings:
        raise ProbeError("ICU-edge ABI ceilings changed")
    if probe.get("required_baseline_curl_reachability") != ["libpsl.so", "libicuuc.so", "libicudata.so"]:
        raise ProbeError("stock libcurl closure contract changed")
    if probe.get("forbidden_candidate_curl_reachability") != ["libpsl.so", "libicuuc.so", "libicudata.so"]:
        raise ProbeError("candidate libcurl closure contract changed")
    if probe.get("required_mpich_control_reachability") != [
        "libhwloc.so",
        "libxml2.so",
        "libicuuc.so",
        "libicudata.so",
    ]:
        raise ProbeError("MPICH ICU-control closure contract changed")

    capability = data.get("capability_impact")
    if not isinstance(capability, dict) or "disabled" not in str(capability.get("libcurl_public_suffix_list", "")):
        raise ProbeError("ICU-edge contract must state the libcurl PSL capability impact")
    return data


def package_records(lock: dict) -> dict[str, dict]:
    records = lock.get("packages")
    if not isinstance(records, list):
        raise ProbeError("runtime lock has no packages list")
    result: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("name"), str):
            raise ProbeError("runtime lock contains an invalid package record")
        name = record["name"]
        if name in result:
            raise ProbeError(f"runtime lock contains duplicate package {name}")
        result[name] = record
    return result


def verify_runtime_lock(contract: dict, lock_path: Path) -> dict:
    lock = load_json(lock_path)
    records = package_records(lock)
    if len(records) != EXPECTED_LOCK_RECORD_COUNT:
        raise ProbeError(f"runtime lock record count changed: {len(records)} != {EXPECTED_LOCK_RECORD_COUNT}")
    expected = contract["runtime_lock"]["packages"]
    verified = {}
    for name, identity in expected.items():
        actual = records.get(name)
        if actual is None:
            raise ProbeError(f"runtime lock lost ICU-edge package: {name}")
        for key in ("version", "build", "sha256"):
            if actual.get(key) != identity[key]:
                raise ProbeError(f"runtime lock identity mismatch for {name} {key}: {actual.get(key)!r} != {identity[key]!r}")
        verified[name] = {key: actual[key] for key in ("version", "build", "sha256", "url")}
    return {
        "path": str(lock_path.resolve()),
        "sha256": sha256_file(lock_path),
        "record_count": len(records),
        "packages": verified,
    }


def validate_hosted_evidence(prefix_verification: Path, build_tool_verification: Path, stock_abi_preflight: Path) -> dict:
    prefix = load_json(prefix_verification)
    if (
        prefix.get("solver_used") is not False
        or prefix.get("lock_record_count") != EXPECTED_LOCK_RECORD_COUNT
        or prefix.get("installed_record_count") != EXPECTED_LOCK_RECORD_COUNT
        or prefix.get("verified_package_archive_count") != EXPECTED_LOCK_RECORD_COUNT
        or prefix.get("package_hash_verification") != "cached-package-sha256-plus-installed-record-match"
        or prefix.get("replay_input_format") != "@EXPLICIT-url-plus-sha256"
        or not isinstance(prefix.get("packages"), list)
        or len(prefix["packages"]) != EXPECTED_LOCK_RECORD_COUNT
    ):
        raise ProbeError("hosted FEniCS prefix verification does not prove the exact 128-record locked replay")

    build_tools = load_json(build_tool_verification)
    expected_roots = {
        "gcc_linux-64": ("14.4.0", "hc469d41_2"),
        "gxx_linux-64": ("14.4.0", "h0047a55_2"),
        "sysroot_linux-64": ("2.17", "h0157908_18"),
        "pkg-config": ("0.29.2", "h1114479_1013"),
        "make": ("4.4.1", "h1114479_4"),
    }
    roots = build_tools.get("roots")
    if (
        build_tools.get("purpose") != "linux-fenics-cxx20-build-toolchain-only"
        or build_tools.get("build_only") is not True
        or build_tools.get("runtime_lock_modified") is not False
        or build_tools.get("dependency_solver_used") is not False
        or build_tools.get("package_count") != 41
        or build_tools.get("source_prefix_record_count") != 41
        or build_tools.get("exact_prefix") is not True
        or not isinstance(roots, dict)
    ):
        raise ProbeError("hosted build-tool verification does not prove the exact 41-record build-only replay")
    for name, (version, build) in expected_roots.items():
        record = roots.get(name)
        if not isinstance(record, dict) or (record.get("version"), record.get("build")) != (version, build):
            raise ProbeError(f"hosted build-tool verification root mismatch: {name}")

    preflight = load_json(stock_abi_preflight)
    violating = preflight.get("violating_packages")
    if (
        preflight.get("format_version") != 1
        or preflight.get("stage") != "stock-binary-closure-before-selective-rebuild"
        or preflight.get("status") != "fail"
        or preflight.get("source_rebuilds_authorized") is not False
        or not isinstance(violating, list)
        or "icu" not in violating
    ):
        raise ProbeError("stock ABI preflight does not prove ICU is in the failing locked native closure")
    return {
        "prefix_verification": {"path": str(prefix_verification.resolve()), "sha256": sha256_file(prefix_verification)},
        "build_tool_verification": {
            "path": str(build_tool_verification.resolve()),
            "sha256": sha256_file(build_tool_verification),
        },
        "stock_abi_preflight": {"path": str(stock_abi_preflight.resolve()), "sha256": sha256_file(stock_abi_preflight)},
    }


def fetch_bytes(url: str) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in PUBLIC_FETCH_HOSTS or parsed.username or parsed.password:
        raise ProbeError(f"refusing non-public or unpinned fetch origin: {url}")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            request = Request(url, headers={"User-Agent": "precsim-linux-fenics-icu-edge-probe/1"})
            with urlopen(request, timeout=90) as response:
                resolved = response.geturl()
                resolved_parsed = urlparse(resolved)
                if resolved_parsed.scheme != "https" or resolved_parsed.hostname not in PUBLIC_FETCH_HOSTS:
                    raise ProbeError(f"public input redirect left allowed HTTPS origins: {url} -> {resolved}")
                return response.read()
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(attempt)
    assert last_error is not None
    if isinstance(last_error, ProbeError):
        raise last_error
    raise ProbeError(f"failed to fetch {url}: {last_error}") from last_error


def verify_input_bundle(contract: dict, input_dir: Path) -> dict:
    source_record = contract["curl_source"]
    source = input_dir / source_record["filename"]
    if not source.is_file() or source.is_symlink():
        raise ProbeError(f"pinned curl source is missing: {source}")
    if sha256_file(source) != source_record["sha256"]:
        raise ProbeError("pinned curl source SHA-256 mismatch")

    feedstock_evidence = {}
    feedstock_root = input_dir / "feedstock"
    for relative, record in contract["feedstock"]["files"].items():
        path = feedstock_root / Path(*relative.split("/"))
        if not path.is_file() or path.is_symlink():
            raise ProbeError(f"pinned feedstock evidence is missing: {relative}")
        data = path.read_bytes()
        actual_blob = git_blob_sha1(data)
        if actual_blob != record["git_blob_sha1"]:
            raise ProbeError(f"feedstock git-blob identity mismatch: {relative}: {actual_blob}")
        feedstock_evidence[relative] = {"git_blob_sha1": actual_blob, "sha256": hashlib.sha256(data).hexdigest()}

    meta = (feedstock_root / "recipe" / "meta.yaml").read_text(encoding="utf-8")
    build = (feedstock_root / "recipe" / "build.sh").read_text(encoding="utf-8")
    if EXPECTED_CURL_SOURCE_SHA256 not in meta or '{% set version = "8.23.0" %}' not in meta:
        raise ProbeError("pinned feedstock recipe no longer binds curl 8.23.0 source identity")
    if "--with-libpsl" not in build or "--without-libpsl" in build:
        raise ProbeError("pinned feedstock build script no longer proves the PSL-enabled baseline")
    return {
        "source": {"path": str(source.resolve()), "sha256": sha256_file(source)},
        "feedstock_commit": contract["feedstock"]["commit"],
        "feedstock_files": feedstock_evidence,
    }


def fetch_inputs(contract_path: Path, output_dir: Path, evidence_path: Path) -> None:
    contract = load_contract(contract_path)
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ProbeError(f"ICU-edge input directory must start empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    source = contract["curl_source"]
    source_bytes = fetch_bytes(source["url"])
    if hashlib.sha256(source_bytes).hexdigest() != source["sha256"]:
        raise ProbeError("downloaded curl source SHA-256 mismatch")
    (output_dir / source["filename"]).write_bytes(source_bytes)

    for relative, record in contract["feedstock"]["files"].items():
        data = fetch_bytes(record["url"])
        if git_blob_sha1(data) != record["git_blob_sha1"]:
            raise ProbeError(f"downloaded feedstock git-blob mismatch: {relative}")
        destination = output_dir / "feedstock" / Path(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    verified = verify_input_bundle(contract, output_dir)
    evidence = {
        "format_version": 1,
        "kind": "linux-fenics-icu-edge-public-inputs",
        "contract_sha256": sha256_file(contract_path),
        "private_sources_used": False,
        "inputs": verified,
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        raise ProbeError(f"command failed ({result.returncode}): {' '.join(args)}\n{result.stdout}")
    return result


def verify_hosted_container() -> dict:
    architecture = run(["uname", "-m"]).stdout.strip()
    glibc = run(["getconf", "GNU_LIBC_VERSION"]).stdout.strip()
    if architecture != "x86_64":
        raise ProbeError(f"ICU-edge probe requires x86_64 hosted container, got {architecture!r}")
    if glibc != "glibc 2.17":
        raise ProbeError(f"ICU-edge probe requires exact glibc 2.17 baseline, got {glibc!r}")

    route_path = Path("/proc/net/route")
    if not route_path.is_file():
        raise ProbeError("ICU-edge probe cannot verify network isolation: /proc/net/route is missing")
    rows = route_path.read_text(encoding="ascii", errors="strict").splitlines()[1:]
    default_routes = []
    for row in rows:
        fields = row.split()
        if len(fields) >= 2 and fields[1] == "00000000" and fields[0] != "lo":
            default_routes.append(fields[0])
    if default_routes:
        raise ProbeError(f"ICU-edge probe requires docker --network none; default routes remain: {default_routes}")
    return {"architecture": architecture, "glibc": glibc, "non_loopback_default_routes": default_routes}


def is_elf(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError:
        return False


def audit_version_tokens(tokens: list[str], *, strict: bool) -> dict[str, str | None]:
    maxima: dict[str, str | None] = {}
    unexpected_nonnumeric = []
    for namespace, ceiling in ABI_CEILINGS.items():
        numeric_values = []
        prefix = namespace + "_"
        for token in tokens:
            if not token.startswith(prefix):
                continue
            suffix = token[len(prefix):]
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", suffix):
                value = tuple(int(part) for part in suffix.split("."))
                numeric_values.append(value)
                if strict and value > ceiling:
                    raise ProbeError(f"ELF ABI ceiling exceeded: {token} > {namespace}_{'.'.join(map(str, ceiling))}")
            elif token not in ALLOWED_NONNUMERIC_VERSION_TOKENS:
                unexpected_nonnumeric.append(token)
        maximum = max(numeric_values, default=None)
        maxima[namespace] = None if maximum is None else ".".join(map(str, maximum))
    if strict and unexpected_nonnumeric:
        raise ProbeError(f"ELF has unsupported nonnumeric version requirements: {sorted(set(unexpected_nonnumeric))}")
    return maxima


def audit_rpaths(rpaths: list[str], *, strict: bool) -> None:
    if not strict:
        return
    for packed in rpaths:
        for entry in packed.split(":"):
            if entry and not entry.startswith("$ORIGIN"):
                raise ProbeError(f"ELF has absolute/non-relative RPATH: {entry}")


def read_elf(path: Path, readelf: Path, *, strict_abi: bool = False) -> dict:
    if not is_elf(path):
        raise ProbeError(f"expected ELF file: {path}")
    header = run([str(readelf), "-hW", str(path)]).stdout
    if "ELF64" not in header or "Advanced Micro Devices X86-64" not in header:
        raise ProbeError(f"non-x86_64 ELF in ICU-edge probe: {path}")
    dynamic = run([str(readelf), "-dW", str(path)]).stdout
    versions = run([str(readelf), "--version-info", "-W", str(path)]).stdout
    needed = sorted(set(re.findall(r"Shared library: \[([^\]]+)\]", dynamic)))
    sonames = re.findall(r"Library soname: \[([^\]]+)\]", dynamic)
    rpaths = re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]*)\]", dynamic)
    tokens = sorted(set(re.findall(r"\bName: ((?:GLIBC|GLIBCXX|CXXABI|GCC)_[A-Za-z0-9_.]+)", versions)))
    try:
        maxima = audit_version_tokens(tokens, strict=strict_abi)
        audit_rpaths(rpaths, strict=strict_abi)
    except ProbeError as exc:
        raise ProbeError(f"{path}: {exc}") from exc
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "soname": sonames[0] if len(sonames) == 1 else None,
        "needed": needed,
        "rpath": rpaths,
        "required_versions": tokens,
        "version_max": maxima,
    }


def build_provider_index(prefix: Path, readelf: Path) -> tuple[dict[str, Path], dict[Path, dict]]:
    library_root = prefix / "lib"
    if not library_root.is_dir():
        raise ProbeError(f"locked prefix has no lib directory: {prefix}")
    providers: dict[str, Path] = {}
    audits: dict[Path, dict] = {}
    for path in sorted(library_root.iterdir()):
        if path.is_symlink() or not is_elf(path):
            continue
        audit = read_elf(path, readelf)
        audits[path.resolve()] = audit
        keys = {path.name}
        if audit["soname"]:
            keys.add(audit["soname"])
        for key in keys:
            prior = providers.get(key)
            if prior is not None and prior.resolve() != path.resolve():
                raise ProbeError(f"multiple locked providers for {key}: {prior} and {path}")
            providers[key] = path.resolve()
    for path in sorted(library_root.iterdir()):
        if not path.is_symlink():
            continue
        target = path.resolve(strict=False)
        if target in audits:
            prior = providers.get(path.name)
            if prior is not None and prior.resolve() != target:
                raise ProbeError(f"ambiguous locked symlink provider for {path.name}")
            providers[path.name] = target
    return providers, audits


def candidate_curl_provider(install_prefix: Path, readelf: Path) -> tuple[Path, dict, dict[str, Path]]:
    library_root = install_prefix / "lib"
    real = [path for path in sorted(library_root.glob("libcurl.so*")) if not path.is_symlink() and is_elf(path)]
    if len(real) != 1:
        raise ProbeError(f"expected one rebuilt real libcurl ELF, found {len(real)}: {real}")
    path = real[0].resolve()
    audit = read_elf(path, readelf, strict_abi=True)
    if audit["soname"] != "libcurl.so.4":
        raise ProbeError(f"rebuilt curl SONAME changed: {audit['soname']!r}")
    aliases = {"libcurl.so.4": path, path.name: path}
    for link in sorted(library_root.glob("libcurl.so*")):
        if link.is_symlink() and link.resolve(strict=False) == path:
            aliases[link.name] = path
    return path, audit, aliases


def find_root(prefix: Path, pattern: str) -> Path:
    candidates = [path.resolve() for path in sorted((prefix / "lib").glob(pattern)) if not path.is_symlink() and is_elf(path)]
    unique = sorted(set(candidates))
    if len(unique) != 1:
        raise ProbeError(f"expected one ELF root for {pattern}, found {len(unique)}: {unique}")
    return unique[0]


def closure(root: Path, providers: dict[str, Path], readelf: Path, external_allowlist: set[str]) -> dict:
    pending = [root.resolve()]
    visited: dict[Path, dict] = {}
    edges = []
    unresolved = []
    external = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        audit = read_elf(current, readelf)
        visited[current] = audit
        for needed in audit["needed"]:
            provider = providers.get(needed)
            if provider is not None:
                provider = provider.resolve()
                edges.append({"consumer": str(current), "needed": needed, "provider": str(provider)})
                if provider not in visited:
                    pending.append(provider)
            elif needed in external_allowlist:
                external.add(needed)
            else:
                unresolved.append({"consumer": str(current), "needed": needed})
    if unresolved:
        raise ProbeError(f"native closure has unresolved non-system dependencies: {unresolved}")
    reached_names = set()
    for path, audit in visited.items():
        reached_names.add(path.name)
        if audit["soname"]:
            reached_names.add(audit["soname"])
    return {
        "root": str(root),
        "elf_count": len(visited),
        "paths": sorted(str(path) for path in visited),
        "reached_names": sorted(reached_names),
        "edges": sorted(edges, key=lambda item: (item["consumer"], item["needed"], item["provider"])),
        "external": sorted(external),
        "audits": visited,
    }


def has_name_prefix(summary: dict, prefix: str) -> bool:
    return any(name.startswith(prefix) for name in summary["reached_names"])


def validate_closure_contract(
    contract: dict,
    baseline_curl: dict,
    candidate_curl: dict,
    candidate_hdf5: dict,
    mpich_control: dict,
) -> None:
    probe = contract["probe"]
    missing_baseline = [
        prefix for prefix in probe["required_baseline_curl_reachability"] if not has_name_prefix(baseline_curl, prefix)
    ]
    if missing_baseline:
        raise ProbeError(f"stock libcurl closure no longer proves the PSL/ICU edge: missing {missing_baseline}")
    if not has_name_prefix(candidate_hdf5, "libcurl.so"):
        raise ProbeError("candidate HDF5 closure does not reach rebuilt libcurl")
    forbidden = [
        prefix for prefix in probe["forbidden_candidate_curl_reachability"] if has_name_prefix(candidate_curl, prefix)
    ]
    if forbidden:
        raise ProbeError(f"candidate libcurl closure still reaches removed PSL/ICU providers: {forbidden}")
    missing_control = [prefix for prefix in probe["required_mpich_control_reachability"] if not has_name_prefix(mpich_control, prefix)]
    if missing_control:
        raise ProbeError(f"independent MPICH->ICU control disappeared: missing {missing_control}")


def validate_needed_delta(stock: dict, candidate: dict) -> dict:
    stock_needed = set(stock["needed"])
    candidate_needed = set(candidate["needed"])
    removed = sorted(stock_needed - candidate_needed)
    added = sorted(candidate_needed - stock_needed)
    if not removed or any(not name.startswith("libpsl.so") for name in removed):
        raise ProbeError(f"rebuilt libcurl must remove only a libpsl DT_NEEDED edge: removed={removed}")
    if added:
        raise ProbeError(f"rebuilt libcurl introduced new DT_NEEDED edges: {added}")
    if any(name.startswith(("libpsl.so", "libicu")) for name in candidate_needed):
        raise ProbeError(f"rebuilt libcurl retains forbidden PSL/ICU DT_NEEDED: {sorted(candidate_needed)}")
    if "libstdc++.so.6" in candidate_needed:
        raise ProbeError("rebuilt C libcurl unexpectedly depends on libstdc++.so.6")
    return {"removed": removed, "added": added}


def safe_extract(source: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(source, "r:bz2") as archive:
        members = archive.getmembers()
        for member in members:
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ProbeError(f"unsafe curl source archive path: {member.name}")
        archive.extractall(destination, filter="data")
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1 or roots[0].name != "curl-8.23.0":
        raise ProbeError(f"unexpected curl source archive layout: {roots}")
    return roots[0]


def build_curl(contract: dict, source: Path, locked_prefix: Path, build_tool_prefix: Path, scratch: Path) -> tuple[Path, dict]:
    source_root = safe_extract(source, scratch / "source")
    install_prefix = scratch / "install"
    tools = {
        "cc": build_tool_prefix / "bin" / "x86_64-conda-linux-gnu-gcc",
        "ar": build_tool_prefix / "bin" / "x86_64-conda-linux-gnu-ar",
        "ranlib": build_tool_prefix / "bin" / "x86_64-conda-linux-gnu-ranlib",
        "make": build_tool_prefix / "bin" / "make",
        "pkg_config": build_tool_prefix / "bin" / "pkg-config",
    }
    for name, tool in tools.items():
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise ProbeError(f"verified build-tool prefix is missing {name}: {tool}")
    for required in (locked_prefix / "lib", locked_prefix / "include", locked_prefix / "ssl" / "cacert.pem"):
        if not required.exists():
            raise ProbeError(f"locked prefix is missing curl build input: {required}")

    home = scratch / "home"
    home.mkdir()
    env = {
        "PATH": os.pathsep.join([str(build_tool_prefix / "bin"), str(locked_prefix / "bin"), "/usr/bin", "/bin"]),
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "CC": str(tools["cc"]),
        "AR": str(tools["ar"]),
        "RANLIB": str(tools["ranlib"]),
        "PKG_CONFIG": str(tools["pkg_config"]),
        "PKG_CONFIG_PATH": os.pathsep.join(
            [str(locked_prefix / "lib" / "pkgconfig"), str(locked_prefix / "share" / "pkgconfig")]
        ),
        "PKG_CONFIG_LIBDIR": os.pathsep.join(
            [str(locked_prefix / "lib" / "pkgconfig"), str(locked_prefix / "share" / "pkgconfig")]
        ),
        "CPPFLAGS": f"-I{locked_prefix / 'include'}",
        "CFLAGS": "-O2 -fPIC",
        "LDFLAGS": f"-L{locked_prefix / 'lib'}",
        "LD_LIBRARY_PATH": str(locked_prefix / "lib"),
    }
    configure = [str(source_root / "configure"), f"--prefix={install_prefix}", f"--host={contract['probe']['host_triplet']}"]
    configure.extend(flag.format(locked_prefix=locked_prefix) for flag in contract["probe"]["configure_flags"])
    configure_result = run(configure, cwd=source_root, env=env)
    run([str(tools["make"]), "-j2"], cwd=source_root, env=env)
    run([str(tools["make"]), "install"], cwd=source_root, env=env)
    return install_prefix, {
        "configure": configure,
        "configure_output_sha256": hashlib.sha256(configure_result.stdout.encode("utf-8")).hexdigest(),
        "configure_delta_from_feedstock": contract["probe"]["configure_delta_from_feedstock"],
    }


def external_allowlist(base_provenance_path: Path) -> set[str]:
    data = load_json(base_provenance_path)
    baseline = data.get("baseline")
    values = baseline.get("pep599_external_allowlist") if isinstance(baseline, dict) else None
    if not isinstance(values, list) or not values or any(not isinstance(item, str) for item in values):
        raise ProbeError("public NumPy base provenance has no PEP 599 external allowlist")
    result = set(values)
    result.add("ld-linux-x86-64.so.2")
    return result


def audit_candidate_closure(summary: dict, readelf: Path) -> list[dict]:
    records = []
    for raw in summary["paths"]:
        records.append(read_elf(Path(raw), readelf, strict_abi=True))
    return records


def run_linker_resolution_probe(hdf5: Path, candidate_lib: Path, locked_prefix: Path) -> str:
    env = {
        "PATH": "/usr/bin:/bin",
        "LD_LIBRARY_PATH": os.pathsep.join([str(candidate_lib), str(locked_prefix / "lib")]),
        "LD_BIND_NOW": "1",
    }
    output = run(["ldd", "-r", str(hdf5)], env=env).stdout
    if "not found" in output:
        raise ProbeError(f"ldd -r reported an unresolved candidate HDF5 dependency:\n{output}")
    return hashlib.sha256(output.encode("utf-8")).hexdigest()


def run_probe(
    contract_path: Path,
    input_dir: Path,
    runtime_lock: Path,
    locked_prefix: Path,
    build_tool_prefix: Path,
    base_provenance: Path,
    prefix_verification: Path,
    build_tool_verification: Path,
    stock_abi_preflight: Path,
    scratch: Path,
    evidence_path: Path,
) -> None:
    contract = load_contract(contract_path)
    hosted_container = verify_hosted_container()
    lock_evidence = verify_runtime_lock(contract, runtime_lock)
    input_evidence = verify_input_bundle(contract, input_dir)
    hosted_evidence = validate_hosted_evidence(prefix_verification, build_tool_verification, stock_abi_preflight)
    if scratch.exists():
        if any(scratch.iterdir()):
            raise ProbeError(f"ICU-edge scratch root must start empty: {scratch}")
    else:
        scratch.mkdir(parents=True)

    build_tool_prefix = build_tool_prefix.resolve()
    readelf = build_tool_prefix / "bin" / "x86_64-conda-linux-gnu-readelf"
    if not readelf.is_file() or not os.access(readelf, os.X_OK):
        raise ProbeError(f"verified build-tool prefix is missing readelf: {readelf}")
    locked_prefix = locked_prefix.resolve()
    allowlist = external_allowlist(base_provenance)
    providers, stock_audits = build_provider_index(locked_prefix, readelf)

    source = input_dir / contract["curl_source"]["filename"]
    install_prefix, build_evidence = build_curl(contract, source, locked_prefix, build_tool_prefix, scratch)
    candidate_curl, candidate_audit, candidate_aliases = candidate_curl_provider(install_prefix, readelf)

    stock_curl_path = providers.get("libcurl.so.4")
    if stock_curl_path is None:
        raise ProbeError("stock locked prefix has no libcurl.so.4 provider")
    stock_curl_audit = stock_audits.get(stock_curl_path.resolve()) or read_elf(stock_curl_path, readelf)
    needed_delta = validate_needed_delta(stock_curl_audit, candidate_audit)

    hdf5 = find_root(locked_prefix, "libhdf5.so.*")
    mpi = find_root(locked_prefix, "libmpi.so.*")
    baseline_curl = closure(stock_curl_path, providers, readelf, allowlist)
    baseline_hdf5 = closure(hdf5, providers, readelf, allowlist)
    candidate_providers = dict(providers)
    candidate_providers.update(candidate_aliases)
    candidate_curl_closure = closure(candidate_curl, candidate_providers, readelf, allowlist)
    candidate_hdf5 = closure(hdf5, candidate_providers, readelf, allowlist)
    mpich_control = closure(mpi, candidate_providers, readelf, allowlist)
    validate_closure_contract(contract, baseline_curl, candidate_curl_closure, candidate_hdf5, mpich_control)

    candidate_path_set = {Path(raw).resolve() for raw in candidate_hdf5["paths"]}
    if candidate_curl.resolve() not in candidate_path_set:
        raise ProbeError("HDF5 candidate closure resolved libcurl somewhere other than the rebuilt provider")
    closure_abi = audit_candidate_closure(candidate_curl_closure, readelf)
    ldd_sha256 = run_linker_resolution_probe(hdf5, install_prefix / "lib", locked_prefix)

    feature_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(scratch / "home"),
        "LANG": "C",
        "LC_ALL": "C",
        "LD_LIBRARY_PATH": os.pathsep.join([str(install_prefix / "lib"), str(locked_prefix / "lib")]),
    }
    curl_binary = install_prefix / "bin" / "curl"
    if not curl_binary.is_file():
        raise ProbeError("rebuilt curl installation did not produce the curl feature probe executable")
    curl_version = run([str(curl_binary), "--version"], env=feature_env).stdout
    feature_line = next((line for line in curl_version.splitlines() if line.startswith("Features:")), "")
    if not feature_line or re.search(r"(?:^|\s)PSL(?:\s|$)", feature_line):
        raise ProbeError(f"rebuilt curl still reports PSL capability: {feature_line!r}")

    evidence = {
        "format_version": 1,
        "kind": "linux-fenics-libcurl-without-libpsl-icu-edge-result",
        "status": "pass-experimental-closure-only",
        "release_eligible": False,
        "contract": {"path": str(contract_path.resolve()), "sha256": sha256_file(contract_path)},
        "hosted_container": hosted_container,
        "runtime_lock": lock_evidence,
        "inputs": input_evidence,
        "hosted_verification": hosted_evidence,
        "build": build_evidence,
        "stock_libcurl": stock_curl_audit,
        "candidate_libcurl": candidate_audit,
        "dt_needed_delta": needed_delta,
        "baseline_libcurl_closure": {key: value for key, value in baseline_curl.items() if key != "audits"},
        "candidate_libcurl_closure": {key: value for key, value in candidate_curl_closure.items() if key != "audits"},
        "baseline_hdf5_closure": {key: value for key, value in baseline_hdf5.items() if key != "audits"},
        "candidate_hdf5_closure": {key: value for key, value in candidate_hdf5.items() if key != "audits"},
        "mpich_icu_control": {key: value for key, value in mpich_control.items() if key != "audits"},
        "candidate_libcurl_strict_abi": closure_abi,
        "ldd_r_output_sha256": ldd_sha256,
        "curl_feature_line": feature_line,
        "capability_impact": contract["capability_impact"],
        "remaining_blocker": "MPICH->hwloc->libxml2->ICU intentionally remains live; final ICU omission requires the separately pinned libxml2 ICU-off rebuild and full overlay qualification",
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="fetch and hash pinned public curl/feedstock inputs")
    fetch.add_argument("--contract", type=Path, required=True)
    fetch.add_argument("--output-dir", type=Path, required=True)
    fetch.add_argument("--evidence", type=Path, required=True)

    probe = subparsers.add_parser("probe", help="build curl offline and compare the ICU closure edge")
    probe.add_argument("--contract", type=Path, required=True)
    probe.add_argument("--input-dir", type=Path, required=True)
    probe.add_argument("--runtime-lock", type=Path, required=True)
    probe.add_argument("--locked-prefix", type=Path, required=True)
    probe.add_argument("--build-tool-prefix", type=Path, required=True)
    probe.add_argument("--base-provenance", type=Path, required=True)
    probe.add_argument("--prefix-verification", type=Path, required=True)
    probe.add_argument("--build-tool-verification", type=Path, required=True)
    probe.add_argument("--stock-abi-preflight", type=Path, required=True)
    probe.add_argument("--scratch-root", type=Path, required=True)
    probe.add_argument("--evidence", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "fetch":
            fetch_inputs(args.contract.resolve(), args.output_dir, args.evidence.resolve())
            print("icu_edge_public_inputs=pass")
            print(f"evidence={args.evidence.resolve()}")
            return 0
        run_probe(
            args.contract.resolve(),
            args.input_dir.resolve(),
            args.runtime_lock.resolve(),
            args.locked_prefix.resolve(),
            args.build_tool_prefix.resolve(),
            args.base_provenance.resolve(),
            args.prefix_verification.resolve(),
            args.build_tool_verification.resolve(),
            args.stock_abi_preflight.resolve(),
            args.scratch_root.resolve(),
            args.evidence.resolve(),
        )
        print("icu_edge_hdf5_libcurl_without_libpsl=pass")
        print("icu_edge_mpich_control=live")
        print(f"evidence={args.evidence.resolve()}")
        return 0
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
