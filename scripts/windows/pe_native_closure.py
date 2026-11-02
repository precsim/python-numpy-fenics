#!/usr/bin/env python3
"""Build a deterministic PE import/provider closure census for Windows runtimes."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import struct


IMAGE_FILE_MACHINE_AMD64 = 0x8664
IMAGE_DIRECTORY_ENTRY_IMPORT = 1
IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT = 13
WINDOWS_API_PREFIXES = ("api-ms-win-", "ext-ms-win-")


class PEClosureError(RuntimeError):
    pass


@dataclass(frozen=True)
class PEImports:
    machine: int
    imports: tuple[str, ...]
    delay_imports: tuple[str, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unpack(fmt: str, data: bytes, offset: int) -> tuple[int, ...]:
    size = struct.calcsize(fmt)
    if offset < 0 or offset + size > len(data):
        raise PEClosureError(f"truncated PE structure at offset {offset}")
    return struct.unpack_from(fmt, data, offset)


def _cstring(data: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(data):
        raise PEClosureError(f"invalid PE string offset {offset}")
    end = data.find(b"\0", offset)
    if end < 0:
        raise PEClosureError(f"unterminated PE import string at offset {offset}")
    try:
        return data[offset:end].decode("ascii").casefold()
    except UnicodeDecodeError as exc:
        raise PEClosureError(f"non-ASCII PE import name at offset {offset}") from exc


def parse_pe_imports(path: Path) -> PEImports:
    data = path.read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise PEClosureError(f"not a PE image: {path}")
    pe_offset = _unpack("<I", data, 0x3C)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise PEClosureError(f"invalid PE signature: {path}")
    coff = pe_offset + 4
    machine, section_count, _, _, _, optional_size, _ = _unpack("<HHIIIHH", data, coff)
    optional = coff + 20
    if optional + optional_size > len(data):
        raise PEClosureError(f"truncated PE optional header: {path}")
    magic = _unpack("<H", data, optional)[0]
    if magic == 0x20B:
        directory_start = optional + 112
        directory_count = _unpack("<I", data, optional + 108)[0]
        image_base = _unpack("<Q", data, optional + 24)[0]
    elif magic == 0x10B:
        directory_start = optional + 96
        directory_count = _unpack("<I", data, optional + 92)[0]
        image_base = _unpack("<I", data, optional + 28)[0]
    else:
        raise PEClosureError(f"unsupported PE optional-header magic 0x{magic:04x}: {path}")
    size_of_headers = _unpack("<I", data, optional + 60)[0]

    sections: list[tuple[int, int, int, int]] = []
    section_offset = optional + optional_size
    for index in range(section_count):
        current = section_offset + index * 40
        if current + 40 > len(data):
            raise PEClosureError(f"truncated PE section table: {path}")
        virtual_size, virtual_address, raw_size, raw_offset = _unpack("<IIII", data, current + 8)
        sections.append((virtual_address, virtual_size, raw_offset, raw_size))

    def rva_to_offset(rva: int) -> int:
        if rva < size_of_headers:
            if rva >= len(data):
                raise PEClosureError(f"header RVA outside PE image: {path}: 0x{rva:x}")
            return rva
        for virtual_address, virtual_size, raw_offset, raw_size in sections:
            span = max(virtual_size, raw_size)
            if virtual_address <= rva < virtual_address + span:
                offset = raw_offset + (rva - virtual_address)
                if offset >= len(data):
                    break
                return offset
        raise PEClosureError(f"unmapped PE RVA 0x{rva:x}: {path}")

    def directory(index: int) -> tuple[int, int]:
        if index >= directory_count:
            return (0, 0)
        entry = directory_start + index * 8
        if entry + 8 > optional + optional_size:
            raise PEClosureError(f"PE data directory lies outside optional header: {path}")
        return _unpack("<II", data, entry)

    def normal_imports() -> list[str]:
        rva, size = directory(IMAGE_DIRECTORY_ENTRY_IMPORT)
        if not rva:
            return []
        cursor = rva_to_offset(rva)
        limit = max(1, size // 20 + 1) if size else 4096
        names: list[str] = []
        for _ in range(limit):
            descriptor = _unpack("<IIIII", data, cursor)
            if descriptor == (0, 0, 0, 0, 0):
                return names
            names.append(_cstring(data, rva_to_offset(descriptor[3])))
            cursor += 20
        raise PEClosureError(f"unterminated PE import descriptor table: {path}")

    def delay_imports() -> list[str]:
        rva, size = directory(IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT)
        if not rva:
            return []
        cursor = rva_to_offset(rva)
        limit = max(1, size // 32 + 1) if size else 4096
        names: list[str] = []
        for _ in range(limit):
            descriptor = _unpack("<IIIIIIII", data, cursor)
            if descriptor == (0, 0, 0, 0, 0, 0, 0, 0):
                return names
            attributes, name_value = descriptor[0], descriptor[1]
            name_rva = name_value if attributes & 1 else name_value - image_base
            if name_rva <= 0:
                raise PEClosureError(f"invalid delay-import name pointer: {path}")
            names.append(_cstring(data, rva_to_offset(name_rva)))
            cursor += 32
        raise PEClosureError(f"unterminated PE delay-import descriptor table: {path}")

    return PEImports(
        machine=machine,
        imports=tuple(sorted(set(normal_imports()))),
        delay_imports=tuple(sorted(set(delay_imports()))),
    )


def _runtime_manifest(runtime: Path, profile: str) -> tuple[Path, dict[str, dict[str, object]]]:
    manifest_path = runtime / "runtime-manifest.json"
    if not manifest_path.is_file():
        raise PEClosureError(f"runtime manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise PEClosureError("runtime manifest is not an object")
    if (
        manifest.get("format_version") != 1
        or manifest.get("platform") != "win-x64"
        or manifest.get("profile") != profile
        or manifest.get("python_abi") != "cp312"
    ):
        raise PEClosureError("runtime manifest identity is invalid for PE closure")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise PEClosureError("runtime manifest has no file inventory")
    files: dict[str, dict[str, object]] = {}
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise PEClosureError("runtime manifest contains a non-object file entry")
        relative = str(raw.get("path", "")).replace("\\", "/")
        pure = PurePosixPath(relative)
        if not relative or pure.is_absolute() or ".." in pure.parts:
            raise PEClosureError(f"runtime manifest contains unsafe path: {relative!r}")
        key = pure.as_posix()
        if key in files:
            raise PEClosureError(f"runtime manifest contains duplicate path: {key}")
        files[key] = raw
    return manifest_path, files


def census_runtime(runtime: Path, profile: str, system_directory: Path) -> dict[str, object]:
    root = runtime.resolve()
    system_dir = system_directory.resolve()
    if not root.is_dir():
        raise PEClosureError(f"runtime does not exist: {root}")
    if not system_dir.is_dir():
        raise PEClosureError(f"Windows system directory does not exist: {system_dir}")
    _, manifest_files = _runtime_manifest(root, profile)

    pe_files: dict[str, PEImports] = {}
    pe_sha: dict[str, str] = {}
    provider_index: dict[str, list[str]] = {}
    parent_index: dict[tuple[str, str], str] = {}
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda p: p.relative_to(root).as_posix().casefold()):
        with path.open("rb") as stream:
            if stream.read(2) != b"MZ":
                continue
        relative = path.relative_to(root).as_posix()
        parsed = parse_pe_imports(path)
        if parsed.machine != IMAGE_FILE_MACHINE_AMD64:
            raise PEClosureError(f"runtime contains non-x64 PE image: {relative}: 0x{parsed.machine:04x}")
        digest = sha256_file(path)
        manifest_entry = manifest_files.get(relative)
        if not isinstance(manifest_entry, dict) or str(manifest_entry.get("sha256", "")).lower() != digest:
            raise PEClosureError(f"PE image is not byte-bound by runtime manifest: {relative}")
        pe_files[relative] = parsed
        pe_sha[relative] = digest
        basename = path.name.casefold()
        provider_index.setdefault(basename, []).append(relative)
        parent_index[(PurePosixPath(relative).parent.as_posix().casefold(), basename)] = relative

    if not pe_files:
        raise PEClosureError("runtime contains no PE images")
    for paths in provider_index.values():
        paths.sort(key=str.casefold)

    system_names = {path.name.casefold() for path in system_dir.iterdir() if path.is_file()}
    unresolved: list[dict[str, object]] = []
    ambiguous: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    runtime_edges = 0
    system_edges = 0

    for relative in sorted(pe_files, key=str.casefold):
        parsed = pe_files[relative]
        importer_parent = PurePosixPath(relative).parent.as_posix().casefold()
        imports: list[dict[str, object]] = []
        edges = [(name, False) for name in parsed.imports] + [(name, True) for name in parsed.delay_imports]
        for name, delayed in sorted(edges, key=lambda item: (item[0], item[1])):
            record: dict[str, object] = {"name": name, "delay": delayed}
            local = parent_index.get((importer_parent, name))
            root_provider = parent_index.get((".", name)) or parent_index.get(("", name))
            candidates = provider_index.get(name, [])
            if local is not None:
                record.update({"provider_kind": "runtime", "provider_path": local, "provider_sha256": pe_sha[local]})
                runtime_edges += 1
            elif root_provider is not None:
                record.update({"provider_kind": "runtime", "provider_path": root_provider, "provider_sha256": pe_sha[root_provider]})
                runtime_edges += 1
            elif len(candidates) == 1:
                provider = candidates[0]
                record.update({"provider_kind": "runtime", "provider_path": provider, "provider_sha256": pe_sha[provider]})
                runtime_edges += 1
            elif len(candidates) > 1:
                identities = {pe_sha[item] for item in candidates}
                if len(identities) == 1:
                    record.update(
                        {
                            "provider_kind": "runtime-equivalent",
                            "provider_paths": candidates,
                            "provider_sha256": next(iter(identities)),
                        }
                    )
                    runtime_edges += 1
                else:
                    failure = {"importer": relative, "name": name, "delay": delayed, "candidates": candidates}
                    ambiguous.append(failure)
                    record.update({"provider_kind": "ambiguous", "provider_paths": candidates})
            elif name.startswith(WINDOWS_API_PREFIXES):
                record["provider_kind"] = "windows-api-set"
                system_edges += 1
            elif name in system_names:
                record["provider_kind"] = "windows-system32"
                system_edges += 1
            else:
                failure = {"importer": relative, "name": name, "delay": delayed}
                unresolved.append(failure)
                record["provider_kind"] = "unresolved"
            imports.append(record)
        records.append(
            {
                "path": relative,
                "sha256": pe_sha[relative],
                "machine": "amd64",
                "imports": imports,
            }
        )

    pe_tree = hashlib.sha256()
    for relative in sorted(pe_sha, key=str.casefold):
        pe_tree.update(relative.encode("utf-8") + b"\0")
        pe_tree.update(pe_sha[relative].encode("ascii") + b"\n")

    return {
        "format_version": 1,
        "kind": "windows-pe-native-closure",
        "platform": "win-x64",
        "profile": profile,
        "pe_tree_sha256": pe_tree.hexdigest(),
        "pe_file_count": len(records),
        "import_edge_count": runtime_edges + system_edges + len(unresolved) + len(ambiguous),
        "runtime_provider_edge_count": runtime_edges,
        "system_provider_edge_count": system_edges,
        "unresolved": unresolved,
        "ambiguous": ambiguous,
        "files": records,
    }


def refresh_manifest_inventory(runtime: Path) -> None:
    root = runtime.resolve()
    manifest_path = root / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise PEClosureError("runtime manifest is not an object")
    entries: list[dict[str, object]] = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file() and item != manifest_path),
        key=lambda item: item.relative_to(root).as_posix().casefold(),
    ):
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "owners": ["python-numpy-fenics"],
            }
        )
    manifest["files"] = entries
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def ensure_closed(payload: dict[str, object]) -> None:
    unresolved = payload.get("unresolved")
    ambiguous = payload.get("ambiguous")
    if unresolved or ambiguous:
        raise PEClosureError(
            f"PE native closure is incomplete: unresolved={unresolved!r} ambiguous={ambiguous!r}"
        )


def verify_reference(payload: dict[str, object], reference: Path) -> None:
    if not reference.is_file():
        raise PEClosureError(f"packaged PE closure reference is missing: {reference}")
    expected = json.loads(reference.read_text(encoding="utf-8-sig"))
    if expected != payload:
        raise PEClosureError(f"packaged PE closure reference differs from recomputed census: {reference}")


def default_system_directory() -> Path:
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not system_root:
        raise PEClosureError("SystemRoot/WINDIR is not set")
    return Path(system_root) / "System32"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--profile", choices=("core", "numpy"), required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--system-directory")
    parser.add_argument("--seal-manifest", action="store_true")
    parser.add_argument("--reference")
    args = parser.parse_args()

    system_dir = Path(args.system_directory) if args.system_directory else default_system_directory()
    payload = census_runtime(Path(args.runtime), args.profile, system_dir)
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ensure_closed(payload)
    if args.reference:
        verify_reference(payload, Path(args.reference).resolve())
    if args.seal_manifest:
        runtime = Path(args.runtime).resolve()
        if runtime != output.parent and runtime not in output.parents:
            raise PEClosureError("sealed PE closure evidence must be written inside the runtime")
        refresh_manifest_inventory(runtime)
    print(f"windows_pe_file_count={payload['pe_file_count']}")
    print(f"windows_pe_import_edge_count={payload['import_edge_count']}")
    print("windows_pe_native_closure=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
