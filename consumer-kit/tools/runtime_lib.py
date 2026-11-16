"""Shared immutable runtime staging and offline verification helpers.

The consumer kit treats the source-controlled consumer receipt and its pinned
producer pre-release manifest as trust anchors.  It never executes code from a
runtime while establishing its identity, and it never rewrites producer-owned
payload bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Any, Iterable
import zipfile


PRODUCER_REPOSITORY = "precsim/python-numpy-fenics"
PYTHON_ABI = "cp314"
PLATFORMS = {"win-x64", "linux-x64", "macos-arm64", "macos-x64"}
PROFILES = {"core", "numpy", "fenics"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
NUMPY_SITE_PREFIXES = {
    "win-x64": "Lib/site-packages/",
    "linux-x64": "lib/python3.14/site-packages/",
    "macos-arm64": "Python.framework/Versions/3.14/lib/python3.14/site-packages/",
    "macos-x64": "Python.framework/Versions/3.14/lib/python3.14/site-packages/",
}


class ConsumerRuntimeError(ValueError):
    """A runtime receipt, producer asset, or staged tree violates the contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ConsumerRuntimeError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_json_bytes(content: bytes, context: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConsumerRuntimeError(f"invalid JSON for {context}: {exc}") from exc
    require(isinstance(value, dict), f"{context} is not a JSON object")
    return value


def load_json_file(path: Path, context: str) -> dict[str, Any]:
    require(path.is_file(), f"missing {context}: {path}")
    return load_json_bytes(path.read_bytes(), context)


def _sha(value: object, context: str) -> str:
    text = str(value).lower()
    require(SHA256_RE.fullmatch(text) is not None, f"invalid SHA-256 for {context}")
    return text


def _git_sha(value: object, context: str) -> str:
    text = str(value).lower()
    require(GIT_SHA_RE.fullmatch(text) is not None, f"invalid Git revision for {context}")
    return text


def _asset_name(value: object, context: str) -> str:
    name = str(value)
    require(
        bool(name)
        and name == Path(name).name
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and ":" not in name,
        f"invalid release asset name for {context}: {name!r}",
    )
    return name


def _tag(value: object, context: str) -> str:
    tag = str(value)
    require(bool(tag) and SAFE_COMPONENT_RE.fullmatch(tag) is not None, f"invalid release tag for {context}")
    return tag


@dataclass(frozen=True)
class AssetPin:
    tag: str
    name: str
    sha256: str


@dataclass(frozen=True)
class LayerPin:
    profile: str
    producer_revision: str
    artifact: AssetPin
    manifest: AssetPin
    qualification: AssetPin
    manifest_external: bool = True


@dataclass(frozen=True)
class RuntimePlan:
    repository: str
    release_tag: str
    release_revision: str
    version: str
    platform: str
    profile: str
    release_manifest: AssetPin | None
    layers: tuple[LayerPin, ...]
    base_binding: dict[str, Any] | None


@dataclass(frozen=True)
class LayerMaterial:
    pin: LayerPin
    archive_path: Path
    manifest_path: Path | None
    qualification_path: Path
    manifest_bytes: bytes
    manifest: dict[str, Any]
    qualification: dict[str, Any]


@dataclass(frozen=True)
class RuntimeInputs:
    receipt_path: Path
    plan: RuntimePlan
    release_manifest_path: Path | None
    release_manifest: dict[str, Any] | None
    layers: tuple[LayerMaterial, ...]


@dataclass(frozen=True)
class ArchiveFacts:
    modes: dict[str, int]
    directories: dict[str, int | None]


def _record_parent_directories(path: str, directories: dict[str, int | None]) -> None:
    parts = PurePosixPath(path).parts[:-1]
    for index in range(1, len(parts) + 1):
        directories.setdefault(PurePosixPath(*parts[:index]).as_posix(), None)


def read_receipt(path: Path) -> dict[str, Any]:
    receipt = load_json_file(path, "consumer runtime receipt")
    require(receipt.get("format_version") == 1, "unsupported consumer runtime receipt format")
    if "producer_repository" in receipt or "profiles" in receipt:
        require(
            receipt.get("producer_repository") == PRODUCER_REPOSITORY,
            "unexpected producer repository in consumer receipt",
        )
        profiles = receipt.get("profiles")
        require(isinstance(profiles, dict) and bool(profiles), "consumer runtime receipt has no profiles map")
    else:
        require(receipt.get("repository") == PRODUCER_REPOSITORY, "unexpected producer repository in consumer receipt")
        _tag(receipt.get("tag"), "consumer receipt")
        _git_sha(receipt.get("producer_revision"), "consumer receipt")
        _sha(receipt.get("prerelease_manifest_sha256"), "consumer pre-release manifest")
        platforms = receipt.get("platforms")
        require(isinstance(platforms, dict), "consumer runtime receipt has no platform map")
    return receipt


def cache_asset_path(cache: Path, tag: str, name: str) -> Path:
    safe_tag = _tag(tag, "cache")
    safe_name = _asset_name(name, "cache")
    return cache / safe_tag / safe_name


def obtain_asset(
    *,
    repository: str,
    pin: AssetPin,
    cache: Path,
    offline: bool,
) -> Path:
    """Return a hash-verified immutable cache entry, downloading only if allowed."""

    require(repository == PRODUCER_REPOSITORY, "unexpected producer repository")
    path = cache_asset_path(cache, pin.tag, pin.name)
    if path.is_file():
        require(sha256_file(path) == pin.sha256, f"changed pinned cache asset: {pin.tag}/{pin.name}")
        return path
    require(not path.exists() and not path.is_symlink(), f"invalid cache entry: {path}")
    require(not offline, f"offline cache is missing pinned asset: {pin.tag}/{pin.name}")

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-download-", dir=path.parent) as raw:
        temporary = Path(raw)
        subprocess.run(
            [
                "gh",
                "release",
                "download",
                pin.tag,
                "--repo",
                repository,
                "--pattern",
                pin.name,
                "--dir",
                str(temporary),
            ],
            check=True,
        )
        downloaded = temporary / pin.name
        require(downloaded.is_file(), f"release download did not produce pinned asset: {pin.name}")
        require(sha256_file(downloaded) == pin.sha256, f"downloaded pinned asset SHA-256 mismatch: {pin.name}")
        require(not path.exists() and not path.is_symlink(), f"cache entry appeared during download: {path}")
        os.replace(downloaded, path)
    return path


def _release_files(record: dict[str, Any]) -> dict[str, str]:
    raw_files = record.get("files")
    require(isinstance(raw_files, list), "producer pre-release manifest has no files array")
    files: dict[str, str] = {}
    for item in raw_files:
        require(isinstance(item, dict), "producer pre-release manifest has invalid file record")
        name = _asset_name(item.get("name"), "producer pre-release manifest")
        digest = _sha(item.get("sha256"), f"producer release file {name}")
        require(name not in files, f"duplicate producer release asset name: {name}")
        files[name] = digest
    return files


def _qualification_summary(
    record: dict[str, Any], platform: str, profile: str
) -> dict[str, Any]:
    raw = record.get("qualifications")
    require(isinstance(raw, list), "producer pre-release manifest has no qualifications array")
    matches = [
        item
        for item in raw
        if isinstance(item, dict)
        and item.get("platform") == platform
        and item.get("profile") == profile
    ]
    require(len(matches) == 1, f"producer release has no unique {platform}/{profile} qualification")
    return matches[0]


def _current_release_layer(
    record: dict[str, Any], files: dict[str, str], platform: str, profile: str
) -> LayerPin:
    summary = _qualification_summary(record, platform, profile)
    tag = _tag(record.get("tag"), "producer pre-release manifest")
    revision = _git_sha(summary.get("producer_revision"), f"{platform}/{profile} qualification summary")
    require(revision == record.get("producer_revision"), "qualification summary producer revision mismatch")
    artifact_name = _asset_name(summary.get("artifact"), f"{platform}/{profile} artifact")
    artifact_sha = _sha(summary.get("artifact_sha256"), f"{platform}/{profile} artifact")
    manifest_name = _asset_name(summary.get("manifest"), f"{platform}/{profile} manifest")
    manifest_sha = _sha(summary.get("manifest_sha256"), f"{platform}/{profile} manifest")
    qualification_name = f"qualification-{platform}-{profile}.json"
    require(files.get(artifact_name) == artifact_sha, f"producer file table disagrees with {platform}/{profile} artifact")
    require(files.get(manifest_name) == manifest_sha, f"producer file table disagrees with {platform}/{profile} manifest")
    qualification_sha = files.get(qualification_name)
    require(qualification_sha is not None, f"producer release is missing {qualification_name}")
    return LayerPin(
        profile=profile,
        producer_revision=revision,
        artifact=AssetPin(tag, artifact_name, artifact_sha),
        manifest=AssetPin(tag, manifest_name, manifest_sha),
        qualification=AssetPin(tag, qualification_name, qualification_sha),
    )


def _version_from_tag(tag: str, context: str) -> str:
    match = re.fullmatch(r"runtime-v(\d+\.\d+\.\d+)-rc\d+", tag)
    require(match is not None, f"invalid producer release tag for {context}: {tag}")
    return match.group(1)


def _contract_layer(record: object, *, profile: str, context: str) -> LayerPin:
    require(isinstance(record, dict), f"consumer receipt has no {context} record")
    tag = _tag(record.get("release_tag"), context)
    _version_from_tag(tag, context)
    revision = _git_sha(record.get("producer_revision"), context)
    archive = record.get("archive")
    require(isinstance(archive, dict), f"consumer receipt has no archive for {context}")
    artifact_name = _asset_name(archive.get("name"), f"{context} archive")
    artifact_sha = _sha(archive.get("sha256"), f"{context} archive")
    manifest_name = _asset_name(archive.get("manifest_name"), f"{context} embedded manifest")
    expected_manifest_name = "fenics-embed-manifest.json" if profile == "fenics" else "runtime-manifest.json"
    require(
        manifest_name == expected_manifest_name,
        f"consumer receipt names the wrong embedded manifest for {context}: {manifest_name}",
    )
    manifest_sha = _sha(archive.get("manifest_sha256"), f"{context} embedded manifest")
    qualification_name = _asset_name(archive.get("qualification_name"), f"{context} qualification")
    qualification_sha = _sha(archive.get("qualification_sha256"), f"{context} qualification")
    return LayerPin(
        profile=profile,
        producer_revision=revision,
        artifact=AssetPin(tag, artifact_name, artifact_sha),
        # In the v1 receipt, manifest_name is the member inside the archive,
        # not a separately downloaded producer asset.
        manifest=AssetPin(tag, manifest_name, manifest_sha),
        qualification=AssetPin(tag, qualification_name, qualification_sha),
        manifest_external=False,
    )


def _same_archive(left: LayerPin, right: LayerPin) -> bool:
    return (
        left.producer_revision == right.producer_revision
        and left.artifact == right.artifact
        and left.manifest.sha256 == right.manifest.sha256
        and left.qualification == right.qualification
    )


def resolve_contract_plan(
    receipt: dict[str, Any], *, platform: str, profile: str
) -> RuntimePlan:
    """Resolve the versioned consumer-runtime-receipt-v1 contract directly."""

    require(platform in PLATFORMS, f"unsupported runtime platform: {platform}")
    require(profile in PROFILES, f"unsupported runtime profile: {profile}")
    require(
        receipt.get("producer_repository") == PRODUCER_REPOSITORY,
        "unexpected producer repository in consumer receipt",
    )
    platform_profiles = receipt.get("profiles", {}).get(platform)
    require(isinstance(platform_profiles, dict), f"consumer receipt does not support platform: {platform}")
    profile_record = platform_profiles.get(profile)
    require(isinstance(profile_record, dict), f"consumer receipt does not support profile: {platform}/{profile}")
    current = _contract_layer(profile_record, profile=profile, context=f"{platform}/{profile}")
    version = _version_from_tag(current.artifact.tag, f"{platform}/{profile}")

    base_binding: dict[str, Any] | None = None
    if profile == "fenics":
        base = _contract_layer(profile_record.get("base"), profile="numpy", context=f"{platform}/fenics base")
        overlay = _contract_layer(profile_record.get("overlay"), profile="fenics", context=f"{platform}/fenics overlay")
        require(
            _same_archive(current, overlay),
            "FEniCS profile archive must identify the same immutable asset as its overlay layer",
        )
        require(
            _version_from_tag(base.artifact.tag, f"{platform}/fenics base") == version,
            "FEniCS NumPy base release version differs from overlay release version",
        )
        base_binding = {
            "numpy_base_release_tag": base.artifact.tag,
            "numpy_base_producer_revision": base.producer_revision,
            "numpy_artifact": base.artifact.name,
            "numpy_artifact_sha256": base.artifact.sha256,
            "numpy_manifest_sha256": base.manifest.sha256,
            "fenics_artifact": overlay.artifact.name,
            "fenics_artifact_sha256": overlay.artifact.sha256,
        }
        layers = (base, overlay)
    else:
        require(
            "base" not in profile_record and "overlay" not in profile_record,
            f"{platform}/{profile} receipt unexpectedly declares layered runtime inputs",
        )
        layers = (current,)

    return RuntimePlan(
        repository=PRODUCER_REPOSITORY,
        release_tag=current.artifact.tag,
        release_revision=current.producer_revision,
        version=version,
        platform=platform,
        profile=profile,
        release_manifest=None,
        layers=layers,
        base_binding=base_binding,
    )


def resolve_plan(
    receipt: dict[str, Any],
    release: dict[str, Any],
    *,
    platform: str,
    profile: str,
) -> RuntimePlan:
    require(platform in PLATFORMS, f"unsupported runtime platform: {platform}")
    require(profile in PROFILES, f"unsupported runtime profile: {profile}")
    release_tag = _tag(receipt.get("tag"), "consumer receipt")
    release_revision = _git_sha(receipt.get("producer_revision"), "consumer receipt")
    require(
        release.get("format_version") == 1
        and release.get("tag") == release_tag
        and release.get("producer_repository") == receipt.get("repository")
        and release.get("producer_revision") == release_revision,
        "pinned producer pre-release manifest identity mismatch",
    )
    version = str(release.get("version", ""))
    require(re.fullmatch(r"\d+\.\d+\.\d+", version) is not None, "producer pre-release manifest has invalid version")
    files = _release_files(release)
    current = _current_release_layer(release, files, platform, profile)
    selected = receipt.get("platforms", {}).get(platform)
    require(isinstance(selected, dict), f"consumer receipt does not support platform: {platform}")

    base_binding: dict[str, Any] | None = None
    layers: tuple[LayerPin, ...]
    if profile == "fenics":
        fenics_pin = selected.get("fenics")
        base_pin = selected.get("numpy_base")
        require(isinstance(fenics_pin, dict) and isinstance(base_pin, dict), "incomplete FEniCS consumer receipt")
        require(
            current.artifact.name == fenics_pin.get("name")
            and current.artifact.sha256 == str(fenics_pin.get("sha256", "")).lower()
            and current.manifest.sha256 == str(fenics_pin.get("manifest_sha256", "")).lower(),
            "published FEniCS asset differs from consumer receipt",
        )

        raw_bindings = release.get("base_runtime_bindings")
        require(isinstance(raw_bindings, list), "producer pre-release manifest has no base runtime bindings")
        bindings = [
            item
            for item in raw_bindings
            if isinstance(item, dict)
            and item.get("platform") == platform
            and item.get("fenics_artifact") == current.artifact.name
        ]
        require(len(bindings) == 1, "FEniCS release has no unique NumPy base binding")
        base_binding = dict(bindings[0])

        base_tag = _tag(base_pin.get("tag"), "FEniCS NumPy base")
        base_revision = _git_sha(base_pin.get("producer_revision"), "FEniCS NumPy base")
        base_name = _asset_name(base_pin.get("name"), "FEniCS NumPy base")
        base_sha = _sha(base_pin.get("sha256"), "FEnIcs NumPy base artifact")
        base_manifest_sha = _sha(base_pin.get("manifest_sha256"), "FEniCS NumPy base manifest")
        base_qualification_name = _asset_name(base_pin.get("qualification_name"), "FEniCS NumPy base qualification")
        base_qualification_sha = _sha(base_pin.get("qualification_sha256"), "FEniCS NumPy base qualification")
        base_tree_sha = _sha(base_pin.get("numpy_tree_sha256"), "FEniCS NumPy base tree")
        base_entry_count = base_pin.get("numpy_entry_count")
        require(isinstance(base_entry_count, int) and base_entry_count > 0, "invalid FEniCS NumPy base entry count")
        require(
            base_binding.get("numpy_base_release_tag") == base_tag
            and base_binding.get("numpy_base_producer_revision") == base_revision
            and base_binding.get("numpy_artifact") == base_name
            and str(base_binding.get("numpy_artifact_sha256", "")).lower() == base_sha
            and str(base_binding.get("numpy_manifest_sha256", "")).lower() == base_manifest_sha
            and str(base_binding.get("numpy_tree_sha256", "")).lower() == base_tree_sha
            and base_binding.get("numpy_entry_count") == base_entry_count
            and str(base_binding.get("fenics_artifact_sha256", "")).lower() == current.artifact.sha256,
            "FEniCS NumPy base binding does not match pinned prior release",
        )
        base_layer = LayerPin(
            profile="numpy",
            producer_revision=base_revision,
            artifact=AssetPin(base_tag, base_name, base_sha),
            manifest=AssetPin(base_tag, base_name + ".manifest.json", base_manifest_sha),
            qualification=AssetPin(base_tag, base_qualification_name, base_qualification_sha),
        )
        layers = (base_layer, current)
    else:
        layers = (current,)

    return RuntimePlan(
        repository=str(receipt["repository"]),
        release_tag=release_tag,
        release_revision=release_revision,
        version=version,
        platform=platform,
        profile=profile,
        release_manifest=AssetPin(
            release_tag,
            "prerelease-manifest.json",
            _sha(receipt.get("prerelease_manifest_sha256"), "consumer pre-release manifest"),
        ),
        layers=layers,
        base_binding=base_binding,
    )


def _validate_manifest_identity(
    manifest: dict[str, Any], pin: LayerPin, plan: RuntimePlan
) -> None:
    producer = manifest.get("producer")
    require(isinstance(producer, dict), f"{pin.profile} manifest has no producer identity")
    require(
        manifest.get("format_version") == 1
        and manifest.get("version") == _version_from_tag(pin.artifact.tag, f"{pin.profile} manifest")
        and manifest.get("platform") == plan.platform
        and manifest.get("profile") == pin.profile
        and manifest.get("python_abi") == PYTHON_ABI
        and producer.get("repository") == plan.repository
        and producer.get("revision") == pin.producer_revision,
        f"{pin.profile} runtime manifest identity mismatch",
    )
    files = manifest.get("files")
    require(isinstance(files, list), f"{pin.profile} runtime manifest has no file inventory")
    tcl_tk = manifest.get("tcl_tk")
    require(
        isinstance(tcl_tk, dict)
        and tcl_tk.get("version") == "8.6.18"
        and tcl_tk.get("ownership") == "producer",
        f"{pin.profile} runtime does not bind producer Tcl/Tk 8.6.18",
    )


def _validate_qualification(
    qualification: dict[str, Any], pin: LayerPin, plan: RuntimePlan
) -> None:
    checks = qualification.get("checks")
    require(
        qualification.get("format_version") == 1
        and qualification.get("producer_repository") == plan.repository
        and qualification.get("producer_revision") == pin.producer_revision
        and qualification.get("platform") == plan.platform
        and qualification.get("profile") == pin.profile
        and qualification.get("artifact") == pin.artifact.name
        and str(qualification.get("artifact_sha256", "")).lower() == pin.artifact.sha256
        and isinstance(checks, dict)
        and bool(checks)
        and all(value in {"pass", "not-applicable"} for value in checks.values()),
        f"{pin.profile} qualification evidence mismatch",
    )


def _normalize_member_name(raw: str, *, allow_root: bool) -> str | None:
    require(isinstance(raw, str) and "\x00" not in raw and "\\" not in raw and ":" not in raw,
            f"invalid archive member name: {raw!r}")
    value = raw
    while value.startswith("./"):
        value = value[2:]
    value = value.rstrip("/")
    if value in {"", "."}:
        require(allow_root, f"invalid archive member name: {raw!r}")
        return None
    path = PurePosixPath(value)
    parts = value.split("/")
    require(
        not path.is_absolute() and all(part not in {"", ".", ".."} for part in parts),
        f"unsafe archive path: {raw!r}",
    )
    return path.as_posix()


def _safe_symlink_target(root: Path, destination: Path, target: str) -> None:
    require(
        isinstance(target, str)
        and bool(target)
        and "\x00" not in target
        and "\\" not in target
        and ":" not in target
        and not PurePosixPath(target).is_absolute(),
        f"unsafe archive symlink target: {target!r}",
    )
    resolved_root = root.resolve()
    resolved_target = (destination.parent / Path(*PurePosixPath(target).parts)).resolve(strict=False)
    require(
        os.path.commonpath([str(resolved_root), str(resolved_target)]) == str(resolved_root),
        "archive symlink escapes runtime",
    )


def read_embedded_manifest(archive: Path, profile: str) -> bytes:
    manifest_name = "fenics-embed-manifest.json" if profile == "fenics" else "runtime-manifest.json"
    matches: list[bytes] = []
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as opened:
            seen: set[str] = set()
            for item in opened.infolist():
                key = _normalize_member_name(item.filename, allow_root=item.is_dir())
                if key is None:
                    continue
                collision = key.casefold() if os.name == "nt" else key
                require(collision not in seen, f"duplicate archive entry: {key}")
                seen.add(collision)
                if key == manifest_name:
                    require(not item.is_dir(), f"embedded {manifest_name} is not a file")
                    matches.append(opened.read(item))
    else:
        try:
            opened_tar = tarfile.open(archive, "r:gz")
        except tarfile.TarError as exc:
            raise ConsumerRuntimeError(f"unsupported or corrupt runtime archive: {archive.name}") from exc
        with opened_tar as opened:
            seen: set[str] = set()
            for member in opened:
                key = _normalize_member_name(member.name, allow_root=member.isdir())
                if key is None:
                    continue
                collision = key.casefold() if os.name == "nt" else key
                require(collision not in seen, f"duplicate archive entry: {key}")
                seen.add(collision)
                if key == manifest_name:
                    require(member.isfile(), f"embedded {manifest_name} is not a regular file")
                    source = opened.extractfile(member)
                    require(source is not None, f"unreadable embedded {manifest_name}")
                    with source:
                        matches.append(source.read())
    require(len(matches) == 1, f"missing or duplicate embedded {manifest_name}")
    return matches[0]


def _materialize_layer(
    plan: RuntimePlan,
    pin: LayerPin,
    *,
    cache: Path,
    offline: bool,
) -> LayerMaterial:
    archive_path = obtain_asset(repository=plan.repository, pin=pin.artifact, cache=cache, offline=offline)
    qualification_path = obtain_asset(repository=plan.repository, pin=pin.qualification, cache=cache, offline=offline)
    embedded = read_embedded_manifest(archive_path, pin.profile)
    require(
        sha256_bytes(embedded) == pin.manifest.sha256,
        f"embedded {pin.profile} manifest SHA-256 differs from consumer receipt",
    )
    if pin.manifest_external:
        manifest_path = obtain_asset(repository=plan.repository, pin=pin.manifest, cache=cache, offline=offline)
        manifest_bytes = manifest_path.read_bytes()
        require(
            embedded == manifest_bytes,
            f"embedded {pin.profile} manifest differs from pinned producer manifest",
        )
    else:
        manifest_path = None
        manifest_bytes = embedded
    manifest = load_json_bytes(manifest_bytes, f"{pin.profile} runtime manifest")
    qualification = load_json_file(qualification_path, f"{pin.profile} qualification")
    _validate_manifest_identity(manifest, pin, plan)
    _validate_qualification(qualification, pin, plan)
    return LayerMaterial(
        pin=pin,
        archive_path=archive_path,
        manifest_path=manifest_path,
        qualification_path=qualification_path,
        manifest_bytes=manifest_bytes,
        manifest=manifest,
        qualification=qualification,
    )


def numpy_tree_identity(manifest: dict[str, Any], platform: str) -> tuple[str, int]:
    prefix = NUMPY_SITE_PREFIXES[platform]
    entries: dict[str, tuple[str, str]] = {}
    for item in manifest.get("files", []):
        require(isinstance(item, dict), "NumPy base manifest contains a non-object file entry")
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.startswith(prefix):
            continue
        relative = raw_path[len(prefix):]
        if not (
            relative.startswith("numpy/")
            or relative.startswith("numpy.libs/")
            or re.match(r"^numpy-[^/]+\.dist-info/", relative)
        ):
            continue
        require(relative not in entries, f"duplicate NumPy base path: {relative}")
        kind = item.get("kind", "file")
        if kind == "file":
            identity = _sha(item.get("sha256"), f"NumPy base file {relative}")
            entries[relative] = ("F", identity)
        elif kind == "symlink":
            target = item.get("target")
            require(isinstance(target, str) and bool(target), f"invalid NumPy base symlink: {relative}")
            entries[relative] = ("L", target)
        else:
            raise ConsumerRuntimeError(f"unsupported NumPy base entry kind: {relative}")
    require(bool(entries), "NumPy base manifest contains no NumPy payload")
    digest = hashlib.sha256()
    for relative, (kind, identity) in sorted(entries.items()):
        digest.update(kind.encode("ascii") + b"\0" + relative.encode("utf-8") + b"\0" + identity.encode("utf-8") + b"\n")
    return digest.hexdigest(), len(entries)


def _validate_fenics_binding(inputs: RuntimeInputs) -> None:
    plan = inputs.plan
    if plan.profile != "fenics":
        return
    require(len(inputs.layers) == 2 and inputs.layers[0].pin.profile == "numpy" and inputs.layers[1].pin.profile == "fenics",
            "FEniCS runtime must contain a NumPy base followed by one FEniCS overlay")
    base, overlay = inputs.layers
    binding = plan.base_binding
    require(isinstance(binding, dict), "FEniCS runtime has no producer base binding")
    require(
        overlay.manifest.get("tcl_tk") == base.manifest.get("tcl_tk"),
        "FEniCS overlay Tcl/Tk identity differs from its pinned NumPy base",
    )
    tree_sha, entry_count = numpy_tree_identity(base.manifest, plan.platform)
    if "numpy_tree_sha256" in binding or "numpy_entry_count" in binding:
        require(
            tree_sha == str(binding.get("numpy_tree_sha256", "")).lower()
            and entry_count == binding.get("numpy_entry_count"),
            "pinned NumPy base manifest differs from FEniCS release tree binding",
        )
    require(
        binding.get("numpy_base_release_tag") == base.pin.artifact.tag
        and binding.get("numpy_base_producer_revision") == base.pin.producer_revision
        and binding.get("numpy_artifact") == base.pin.artifact.name
        and str(binding.get("numpy_artifact_sha256", "")).lower() == base.pin.artifact.sha256
        and str(binding.get("numpy_manifest_sha256", "")).lower() == base.pin.manifest.sha256
        and binding.get("fenics_artifact") == overlay.pin.artifact.name
        and str(binding.get("fenics_artifact_sha256", "")).lower() == overlay.pin.artifact.sha256,
        "FEniCS receipt/release binding does not match staged layers",
    )
    identity = overlay.manifest.get("numpy")
    require(isinstance(identity, dict), "FEniCS manifest has no NumPy base identity")
    require(
        identity.get("version") == "2.5.2"
        and identity.get("base_producer_revision") == base.pin.producer_revision
        and str(identity.get("base_manifest_sha256", "")).lower() == base.pin.manifest.sha256
        and str(identity.get("base_tree_sha256", "")).lower() == tree_sha
        and identity.get("base_entry_count") == entry_count,
        "FEniCS overlay does not match the pinned NumPy base identity",
    )
    if plan.platform == "win-x64":
        require(
            identity.get("base_release_tag") == base.pin.artifact.tag
            and str(identity.get("base_archive_sha256", "")).lower() == base.pin.artifact.sha256
            and str(identity.get("base_qualification_sha256", base.pin.qualification.sha256)).lower()
            == base.pin.qualification.sha256,
            "Windows FEniCS overlay release base identity mismatch",
        )
    else:
        require(
            identity.get("ownership") == "consumer-numpy-profile"
            and identity.get("producer_payload_excluded") is True,
            "POSIX FEniCS overlay claims unexpected NumPy ownership",
        )


def prepare_runtime_inputs(
    receipt_path: Path,
    *,
    platform: str,
    profile: str,
    cache: Path,
    offline: bool,
) -> RuntimeInputs:
    receipt_path = receipt_path.resolve()
    receipt = read_receipt(receipt_path)
    if "producer_repository" in receipt:
        plan = resolve_contract_plan(receipt, platform=platform, profile=profile)
        release_path = None
        release = None
    else:
        release_pin = AssetPin(
            _tag(receipt.get("tag"), "consumer receipt"),
            "prerelease-manifest.json",
            _sha(receipt.get("prerelease_manifest_sha256"), "consumer pre-release manifest"),
        )
        release_path = obtain_asset(
            repository=str(receipt["repository"]), pin=release_pin, cache=cache, offline=offline
        )
        release = load_json_file(release_path, "producer pre-release manifest")
        plan = resolve_plan(receipt, release, platform=platform, profile=profile)
    layers = tuple(
        _materialize_layer(plan, pin, cache=cache, offline=offline) for pin in plan.layers
    )
    inputs = RuntimeInputs(receipt_path, plan, release_path, release, layers)
    _validate_fenics_binding(inputs)
    return inputs


def _ensure_parent_directories(root: Path, parent: Path) -> None:
    root = root.resolve()
    relative = parent.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ConsumerRuntimeError(f"archive path traverses a symlink: {current}")
        if current.exists():
            require(current.is_dir(), f"archive parent is not a directory: {current}")
        else:
            current.mkdir()


def _claim_destination(
    root: Path,
    path: Path,
    key: str,
    kind: str,
    occupied: dict[str, str],
) -> None:
    collision = key.casefold() if os.name == "nt" else key
    previous = occupied.get(collision)
    if previous is not None:
        require(previous == "dir" and kind == "dir", f"duplicate or overwritten runtime entry: {key}")
        return
    exists = path.is_symlink() or path.exists()
    if exists:
        require(path.is_dir() and not path.is_symlink() and kind == "dir", f"duplicate or overwritten runtime entry: {key}")
    occupied[collision] = kind


def extract_layer(archive: Path, root: Path, occupied: dict[str, str]) -> ArchiveFacts:
    """Safely extract one immutable layer and reject cross-layer collisions."""

    modes: dict[str, int] = {}
    directories: dict[str, int | None] = {}
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as opened:
            for item in opened.infolist():
                key = _normalize_member_name(item.filename, allow_root=item.is_dir())
                if key is None:
                    continue
                path = root.joinpath(*PurePosixPath(key).parts)
                raw_mode = item.external_attr >> 16
                file_type = stat.S_IFMT(raw_mode)
                if item.is_dir():
                    kind = "dir"
                elif file_type == stat.S_IFLNK:
                    kind = "symlink"
                else:
                    require(file_type in {0, stat.S_IFREG}, f"unsupported ZIP file type: {key}")
                    kind = "file"
                _claim_destination(root, path, key, kind, occupied)
                if kind == "dir":
                    _ensure_parent_directories(root, path.parent)
                    path.mkdir(exist_ok=True)
                    mode = stat.S_IMODE(raw_mode)
                    directories[key] = mode or None
                    _record_parent_directories(key, directories)
                    continue
                _record_parent_directories(key, directories)
                _ensure_parent_directories(root, path.parent)
                if kind == "symlink":
                    target = opened.read(item).decode("utf-8")
                    _safe_symlink_target(root, path, target)
                    path.symlink_to(target)
                    continue
                with opened.open(item) as source, path.open("xb") as target_file:
                    shutil.copyfileobj(source, target_file)
                mode = stat.S_IMODE(raw_mode)
                if mode:
                    modes[key] = mode
                    if os.name != "nt":
                        path.chmod(mode)
    else:
        try:
            opened_tar = tarfile.open(archive, "r:gz")
        except tarfile.TarError as exc:
            raise ConsumerRuntimeError(f"unsupported or corrupt runtime archive: {archive.name}") from exc
        with opened_tar as opened:
            for member in opened:
                key = _normalize_member_name(member.name, allow_root=member.isdir())
                if key is None:
                    continue
                path = root.joinpath(*PurePosixPath(key).parts)
                require(member.isdir() or member.isfile() or member.issym(), f"unsupported tar member: {key}")
                kind = "dir" if member.isdir() else "symlink" if member.issym() else "file"
                _claim_destination(root, path, key, kind, occupied)
                if kind == "dir":
                    _ensure_parent_directories(root, path.parent)
                    path.mkdir(exist_ok=True)
                    directories[key] = member.mode & 0o777
                    _record_parent_directories(key, directories)
                    continue
                _record_parent_directories(key, directories)
                _ensure_parent_directories(root, path.parent)
                if kind == "symlink":
                    _safe_symlink_target(root, path, member.linkname)
                    path.symlink_to(member.linkname)
                    continue
                source = opened.extractfile(member)
                require(source is not None, f"unreadable tar member: {key}")
                with source, path.open("xb") as target_file:
                    shutil.copyfileobj(source, target_file)
                mode = member.mode & 0o777
                modes[key] = mode
                if os.name != "nt":
                    path.chmod(mode)
    return ArchiveFacts(modes=modes, directories=directories)


def archive_facts(archive: Path) -> ArchiveFacts:
    """Read hash-verified archive permission metadata without extracting it."""

    modes: dict[str, int] = {}
    directories: dict[str, int | None] = {}
    seen: set[str] = set()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as opened:
            for item in opened.infolist():
                key = _normalize_member_name(item.filename, allow_root=item.is_dir())
                if key is None:
                    continue
                collision = key.casefold() if os.name == "nt" else key
                require(collision not in seen, f"duplicate archive entry: {key}")
                seen.add(collision)
                raw_mode = item.external_attr >> 16
                file_type = stat.S_IFMT(raw_mode)
                if item.is_dir():
                    directories[key] = stat.S_IMODE(raw_mode) or None
                    _record_parent_directories(key, directories)
                    continue
                _record_parent_directories(key, directories)
                if file_type == stat.S_IFLNK:
                    continue
                require(file_type in {0, stat.S_IFREG}, f"unsupported ZIP file type: {key}")
                mode = stat.S_IMODE(raw_mode)
                if mode:
                    modes[key] = mode
    else:
        try:
            opened_tar = tarfile.open(archive, "r:gz")
        except tarfile.TarError as exc:
            raise ConsumerRuntimeError(f"unsupported or corrupt runtime archive: {archive.name}") from exc
        with opened_tar as opened:
            for member in opened:
                key = _normalize_member_name(member.name, allow_root=member.isdir())
                if key is None:
                    continue
                collision = key.casefold() if os.name == "nt" else key
                require(collision not in seen, f"duplicate archive entry: {key}")
                seen.add(collision)
                require(member.isdir() or member.isfile() or member.issym(), f"unsupported tar member: {key}")
                if member.isdir():
                    directories[key] = member.mode & 0o777
                    _record_parent_directories(key, directories)
                    continue
                _record_parent_directories(key, directories)
                if member.isfile():
                    modes[key] = member.mode & 0o777
    return ArchiveFacts(modes=modes, directories=directories)


def _manifest_entries(material: LayerMaterial) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    raw_entries = material.manifest.get("files")
    require(isinstance(raw_entries, list), f"{material.pin.profile} manifest has no files array")
    for raw in raw_entries:
        require(isinstance(raw, dict), f"{material.pin.profile} manifest contains a non-object file entry")
        raw_path = raw.get("path")
        require(isinstance(raw_path, str), f"{material.pin.profile} manifest contains an invalid path")
        path = _normalize_member_name(raw_path, allow_root=False)
        assert path is not None
        require(path not in entries, f"duplicate {material.pin.profile} manifest path: {path}")
        kind = raw.get("kind", "file")
        record = dict(raw)
        record["kind"] = kind
        if kind == "file":
            record["sha256"] = _sha(raw.get("sha256"), f"{material.pin.profile} manifest file {path}")
            size = raw.get("bytes") if raw.get("bytes") is not None else raw.get("size")
            if raw.get("bytes") is not None and raw.get("size") is not None:
                require(raw.get("bytes") == raw.get("size"), f"conflicting byte counts for {material.pin.profile} manifest file {path}")
            require(isinstance(size, int) and size >= 0, f"invalid byte count for {material.pin.profile} manifest file {path}")
            record["bytes"] = size
            mode = raw.get("mode")
            if mode is not None:
                if isinstance(mode, str) and re.fullmatch(r"0o[0-7]{1,3}", mode):
                    mode = int(mode, 8)
                require(isinstance(mode, int) and 0 <= mode <= 0o777, f"invalid mode for {material.pin.profile} manifest file {path}")
                record["mode"] = mode
        elif kind == "symlink":
            target = raw.get("target")
            if isinstance(target, str) and target:
                record["target"] = target
            else:
                # macOS FEniCS overlay manifests bind symlink text by size and
                # SHA-256 rather than duplicating the target string.
                size = raw.get("bytes") if raw.get("bytes") is not None else raw.get("size")
                require(isinstance(size, int) and size >= 0, f"invalid symlink byte count for {material.pin.profile} manifest path {path}")
                record["target_bytes"] = size
                record["target_sha256"] = _sha(raw.get("sha256"), f"{material.pin.profile} manifest symlink {path}")
        else:
            raise ConsumerRuntimeError(f"unsupported {material.pin.profile} manifest entry kind for {path}: {kind!r}")
        entries[path] = record

    manifest_name = "fenics-embed-manifest.json" if material.pin.profile == "fenics" else "runtime-manifest.json"
    require(manifest_name not in entries, f"{material.pin.profile} manifest inventories itself")
    entries[manifest_name] = {
        "path": manifest_name,
        "kind": "file",
        "bytes": len(material.manifest_bytes),
        "sha256": material.pin.manifest.sha256,
    }
    return entries


def _walk_runtime(root: Path) -> dict[str, str]:
    actual: dict[str, str] = {}

    def visit(directory: Path, prefix: PurePosixPath | None = None) -> None:
        with os.scandir(directory) as opened:
            entries = sorted(opened, key=lambda item: item.name)
        for item in entries:
            rel = PurePosixPath(item.name) if prefix is None else prefix / item.name
            name = rel.as_posix()
            if item.is_symlink():
                actual[name] = "symlink"
            elif item.is_file(follow_symlinks=False):
                actual[name] = "file"
            elif item.is_dir(follow_symlinks=False):
                actual[name] = "dir"
                visit(Path(item.path), rel)
            else:
                raise ConsumerRuntimeError(f"staged runtime contains unsupported filesystem entry: {name}")

    visit(root)
    return actual


def verify_staged_runtime(
    inputs: RuntimeInputs,
    root: Path,
    *,
    archive_metadata: Iterable[ArchiveFacts] | None = None,
) -> dict[str, Any]:
    """Re-attest the complete staged file/symlink tree without executing it."""

    root = root.resolve()
    require(root.is_dir(), f"staged runtime does not exist: {root}")
    _validate_fenics_binding(inputs)

    expected: dict[str, tuple[dict[str, Any], int]] = {}
    for layer_index, material in enumerate(inputs.layers):
        for path, record in _manifest_entries(material).items():
            require(path not in expected, f"runtime layers claim the same file or symlink: {path}")
            expected[path] = (record, layer_index)

    facts = tuple(archive_metadata) if archive_metadata is not None else tuple(
        archive_facts(material.archive_path) for material in inputs.layers
    )
    require(len(facts) == len(inputs.layers), "archive metadata does not match runtime layers")
    expected_directories: set[str] = set()
    expected_directory_modes: dict[str, int] = {}
    for layer_facts in facts:
        expected_directories.update(layer_facts.directories)
        for path, mode in layer_facts.directories.items():
            if mode is not None:
                expected_directory_modes[path] = mode

    actual = _walk_runtime(root)
    actual_leaves = {path: kind for path, kind in actual.items() if kind != "dir"}
    actual_directories = {path for path, kind in actual.items() if kind == "dir"}
    if set(actual_leaves) != set(expected):
        missing = sorted(set(expected) - set(actual_leaves))
        extra = sorted(set(actual_leaves) - set(expected))
        raise ConsumerRuntimeError(f"staged runtime inventory differs from producer manifests: missing={missing!r} extra={extra!r}")
    if actual_directories != expected_directories:
        missing = sorted(expected_directories - actual_directories)
        extra = sorted(actual_directories - expected_directories)
        raise ConsumerRuntimeError(f"staged runtime directory tree differs from producer archives: missing={missing!r} extra={extra!r}")
    check_posix_modes = os.name != "nt" and inputs.plan.platform != "win-x64"
    root_resolved = root.resolve()
    for relative, (record, layer_index) in expected.items():
        path = root.joinpath(*PurePosixPath(relative).parts)
        kind = record["kind"]
        require(actual_leaves[relative] == kind, f"staged runtime entry kind mismatch: {relative}")
        if kind == "symlink":
            target = os.readlink(path)
            _safe_symlink_target(root, path, target)
            if "target" in record:
                require(target == record["target"], f"staged runtime symlink target mismatch: {relative}")
            else:
                target_bytes = target.encode("utf-8")
                require(
                    len(target_bytes) == record["target_bytes"]
                    and sha256_bytes(target_bytes) == record["target_sha256"],
                    f"staged runtime symlink target mismatch: {relative}",
                )
            continue
        require(not path.is_symlink() and path.is_file(), f"staged runtime regular file is missing: {relative}")
        require(
            path.stat().st_size == record["bytes"] and sha256_file(path) == record["sha256"],
            f"staged runtime file differs from producer manifest: {relative}",
        )
        resolved = path.resolve()
        require(
            os.path.commonpath([str(root_resolved), str(resolved)]) == str(root_resolved),
            f"staged runtime file escapes runtime root: {relative}",
        )
        if check_posix_modes:
            manifest_mode = record.get("mode")
            archive_mode = facts[layer_index].modes.get(relative)
            if manifest_mode is not None and archive_mode is not None:
                require(manifest_mode == archive_mode, f"producer manifest/archive mode mismatch: {relative}")
            expected_mode = manifest_mode if manifest_mode is not None else archive_mode
            if expected_mode is not None:
                actual_mode = stat.S_IMODE(os.lstat(path).st_mode)
                require(actual_mode == expected_mode, f"staged runtime file mode differs from producer archive: {relative}")

    if check_posix_modes:
        for relative, expected_mode in expected_directory_modes.items():
            path = root.joinpath(*PurePosixPath(relative).parts)
            actual_mode = stat.S_IMODE(os.lstat(path).st_mode)
            require(
                actual_mode == expected_mode,
                f"staged runtime directory mode differs from producer archive: {relative}",
            )

    layers: list[dict[str, Any]] = []
    for material in inputs.layers:
        embedded_manifest_name = (
            "fenics-embed-manifest.json"
            if material.pin.profile == "fenics"
            else "runtime-manifest.json"
        )
        layers.append(
            {
                # Flat aliases keep the staged evidence compatible with the
                # existing reference-package consumer. Nested identities are
                # the stable representation for new downstream tooling.
                "profile": material.pin.profile,
                "tag": material.pin.artifact.tag,
                "release_tag": material.pin.artifact.tag,
                "producer_revision": material.pin.producer_revision,
                "artifact": material.pin.artifact.name,
                "artifact_sha256": material.pin.artifact.sha256,
                "archive": {
                    "name": material.pin.artifact.name,
                    "sha256": material.pin.artifact.sha256,
                },
                "manifest": {
                    "name": embedded_manifest_name,
                    "sha256": material.pin.manifest.sha256,
                },
                "qualification": {
                    "name": material.pin.qualification.name,
                    "sha256": material.pin.qualification.sha256,
                },
            }
        )
    selected = [layer for layer in layers if layer["profile"] == inputs.plan.profile]
    require(len(selected) == 1, "runtime evidence has no unique selected producer layer")
    selected_layer = selected[0]
    return {
        "format_version": 1,
        "producer_repository": inputs.plan.repository,
        "platform": inputs.plan.platform,
        "profile": inputs.plan.profile,
        "python_abi": PYTHON_ABI,
        "release_tag": inputs.plan.release_tag,
        "release_revision": inputs.plan.release_revision,
        "producer_revision": inputs.plan.release_revision,
        "receipt_sha256": sha256_file(inputs.receipt_path),
        "archive": dict(selected_layer["archive"]),
        "manifest": dict(selected_layer["manifest"]),
        "qualification": dict(selected_layer["qualification"]),
        "layers": layers,
        "layer_count": len(inputs.layers),
        "entry_count": len(expected),
        "directory_count": len(expected_directories),
        "status": "pass",
    }


def _apply_directory_modes(root: Path, facts: Iterable[ArchiveFacts], platform: str) -> None:
    if os.name == "nt" or platform == "win-x64":
        return
    final_modes: dict[str, int] = {}
    for layer_facts in facts:
        for path, mode in layer_facts.directories.items():
            if mode is not None:
                final_modes[path] = mode
    # Children are chmodded first so tightening a parent directory cannot make
    # a deeper producer-owned directory temporarily unreachable.
    for relative in sorted(final_modes, key=lambda value: (value.count("/"), value), reverse=True):
        path = root.joinpath(*PurePosixPath(relative).parts)
        path.chmod(final_modes[relative])


def stage_runtime(inputs: RuntimeInputs, output: Path) -> dict[str, Any]:
    """Transactionally stage verified producer layers into a new destination."""

    output = output.resolve()
    require(not output.exists() and not output.is_symlink(), "destination already exists; refusing to overwrite runtime")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-stage-", dir=output.parent) as raw:
        temporary = Path(raw) / "runtime"
        temporary.mkdir()
        occupied: dict[str, str] = {}
        facts = [extract_layer(material.archive_path, temporary, occupied) for material in inputs.layers]
        _apply_directory_modes(temporary, facts, inputs.plan.platform)
        verification = verify_staged_runtime(inputs, temporary, archive_metadata=facts)
        temporary.rename(output)
    result = dict(verification)
    result["runtime"] = str(output)
    return result


def write_evidence_record(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write deterministic JSON evidence for downstream consumers."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as opened:
        temporary = Path(opened.name)
        opened.write(content)
        opened.flush()
        os.fsync(opened.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
