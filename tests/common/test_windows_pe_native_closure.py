"""Host-only tests for deterministic Windows PE import/provider closure."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest

from scripts.windows.pe_native_closure import (
    PEClosureError,
    census_runtime,
    ensure_closed,
    parse_pe_imports,
    refresh_manifest_inventory,
    verify_reference,
)


def build_pe(imports: tuple[str, ...] = (), delay_imports: tuple[str, ...] = ()) -> bytes:
    pe_offset = 0x80
    optional_size = 0xF0
    section_header = pe_offset + 4 + 20 + optional_size
    raw_offset = 0x200
    section_rva = 0x1000
    raw_size = 0x1000
    data = bytearray(raw_offset + raw_size)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, pe_offset)
    data[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", data, pe_offset + 4, 0x8664, 1, 0, 0, 0, optional_size, 0x2022)
    optional = pe_offset + 24
    struct.pack_into("<H", data, optional, 0x20B)
    struct.pack_into("<Q", data, optional + 24, 0x140000000)
    struct.pack_into("<I", data, optional + 60, raw_offset)
    struct.pack_into("<I", data, optional + 108, 16)
    data[section_header : section_header + 8] = b".rdata\0\0"
    struct.pack_into("<IIII", data, section_header + 8, raw_size, section_rva, raw_size, raw_offset)

    if imports:
        descriptor_offset = raw_offset
        name_offset = raw_offset + 0x100
        for index, name in enumerate(imports):
            encoded = name.encode("ascii") + b"\0"
            name_rva = section_rva + (name_offset - raw_offset)
            struct.pack_into("<IIIII", data, descriptor_offset + index * 20, 0, 0, 0, name_rva, 0)
            data[name_offset : name_offset + len(encoded)] = encoded
            name_offset += len(encoded)
        directory = optional + 112 + 8
        struct.pack_into("<II", data, directory, section_rva, (len(imports) + 1) * 20)

    if delay_imports:
        descriptor_offset = raw_offset + 0x400
        descriptor_rva = section_rva + 0x400
        name_offset = raw_offset + 0x600
        for index, name in enumerate(delay_imports):
            encoded = name.encode("ascii") + b"\0"
            name_rva = section_rva + (name_offset - raw_offset)
            struct.pack_into("<IIIIIIII", data, descriptor_offset + index * 32, 1, name_rva, 0, 0, 0, 0, 0, 0)
            data[name_offset : name_offset + len(encoded)] = encoded
            name_offset += len(encoded)
        directory = optional + 112 + 13 * 8
        struct.pack_into("<II", data, directory, descriptor_rva, (len(delay_imports) + 1) * 32)
    return bytes(data)


def write_manifest(runtime: Path, profile: str) -> None:
    files = []
    for path in sorted(item for item in runtime.rglob("*") if item.is_file() and item.name != "runtime-manifest.json"):
        payload = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(runtime).as_posix(),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "owners": ["python-numpy-fenics"],
            }
        )
    manifest = {
        "format_version": 1,
        "artifact": f"python-runtime-win-x64-cp312-{profile}",
        "version": "0.1.0",
        "platform": "win-x64",
        "python_abi": "cp312",
        "profile": profile,
        "producer": {"repository": "precsim/python-numpy-fenics", "revision": "a" * 40},
        "files": files,
    }
    (runtime / "runtime-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class WindowsPENativeClosureTests(unittest.TestCase):
    def test_parser_reads_import_and_delay_import_tables(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            image = Path(raw) / "probe.exe"
            image.write_bytes(build_pe(("KERNEL32.dll", "python312.dll"), ("USER32.dll",)))
            parsed = parse_pe_imports(image)
            self.assertEqual(parsed.machine, 0x8664)
            self.assertEqual(parsed.imports, ("kernel32.dll", "python312.dll"))
            self.assertEqual(parsed.delay_imports, ("user32.dll",))

    def test_census_is_relocation_deterministic_and_resolves_runtime_and_system(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            system = base / "system32"
            system.mkdir()
            (system / "kernel32.dll").write_bytes(b"system")
            first = base / "runtime one"
            first.mkdir()
            (first / "python.exe").write_bytes(build_pe(("python312.dll", "KERNEL32.dll")))
            (first / "python312.dll").write_bytes(build_pe(("api-ms-win-core-file-l1-1-0.dll",)))
            write_manifest(first, "core")
            second = base / "runtime two"
            shutil.copytree(first, second)

            left = census_runtime(first, "core", system)
            right = census_runtime(second, "core", system)
            ensure_closed(left)
            ensure_closed(right)
            self.assertEqual(left, right)
            self.assertEqual(left["runtime_provider_edge_count"], 1)
            self.assertEqual(left["system_provider_edge_count"], 2)
            self.assertEqual(left["unresolved"], [])
            self.assertEqual(left["ambiguous"], [])

    def test_census_rejects_conflicting_runtime_providers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            runtime = base / "runtime"
            system = base / "system32"
            (runtime / "a").mkdir(parents=True)
            (runtime / "b").mkdir(parents=True)
            system.mkdir()
            (runtime / "python.exe").write_bytes(build_pe(("provider.dll",)))
            (runtime / "a/provider.dll").write_bytes(build_pe() + b"A")
            (runtime / "b/provider.dll").write_bytes(build_pe() + b"B")
            write_manifest(runtime, "core")
            payload = census_runtime(runtime, "core", system)
            with self.assertRaisesRegex(PEClosureError, "ambiguous"):
                ensure_closed(payload)

    def test_census_rejects_unresolved_import(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            runtime = base / "runtime"
            system = base / "system32"
            runtime.mkdir()
            system.mkdir()
            (runtime / "python.exe").write_bytes(build_pe(("missing-provider.dll",)))
            write_manifest(runtime, "core")
            payload = census_runtime(runtime, "core", system)
            with self.assertRaisesRegex(PEClosureError, "unresolved"):
                ensure_closed(payload)

    def test_sealed_census_is_hash_bound_by_final_runtime_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            runtime = base / "runtime"
            system = base / "system32"
            runtime.mkdir()
            system.mkdir()
            (system / "kernel32.dll").write_bytes(b"system")
            (runtime / "python.exe").write_bytes(build_pe(("kernel32.dll",)))
            write_manifest(runtime, "core")
            payload = census_runtime(runtime, "core", system)
            ensure_closed(payload)
            evidence = runtime / "provenance/pe-native-closure-core.json"
            evidence.parent.mkdir()
            evidence.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            refresh_manifest_inventory(runtime)
            recomputed = census_runtime(runtime, "core", system)
            self.assertEqual(payload, recomputed)
            verify_reference(recomputed, evidence)
            manifest = json.loads((runtime / "runtime-manifest.json").read_text(encoding="utf-8"))
            record = next(item for item in manifest["files"] if item["path"] == "provenance/pe-native-closure-core.json")
            self.assertEqual(record["sha256"], hashlib.sha256(evidence.read_bytes()).hexdigest())
            self.assertRegex(payload["pe_tree_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
