#!/usr/bin/env python3
"""Verify hash-bound macOS runtime native and relocation evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_key(value: str) -> tuple[int, ...]:
    parts = [int(part) for part in value.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def verify(runtime: Path, profile: str, arch: str) -> dict[str, str]:
    runtime = runtime.resolve()
    provenance_path = runtime / "build-provenance-macos.json"
    if not provenance_path.is_file():
        raise SystemExit(f"macOS provenance is missing: {provenance_path}")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if provenance.get("runtime_profile") != profile:
        raise SystemExit("macOS provenance profile mismatch")
    if provenance.get("python", {}).get("architecture") != arch:
        raise SystemExit("macOS provenance architecture mismatch")

    native = provenance.get("native_closure")
    if not isinstance(native, dict) or native.get("status") != "pass":
        raise SystemExit("macOS provenance has no passing native closure binding")
    census_rel = native.get("census_path")
    if not isinstance(census_rel, str) or not census_rel:
        raise SystemExit("macOS native closure census path is invalid")
    census_path = runtime / census_rel
    if not census_path.is_file() or sha256(census_path) != native.get("census_sha256"):
        raise SystemExit("macOS native closure census hash binding is invalid")
    census = json.loads(census_path.read_text(encoding="utf-8"))
    measured = census.get("target_measured_max_minos")
    if (
        census.get("format_version") != 2
        or census.get("status") != "pass"
        or census.get("target_arch") != arch
        or census.get("root") != "."
        or census.get("max_allowed_minos") != "14.0"
        or not isinstance(measured, str)
        or version_key(measured) > (14, 0)
        or census.get("macho_count") != native.get("macho_count")
        or measured != native.get("target_measured_max_minos")
    ):
        raise SystemExit("macOS native closure census metadata is invalid")
    mach_o = census.get("mach_o")
    if (
        not isinstance(mach_o, list)
        or not mach_o
        or len(mach_o) != census.get("macho_count")
    ):
        raise SystemExit("macOS native closure census is empty")
    for entry in mach_o:
        if not isinstance(entry, dict):
            raise SystemExit("macOS native closure contains an invalid Mach-O entry")
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise SystemExit("macOS native closure contains an invalid Mach-O entry")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise SystemExit("macOS native closure contains an unsafe Mach-O path")
        binary = runtime.joinpath(*pure.parts)
        if not binary.is_file() or binary.is_symlink() or sha256(binary) != digest:
            raise SystemExit(f"macOS native closure Mach-O hash mismatch: {relative}")
        if entry.get("target_arch") != arch or arch not in entry.get("architectures", []):
            raise SystemExit(f"macOS native closure architecture mismatch: {relative}")
        minos = entry.get("minos")
        if not isinstance(minos, str) or version_key(minos) > (14, 0):
            raise SystemExit(f"macOS native closure minOS mismatch: {relative}")
        dependencies = entry.get("dependencies")
        resolutions = entry.get("dependency_resolutions")
        if (
            not isinstance(dependencies, list)
            or not isinstance(resolutions, list)
            or len(dependencies) != len(resolutions)
        ):
            raise SystemExit("macOS native closure has an unresolved dependency")
        if [record.get("name") for record in resolutions if isinstance(record, dict)] != dependencies:
            raise SystemExit("macOS native closure dependency records are inconsistent")
        for record in resolutions:
            if not isinstance(record, dict) or record.get("resolution") not in {"system", "bundled"}:
                raise SystemExit("macOS native closure has an unresolved dependency")
            if record["resolution"] != "bundled":
                continue
            provider_rel = record.get("provider")
            provider_sha = record.get("provider_sha256")
            if not isinstance(provider_rel, str) or not isinstance(provider_sha, str):
                raise SystemExit("macOS bundled provider evidence is invalid")
            provider_pure = PurePosixPath(provider_rel)
            if provider_pure.is_absolute() or ".." in provider_pure.parts or not provider_pure.parts:
                raise SystemExit("macOS bundled provider path is unsafe")
            provider = runtime.joinpath(*provider_pure.parts)
            if not provider.is_file() or sha256(provider) != provider_sha:
                raise SystemExit(f"macOS bundled provider hash mismatch: {provider_rel}")

    relocation = provenance.get("relocation_qualification")
    if not isinstance(relocation, dict) or relocation.get("status") != "pass":
        raise SystemExit("macOS provenance has no passing relocation binding")
    evidence_rel = relocation.get("evidence_path")
    if not isinstance(evidence_rel, str) or not evidence_rel:
        raise SystemExit("macOS relocation evidence path is invalid")
    evidence_path = runtime / evidence_rel
    if not evidence_path.is_file() or sha256(evidence_path) != relocation.get("evidence_sha256"):
        raise SystemExit("macOS relocation evidence hash binding is invalid")
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if (
        evidence.get("format_version") != 1
        or evidence.get("status") != "pass"
        or evidence.get("profile") != profile
        or evidence.get("architecture") != arch
        or evidence.get("tcl", {}).get("provider")
        != "Python.framework/Versions/3.12/lib/libtcl8.6.dylib"
        or evidence.get("tk", {}).get("provider")
        != "Python.framework/Versions/3.12/lib/libtk8.6.dylib"
        or evidence.get("tcl", {}).get("script_root") != "tcl/tcl8.6"
        or evidence.get("tk", {}).get("script_root") != "tcl/tk8.6"
    ):
        raise SystemExit("macOS relocation evidence metadata is invalid")
    tcl_tk = provenance.get("tcl_tk", {})
    staged_hashes = tcl_tk.get("staged_hashes", {}) if isinstance(tcl_tk, dict) else {}
    for key, expected_path in (
        ("tcl", "Python.framework/Versions/3.12/lib/libtcl8.6.dylib"),
        ("tk", "Python.framework/Versions/3.12/lib/libtk8.6.dylib"),
    ):
        provider = runtime / expected_path
        provider_sha = sha256(provider) if provider.is_file() else None
        if (
            provider_sha is None
            or evidence.get(key, {}).get("provider_sha256") != provider_sha
            or staged_hashes.get(key) != provider_sha
        ):
            raise SystemExit(f"macOS relocated {key} provider identity is invalid")
    if (
        evidence.get("tcl", {}).get("api_version") != str(tcl_tk.get("tcl_api_version"))
        or evidence.get("tk", {}).get("api_version") != str(tcl_tk.get("tk_api_version"))
        or evidence.get("tcl", {}).get("runtime_version") != str(tcl_tk.get("tcl_runtime_version"))
        or evidence.get("tk", {}).get("runtime_version") != str(tcl_tk.get("tk_runtime_version"))
    ):
        raise SystemExit("macOS relocated Tcl/Tk version identity is invalid")

    if profile == "numpy":
        for key, expected_path_key, expected_name in (
            ("core_native_closure", "census_path", "core-native-census-macos.json"),
            (
                "core_relocation_qualification",
                "evidence_path",
                "core-relocation-qualification-macos.json",
            ),
        ):
            binding = provenance.get(key)
            if not isinstance(binding, dict) or binding.get("status") != "pass":
                raise SystemExit(f"NumPy provenance is missing inherited {key} evidence")
            inherited_rel = binding.get(expected_path_key)
            if inherited_rel != expected_name:
                raise SystemExit(f"NumPy inherited {key} evidence path is invalid")
            inherited = runtime / expected_name
            hash_key = "census_sha256" if key == "core_native_closure" else "evidence_sha256"
            if not inherited.is_file() or sha256(inherited) != binding.get(hash_key):
                raise SystemExit(f"NumPy inherited {key} evidence hash is invalid")

    return {
        "census_sha256": sha256(census_path),
        "relocation_sha256": sha256(evidence_path),
        "target_measured_max_minos": measured,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--profile", choices=("core", "numpy"), required=True)
    parser.add_argument("--arch", choices=("arm64", "x86_64"), required=True)
    args = parser.parse_args()
    result = verify(Path(args.runtime), args.profile, args.arch)
    print(f"native_census_sha256={result['census_sha256']}")
    print(f"relocation_evidence_sha256={result['relocation_sha256']}")
    print(f"target_measured_max_minos={result['target_measured_max_minos']}")
    print("macos_runtime_evidence=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
