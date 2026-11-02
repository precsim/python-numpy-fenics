#!/usr/bin/env python3
"""Fetch and prepare the pinned public Python wheel-builder for Linux FEniCS."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
from email.policy import compat32
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import zipfile


EXPECTED_WHEELS = {
    "pip": ("25.3", "pip-25.3-py3-none-any.whl", "9655943313a94722b7774661c21049070f6bbb0a1516bf02f7c8d5d9201514cd"),
    "packaging": ("25.0", "packaging-25.0-py3-none-any.whl", "29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484"),
    "pyproject-metadata": ("0.9.1", "pyproject_metadata-0.9.1-py3-none-any.whl", "ee5efde548c3ed9b75a354fc319d5afd25e9585fa918a34f62f904cc731973ad"),
    "pathspec": ("0.12.1", "pathspec-0.12.1-py3-none-any.whl", "a0d503e138a4c123b27490a4f7beda6a01c6f288df0e4a8b79c7eb0dc7b4cc08"),
    "scikit-build-core": ("0.11.6", "scikit_build_core-0.11.6-py3-none-any.whl", "ce6d8fe64e6b4c759ea0fb95d2f8a68f60d2df31c2989838633b8ec930736360"),
    "nanobind": ("2.9.2", "nanobind-2.9.2-py3-none-any.whl", "c37957ffd5eac7eda349cff3622ecd32e5ee1244ecc912c99b5bc8188bafd16e"),
    "wheel": ("0.48.0", "wheel-0.48.0-py3-none-any.whl", "3217dcc807155e45db462d7ef2431f5ddda0d7273b700d05a67b271ceb1287ab"),
}
EXPECTED_ORDER = tuple(EXPECTED_WHEELS)
EXPECTED_MODULES = {name: record[0] for name, record in EXPECTED_WHEELS.items()}
EXPECTED_ACTIVE_DEPENDENCIES = {
    "pip": (),
    "packaging": (),
    "pyproject-metadata": ("packaging",),
    "pathspec": (),
    "scikit-build-core": ("packaging", "pathspec"),
    "nanobind": (),
    "wheel": ("packaging",),
}
PYTHON_IMPORTS = {
    "pip": "pip",
    "packaging": "packaging",
    "pyproject-metadata": "pyproject_metadata",
    "pathspec": "pathspec",
    "scikit-build-core": "scikit_build_core",
    "nanobind": "nanobind",
    "wheel": "wheel",
}
PUBLIC_WHEEL_HOST = "files.pythonhosted.org"
DIST_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")
EXPECTED_SITE_TREE_SHA256 = "de8582981bdeea3c178eeb71265427229a66ceb421ad435c24136cce67b94147"
EXPECTED_SITE_FILE_COUNT = 725


class BuilderInputError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuilderInputError(f"cannot read JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BuilderInputError(f"JSON input must contain an object: {path}")
    return value


def normalize_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def wheel_records(lock: dict) -> dict[str, dict]:
    records = lock.get("wheels")
    if not isinstance(records, list):
        raise BuilderInputError("Python builder wheel lock has no wheels list")
    result: dict[str, dict] = {}
    order: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("name"), str):
            raise BuilderInputError("Python builder wheel lock contains an invalid record")
        name = normalize_name(record["name"])
        if name in result:
            raise BuilderInputError(f"duplicate Python builder wheel record: {name}")
        result[name] = record
        order.append(name)
    if tuple(order) != EXPECTED_ORDER:
        raise BuilderInputError(f"Python builder wheel order must be exactly {EXPECTED_ORDER!r}")
    return result


def validate_lock(lock_path: Path, numpy_sources_path: Path, fenics_package_lock_path: Path) -> tuple[dict, dict[str, dict]]:
    lock = load_json(lock_path)
    if lock.get("schema_version") != 1:
        raise BuilderInputError("unsupported Python builder wheel-lock schema")
    if lock.get("target") != {
        "architecture": "x86_64",
        "python_version": "3.12.10",
        "python_abi": "cp312",
        "glibc_floor": "2.17",
    }:
        raise BuilderInputError("Python builder target differs from the frozen Linux CPython contract")
    if lock.get("base_runtime") != {
        "profile": "numpy",
        "source_manifest": "locks/linux/linux-glibc217-numpy-sources.json",
    }:
        raise BuilderInputError("Python builder base-runtime identity differs from the NumPy profile")
    policy = lock.get("policy")
    expected_policy = {
        "dependency_resolution": False,
        "index_access": False,
        "network_access_after_input_verification": False,
        "wheel_installation": "deterministic-pure-wheel-extraction",
        "base_runtime_modified": False,
    }
    if policy != expected_policy:
        raise BuilderInputError("Python builder policy differs from the fail-closed offline contract")
    if lock.get("modules") != EXPECTED_MODULES:
        raise BuilderInputError("Python builder module versions differ from the fixed wheel closure")
    if lock.get("prepared_site") != {
        "file_count": EXPECTED_SITE_FILE_COUNT,
        "tree_sha256": EXPECTED_SITE_TREE_SHA256,
    }:
        raise BuilderInputError("Python builder prepared-site identity differs from the pinned wheel closure")

    records = wheel_records(lock)
    for name, (version, filename, expected_sha) in EXPECTED_WHEELS.items():
        record = records[name]
        if (record.get("version"), record.get("filename"), record.get("sha256")) != (
            version,
            filename,
            expected_sha,
        ):
            raise BuilderInputError(f"Python builder wheel identity differs for {name}")
        if record.get("active_dependencies") != list(EXPECTED_ACTIVE_DEPENDENCIES[name]):
            raise BuilderInputError(f"Python builder dependency closure differs for {name}")
        url = record.get("url")
        if not isinstance(url, str):
            raise BuilderInputError(f"Python builder wheel has no URL: {name}")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != PUBLIC_WHEEL_HOST
            or parsed.username is not None
            or parsed.password is not None
            or Path(parsed.path).name != filename
        ):
            raise BuilderInputError(f"Python builder wheel URL is not the fixed public PyPI file: {name}")

    numpy_sources = load_json(numpy_sources_path)
    if numpy_sources.get("target", {}).get("python_version") != "3.12.10":
        raise BuilderInputError("NumPy source manifest does not identify CPython 3.12.10")
    numpy_wheels = {
        normalize_name(str(record.get("name"))): record
        for record in numpy_sources.get("python_build_wheels", [])
        if isinstance(record, dict)
    }
    for name in ("pip", "packaging", "pyproject-metadata"):
        source = numpy_wheels.get(name)
        if source is None:
            raise BuilderInputError(f"NumPy source manifest is missing reused Python builder pin: {name}")
        record = records[name]
        for key in ("version", "filename", "url", "sha256"):
            if source.get(key) != record.get(key):
                raise BuilderInputError(f"Python builder {name} pin differs from NumPy source manifest")

    scikit_record = records["scikit-build-core"]
    if scikit_record.get("selected_extras") != ["pyproject"]:
        raise BuilderInputError("scikit-build-core must retain the source-requested pyproject extra")
    if scikit_record.get("extra_compatibility_dependencies") != ["pyproject-metadata"]:
        raise BuilderInputError("scikit-build-core pyproject compatibility closure must include pyproject-metadata")

    fenics_lock = load_json(fenics_package_lock_path)
    fenics_packages = {
        normalize_name(str(record.get("name"))): record
        for record in fenics_lock.get("packages", [])
        if isinstance(record, dict)
    }
    wheel_package = fenics_packages.get("wheel")
    if wheel_package is None or wheel_package.get("version") != records["wheel"].get("version"):
        raise BuilderInputError("Python builder wheel version differs from the immutable FEniCS package lock")
    return lock, records


def fetch_wheel(url: str, destination: Path, expected_sha: str) -> dict:
    partial = destination.with_name(f".{destination.name}.part")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        digest = hashlib.sha256()
        size = 0
        try:
            request = Request(url, headers={"User-Agent": "precsim-fenics-python-builder-fetch/1"})
            with urlopen(request, timeout=90) as response, partial.open("wb") as stream:
                resolved = response.geturl()
                parsed = urlparse(resolved)
                if parsed.scheme != "https" or parsed.hostname != PUBLIC_WHEEL_HOST:
                    raise BuilderInputError(f"wheel redirect left the fixed public PyPI host: {url} -> {resolved}")
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    digest.update(block)
                    size += len(block)
            actual = digest.hexdigest()
            if actual != expected_sha:
                raise BuilderInputError(f"wheel SHA-256 mismatch for {destination.name}: {actual} != {expected_sha}")
            os.replace(partial, destination)
            return {"filename": destination.name, "sha256": actual, "size": size, "url": url, "resolved_url": resolved}
        except Exception as exc:
            last_error = exc
            partial.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(attempt)
    assert last_error is not None
    if isinstance(last_error, BuilderInputError):
        raise last_error
    raise BuilderInputError(f"failed to fetch {url}: {last_error}") from last_error


def fetch_all(lock_path: Path, records: dict[str, dict], output_dir: Path, evidence_path: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise BuilderInputError(f"Python builder wheel directory must start empty: {output_dir}")
    fetched = []
    for name in EXPECTED_ORDER:
        record = records[name]
        fetched.append(fetch_wheel(record["url"], output_dir / record["filename"], record["sha256"]))
    evidence = {
        "format_version": 1,
        "kind": "linux-fenics-python-builder-public-wheel-inputs",
        "dependency_solver_used": False,
        "private_sources_used": False,
        "wheel_lock_sha256": sha256_file(lock_path),
        "wheels": fetched,
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def requirement_name(requirement: str) -> str:
    match = DIST_NAME_RE.match(requirement)
    if match is None:
        raise BuilderInputError(f"unsupported wheel Requires-Dist entry: {requirement!r}")
    return normalize_name(match.group(1))


def active_dependency_names(requirements: list[str]) -> tuple[str, ...]:
    active: set[str] = set()
    target_python = (3, 12)
    for raw in requirements:
        if ";" not in raw:
            active.add(requirement_name(raw))
            continue
        requirement, marker = raw.split(";", 1)
        marker = marker.strip()
        if "extra ==" in marker or "extra !=" in marker:
            continue
        match = re.fullmatch(r"python_version\s*(<=|>=|==|!=|<|>)\s*['\"]([0-9]+)\.([0-9]+)['\"]", marker)
        if match:
            operator, major, minor = match.groups()
            threshold = (int(major), int(minor))
            applies = {
                "<": target_python < threshold,
                "<=": target_python <= threshold,
                ">": target_python > threshold,
                ">=": target_python >= threshold,
                "==": target_python == threshold,
                "!=": target_python != threshold,
            }[operator]
            if applies:
                active.add(requirement_name(requirement))
            continue
        raise BuilderInputError(f"unsupported Python 3.12 dependency marker in wheel metadata: {raw!r}")
    return tuple(sorted(active))


def inspect_wheel(path: Path, record: dict) -> dict:
    if not path.is_file() or path.is_symlink():
        raise BuilderInputError(f"Python builder wheel is missing or not a regular file: {path}")
    actual_sha = sha256_file(path)
    if actual_sha != record["sha256"]:
        raise BuilderInputError(f"Python builder wheel SHA-256 mismatch: {path.name}: {actual_sha} != {record['sha256']}")
    with zipfile.ZipFile(path) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise BuilderInputError(f"expected one wheel METADATA file in {path.name}, found {len(metadata_names)}")
        metadata = BytesParser(policy=compat32).parsebytes(archive.read(metadata_names[0]))
    observed_name = normalize_name(str(metadata.get("Name", "")))
    observed_version = str(metadata.get("Version", ""))
    if (observed_name, observed_version) != (record["name"], record["version"]):
        raise BuilderInputError(
            f"wheel metadata identity mismatch for {path.name}: {(observed_name, observed_version)!r}"
        )
    requirements = metadata.get_all("Requires-Dist", []) or []
    active = active_dependency_names([str(value) for value in requirements])
    expected_active = tuple(sorted(record["active_dependencies"]))
    if active != expected_active:
        raise BuilderInputError(f"active dependency closure mismatch for {record['name']}: {active!r} != {expected_active!r}")
    return {
        "name": record["name"],
        "version": record["version"],
        "filename": path.name,
        "sha256": actual_sha,
        "active_dependencies": list(active),
    }


def verify_wheelhouse(records: dict[str, dict], wheel_dir: Path) -> list[dict]:
    expected_files = {records[name]["filename"] for name in EXPECTED_ORDER}
    actual_files = {path.name for path in wheel_dir.glob("*.whl") if path.is_file()}
    if actual_files != expected_files:
        raise BuilderInputError(
            f"Python builder wheelhouse differs from the fixed closure: missing={sorted(expected_files-actual_files)} extra={sorted(actual_files-expected_files)}"
        )
    return [inspect_wheel(wheel_dir / records[name]["filename"], records[name]) for name in EXPECTED_ORDER]


def safe_extract_wheels(records: dict[str, dict], wheel_dir: Path, site_packages: Path) -> None:
    site_packages.mkdir(parents=True, exist_ok=False)
    written: set[str] = set()
    for name in EXPECTED_ORDER:
        wheel_path = wheel_dir / records[name]["filename"]
        with zipfile.ZipFile(wheel_path) as archive:
            for info in archive.infolist():
                pure = PurePosixPath(info.filename)
                if info.is_dir():
                    continue
                if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                    raise BuilderInputError(f"wheel member escapes builder site-packages: {wheel_path.name}: {info.filename}")
                if any(part.endswith(".data") for part in pure.parts):
                    raise BuilderInputError(f"wheel .data layout is unsupported for deterministic extraction: {wheel_path.name}: {info.filename}")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise BuilderInputError(f"wheel symlink member is unsupported: {wheel_path.name}: {info.filename}")
                relative = pure.as_posix()
                if relative in written:
                    raise BuilderInputError(f"duplicate path across Python builder wheels: {relative}")
                written.add(relative)
                destination = site_packages.joinpath(*pure.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(info))


def tree_evidence(root: Path) -> dict:
    digest = hashlib.sha256()
    files = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise BuilderInputError(f"Python builder site contains a symlink: {path}")
        relative = path.relative_to(root).as_posix()
        item_sha = sha256_file(path)
        size = path.stat().st_size
        digest.update(relative.encode("utf-8") + b"\0" + item_sha.encode("ascii") + b"\0" + str(size).encode("ascii") + b"\n")
        files.append({"path": relative, "sha256": item_sha, "size": size})
    if not files:
        raise BuilderInputError("Python builder site-packages is empty")
    return {"tree_sha256": digest.hexdigest(), "file_count": len(files), "files": files}


def normalized_tar_member_name(raw_name: str) -> str:
    if not raw_name or "\\" in raw_name:
        raise BuilderInputError(f"qualified NumPy archive contains unsafe member path: {raw_name!r}")
    path = PurePosixPath(raw_name)
    normalized = path.as_posix()
    if path.is_absolute() or ".." in path.parts:
        raise BuilderInputError(f"qualified NumPy archive contains unsafe member path: {raw_name!r}")
    return normalized


def validate_base_runtime(base_runtime: Path, base_archive: Path, base_qualification_path: Path) -> tuple[dict, Path]:
    runtime_manifest_path = base_runtime / "runtime-manifest.json"
    runtime_manifest = load_json(runtime_manifest_path)
    if (
        runtime_manifest.get("format_version") != 1
        or runtime_manifest.get("platform") != "linux-x64"
        or runtime_manifest.get("profile") != "numpy"
        or runtime_manifest.get("python_abi") != "cp312"
    ):
        raise BuilderInputError("base runtime manifest is not the qualified Linux x64 NumPy cp312 profile")
    python_records = [
        record for record in runtime_manifest.get("files", [])
        if isinstance(record, dict) and record.get("path") == "bin/python3.12"
    ]
    if len(python_records) != 1 or python_records[0].get("kind") != "file":
        raise BuilderInputError("base runtime manifest does not bind one regular bin/python3.12")

    # The builder executes code from the staged base, including its stdlib and
    # shared libraries. A pinned executable alone cannot establish the staged
    # runtime identity: verify the entire producer manifest before execution.
    manifest_files = runtime_manifest.get("files")
    if not isinstance(manifest_files, list):
        raise BuilderInputError("base runtime manifest has no files array")
    expected_paths: set[str] = set()
    for record in manifest_files:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise BuilderInputError("base runtime manifest has an invalid file record")
        relative = PurePosixPath(record["path"])
        name = relative.as_posix()
        if (
            relative.is_absolute()
            or name in ("", ".", "runtime-manifest.json")
            or ".." in relative.parts
            or "\\" in name
            or name != record["path"]
            or name in expected_paths
        ):
            raise BuilderInputError(f"invalid or duplicate base runtime manifest path: {name!r}")
        expected_paths.add(name)
        path = base_runtime.joinpath(*relative.parts)
        resolved_path = path.resolve()
        resolved_root = base_runtime.resolve()
        if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
            raise BuilderInputError(f"base runtime manifest path escapes qualified runtime: {name}")
        kind = record.get("kind")
        if kind == "file":
            if path.is_symlink() or not path.is_file():
                raise BuilderInputError(f"base runtime manifest regular file missing: {name}")
            if record.get("bytes") != path.stat().st_size or record.get("sha256") != sha256_file(path):
                raise BuilderInputError(f"base runtime file differs from runtime-manifest.json: {name}")
        elif kind == "symlink":
            if not path.is_symlink() or os.readlink(path) != record.get("target"):
                raise BuilderInputError(f"base runtime symlink differs from runtime-manifest.json: {name}")
        else:
            raise BuilderInputError(f"unsupported base runtime manifest file kind: {name}")
    actual_paths = {
        path.relative_to(base_runtime).as_posix()
        for path in base_runtime.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    actual_paths.discard("runtime-manifest.json")
    if actual_paths != expected_paths:
        raise BuilderInputError("staged base runtime file inventory differs from runtime-manifest.json")

    base_python = base_runtime / "bin" / "python3.12"
    if not base_python.is_file() or base_python.is_symlink():
        raise BuilderInputError(f"base runtime Python is missing or not a regular file: {base_python}")
    python_sha = sha256_file(base_python)
    if python_records[0].get("sha256") != python_sha:
        raise BuilderInputError("base runtime Python differs from runtime-manifest.json")

    qualification = load_json(base_qualification_path)
    required_checks = {
        "pinned_manylinux": "pass",
        "glibc_floor": "pass",
        "inherited_core_identity": "pass",
        "numpy_source_build": "pass",
        "private_openblas": "pass",
        "blas_lapack_functional": "pass",
        "native_closure": "pass",
        "private_consumer_checkout": "not-applicable",
    }
    if (
        qualification.get("format_version") != 1
        or qualification.get("platform") != "linux-x64"
        or qualification.get("profile") != "numpy"
        or qualification.get("artifact") != base_archive.name
        or qualification.get("checks") != required_checks
    ):
        raise BuilderInputError("base qualification does not match the required qualified NumPy handoff")
    if sha256_file(base_archive) != qualification.get("artifact_sha256"):
        raise BuilderInputError("base NumPy archive differs from qualification evidence")
    runtime_revision = runtime_manifest.get("producer", {}).get("revision")
    if not isinstance(runtime_revision, str) or qualification.get("producer_revision") != runtime_revision:
        raise BuilderInputError("base runtime and qualification producer revisions differ")
    with tarfile.open(base_archive, "r:gz") as archive:
        matches = []
        seen_archive_paths: set[str] = set()
        for member in archive.getmembers():
            normalized_name = normalized_tar_member_name(member.name)
            if normalized_name != ".":
                if normalized_name in seen_archive_paths:
                    raise BuilderInputError(
                        f"qualified NumPy archive contains duplicate normalized member path: {normalized_name}"
                    )
                seen_archive_paths.add(normalized_name)
            if normalized_name == "runtime-manifest.json":
                if not member.isfile():
                    raise BuilderInputError("qualified NumPy archive runtime-manifest.json is not a regular file")
                matches.append(member)
        if len(matches) != 1:
            raise BuilderInputError("qualified NumPy archive does not contain exactly one runtime-manifest.json")
        stream = archive.extractfile(matches[0])
        if stream is None or stream.read() != runtime_manifest_path.read_bytes():
            raise BuilderInputError("qualified NumPy archive runtime manifest differs from staged base runtime")
    return {
        "profile": "numpy",
        "python_version": "3.12.10",
        "python_abi": "cp312",
        "producer_revision": runtime_revision,
        "runtime_manifest_sha256": sha256_file(runtime_manifest_path),
        "qualified_archive_sha256": qualification["artifact_sha256"],
        "python_sha256": python_sha,
    }, base_python


def validate_builder_python(python: Path, site_packages: Path, expected_modules: dict[str, str]) -> dict:
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONUSERBASE", "LD_LIBRARY_PATH", "LD_PRELOAD"):
        environment.pop(name, None)
    environment.update({
        "PYTHONPATH": str(site_packages),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
    })
    code = (
        "import importlib,importlib.metadata,json,sys; "
        f"dists={expected_modules!r}; imports={PYTHON_IMPORTS!r}; "
        "[importlib.import_module(imports[n]) for n in dists]; "
        "print(json.dumps({'version':list(sys.version_info[:3]),'modules':{n:importlib.metadata.version(n) for n in dists}},sort_keys=True))"
    )
    result = subprocess.run(
        [str(python), "-s", "-c", code],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        raise BuilderInputError(f"prepared Python builder failed import/version validation:\n{result.stdout}")
    try:
        observed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BuilderInputError(f"prepared Python builder returned invalid validation JSON: {result.stdout!r}") from exc
    if observed.get("version") != [3, 12, 10] or observed.get("modules") != expected_modules:
        raise BuilderInputError(f"prepared Python builder identity mismatch: {observed!r}")
    return observed


def relative_manifest_path(manifest: Path, target: Path) -> str:
    return Path(os.path.relpath(target.resolve(), manifest.parent.resolve())).as_posix()


def prepare_builder(
    *,
    lock_path: Path,
    numpy_sources_path: Path,
    fenics_package_lock_path: Path,
    base_runtime: Path,
    base_archive: Path,
    base_qualification_path: Path,
    wheel_dir: Path,
    output_dir: Path,
    manifest_path: Path,
) -> dict:
    lock, records = validate_lock(lock_path, numpy_sources_path, fenics_package_lock_path)
    wheel_evidence = verify_wheelhouse(records, wheel_dir)
    base_evidence, base_python = validate_base_runtime(base_runtime, base_archive, base_qualification_path)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BuilderInputError(f"Python builder output directory must be absent or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir != manifest_path.parent.resolve():
        raise BuilderInputError("Python builder manifest must be written directly inside the output directory")
    site_packages = output_dir / "site-packages"
    safe_extract_wheels(records, wheel_dir, site_packages)
    site_evidence = tree_evidence(site_packages)
    if (
        site_evidence["tree_sha256"] != EXPECTED_SITE_TREE_SHA256
        or site_evidence["file_count"] != EXPECTED_SITE_FILE_COUNT
    ):
        raise BuilderInputError("prepared Python builder site-packages differs from the pinned deterministic extraction")
    observed = validate_builder_python(base_python, site_packages, EXPECTED_MODULES)
    manifest = {
        "format_version": 1,
        "kind": "fenics-python-wheel-builder",
        "target": lock["target"],
        "policy": lock["policy"],
        "base_runtime": base_evidence,
        "python": {
            "path": relative_manifest_path(manifest_path, base_python),
            "sha256": base_evidence["python_sha256"],
        },
        "pythonpath": {
            "path": relative_manifest_path(manifest_path, site_packages),
            "tree_sha256": site_evidence["tree_sha256"],
            "file_count": site_evidence["file_count"],
        },
        "modules": observed["modules"],
        "wheel_lock": {
            "sha256": sha256_file(lock_path),
            "numpy_source_manifest_sha256": sha256_file(numpy_sources_path),
            "fenics_package_lock_sha256": sha256_file(fenics_package_lock_path),
        },
        "wheels": wheel_evidence,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="download the exact SHA-bound public wheel closure")
    fetch.add_argument("--lock", type=Path, required=True)
    fetch.add_argument("--numpy-source-manifest", type=Path, required=True)
    fetch.add_argument("--fenics-package-lock", type=Path, required=True)
    fetch.add_argument("--output-dir", type=Path, required=True)
    fetch.add_argument("--evidence", type=Path, required=True)

    prepare = subparsers.add_parser("prepare", help="prepare the builder offline from already verified wheel bytes")
    prepare.add_argument("--lock", type=Path, required=True)
    prepare.add_argument("--numpy-source-manifest", type=Path, required=True)
    prepare.add_argument("--fenics-package-lock", type=Path, required=True)
    prepare.add_argument("--base-runtime", type=Path, required=True)
    prepare.add_argument("--base-archive", type=Path, required=True)
    prepare.add_argument("--base-qualification", type=Path, required=True)
    prepare.add_argument("--wheel-dir", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()

    try:
        lock_path = args.lock.resolve()
        numpy_sources_path = args.numpy_source_manifest.resolve()
        fenics_package_lock_path = args.fenics_package_lock.resolve()
        lock, records = validate_lock(lock_path, numpy_sources_path, fenics_package_lock_path)
        if args.command == "fetch":
            evidence = fetch_all(lock_path, records, args.output_dir.resolve(), args.evidence.resolve())
            print(f"python_builder_wheel_count={len(evidence['wheels'])}")
            print("dependency_solver_used=false")
            print("private_sources_used=false")
            return 0

        manifest_path = args.manifest.resolve()
        manifest = prepare_builder(
            lock_path=lock_path,
            numpy_sources_path=numpy_sources_path,
            fenics_package_lock_path=fenics_package_lock_path,
            base_runtime=args.base_runtime.resolve(),
            base_archive=args.base_archive.resolve(),
            base_qualification_path=args.base_qualification.resolve(),
            wheel_dir=args.wheel_dir.resolve(),
            output_dir=args.output_dir.resolve(),
            manifest_path=manifest_path,
        )
        print(f"python_builder_manifest={manifest_path}")
        print(f"python_builder_site_sha256={manifest['pythonpath']['tree_sha256']}")
        print("dependency_solver_used=false")
        print("network_access_after_input_verification=false")
        return 0
    except BuilderInputError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
