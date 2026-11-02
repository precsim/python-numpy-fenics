"""Tests for the fail-closed Linux GNU_UNIQUE binding transform."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

from scripts.linux.elf_gnu_unique import (
    ElfContractError,
    STB_GLOBAL,
    STB_GNU_UNIQUE,
    TransformContract,
    audit_bytes,
    main,
    transform_bytes,
    validate_audit,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _build_elf(
    *,
    unique_count: int,
    glibc: str = "2.17",
    gcc: str = "4.3.0",
    needed: tuple[str, ...] = ("libc.so.6",),
    machine: int = 62,
) -> bytes:
    names = [f"unique_{index}" for index in range(unique_count)]
    dynstr_values = ["", *names, "libfenics_cxxrt.so.1", *needed, f"GLIBC_{glibc}", f"GCC_{gcc}"]
    dynstr = bytearray()
    offsets: dict[str, int] = {}
    for value in dynstr_values:
        if value in offsets:
            continue
        offsets[value] = len(dynstr)
        dynstr.extend(value.encode() + b"\0")

    dynsym = bytearray(24)  # required null symbol
    for name in names:
        dynsym.extend(struct.pack("<IBBHQQ", offsets[name], STB_GNU_UNIQUE << 4, 0, 1, 0, 0))
    dynsym.extend(struct.pack("<IBBHQQ", offsets["libfenics_cxxrt.so.1"], STB_GLOBAL << 4, 0, 1, 0, 0))

    dynamic = bytearray()
    dynamic.extend(struct.pack("<qQ", 14, offsets["libfenics_cxxrt.so.1"]))
    for name in needed:
        dynamic.extend(struct.pack("<qQ", 1, offsets[name]))
    dynamic.extend(struct.pack("<qQ", 0, 0))

    # One Verneed record with two Vernaux records, enough to exercise exact
    # GLIBC/GCC maxima without relying on external readelf/objdump tools.
    verneed = bytearray(48)
    struct.pack_into("<HHIII", verneed, 0, 1, 2, offsets[needed[0]], 16, 0)
    struct.pack_into("<IHHII", verneed, 16, 0, 0, 2, offsets[f"GLIBC_{glibc}"], 16)
    struct.pack_into("<IHHII", verneed, 32, 0, 0, 3, offsets[f"GCC_{gcc}"], 0)

    section_payloads = [b"", bytes(dynstr), bytes(dynsym), bytes(dynamic), bytes(verneed)]
    section_types = [0, 3, 11, 6, 0x6FFFFFFE]
    section_links = [0, 0, 1, 1, 1]
    section_entsizes = [0, 0, 24, 16, 0]
    offsets_in_file = [0] * len(section_payloads)
    cursor = 64
    blob = bytearray(b"\0" * 64)
    for index in range(1, len(section_payloads)):
        cursor = _align(cursor, 8)
        if len(blob) < cursor:
            blob.extend(b"\0" * (cursor - len(blob)))
        offsets_in_file[index] = cursor
        blob.extend(section_payloads[index])
        cursor += len(section_payloads[index])
    shoff = _align(cursor, 8)
    if len(blob) < shoff:
        blob.extend(b"\0" * (shoff - len(blob)))
    for index, payload in enumerate(section_payloads):
        blob.extend(
            struct.pack(
                "<IIQQQQIIQQ",
                0,
                section_types[index],
                0,
                0,
                offsets_in_file[index],
                len(payload),
                section_links[index],
                0,
                8 if index else 0,
                section_entsizes[index],
            )
        )

    ident = bytearray(16)
    ident[:4] = b"\x7fELF"
    ident[4] = 2
    ident[5] = 1
    ident[6] = 1
    ident[7] = 3
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        bytes(ident),
        3,
        machine,
        1,
        0,
        0,
        shoff,
        0,
        64,
        0,
        0,
        64,
        len(section_payloads),
        0,
    )
    blob[:64] = header
    return bytes(blob)


def _contract(source: bytes, transformed: bytes, unique_count: int) -> TransformContract:
    return TransformContract(
        input_sha256=_sha(source),
        output_sha256=_sha(transformed),
        expected_unique_count=unique_count,
        expected_dynsym_count=unique_count + 2,
        expected_soname="libfenics_cxxrt.so.1",
        expected_version_max={
            "GLIBC": (2, 17),
            "GLIBCXX": None,
            "CXXABI": None,
            "GCC": (4, 3, 0),
        },
    )


def _expected_transformed(source: bytes) -> bytes:
    audit = audit_bytes(source)
    mutable = bytearray(source)
    for symbol in audit.unique_symbols:
        mutable[symbol.st_info_offset] = (STB_GLOBAL << 4) | symbol.symbol_type
    return bytes(mutable)


class LinuxElfGnuUniqueTests(unittest.TestCase):
    def test_same_transform_supports_both_proven_symbol_counts(self) -> None:
        for unique_count in (139, 146):
            with self.subTest(unique_count=unique_count):
                source = _build_elf(unique_count=unique_count)
                expected = _expected_transformed(source)
                contract = _contract(source, expected, unique_count)
                actual, before, after = transform_bytes(source, contract)
                self.assertEqual(actual, expected)
                self.assertEqual(len(before.unique_symbols), unique_count)
                self.assertEqual(len(after.unique_symbols), 0)
                validate_audit(after, contract, state="transformed")

    def test_transform_changes_only_unique_binding_bytes(self) -> None:
        source = _build_elf(unique_count=3)
        expected = _expected_transformed(source)
        contract = _contract(source, expected, 3)
        actual, before, _after = transform_bytes(source, contract)
        changed = {index for index, pair in enumerate(zip(source, actual)) if pair[0] != pair[1]}
        self.assertEqual(changed, {symbol.st_info_offset for symbol in before.unique_symbols})

    def test_wrong_source_hash_fails_closed(self) -> None:
        source = _build_elf(unique_count=2)
        expected = _expected_transformed(source)
        contract = _contract(source, expected, 2)
        bad = TransformContract("0" * 64, contract.output_sha256, 2, 4, contract.expected_soname, contract.expected_version_max)
        with self.assertRaisesRegex(ElfContractError, "source SHA-256 mismatch"):
            transform_bytes(source, bad)

    def test_wrong_expected_unique_count_fails_closed(self) -> None:
        source = _build_elf(unique_count=2)
        expected = _expected_transformed(source)
        base = _contract(source, expected, 2)
        contract = TransformContract(
            input_sha256=base.input_sha256,
            output_sha256=base.output_sha256,
            expected_unique_count=3,
            expected_dynsym_count=base.expected_dynsym_count,
            expected_soname=base.expected_soname,
            expected_version_max=base.expected_version_max,
        )
        with self.assertRaisesRegex(ElfContractError, "GNU_UNIQUE count mismatch"):
            transform_bytes(source, contract)

    def test_wrong_expected_output_hash_fails_closed(self) -> None:
        source = _build_elf(unique_count=2)
        expected = _expected_transformed(source)
        base = _contract(source, expected, 2)
        contract = TransformContract(
            input_sha256=base.input_sha256,
            output_sha256="f" * 64,
            expected_unique_count=base.expected_unique_count,
            expected_dynsym_count=base.expected_dynsym_count,
            expected_soname=base.expected_soname,
            expected_version_max=base.expected_version_max,
        )
        with self.assertRaisesRegex(ElfContractError, "transformed SHA-256 mismatch"):
            transform_bytes(source, contract)

    def test_cli_validation_failure_does_not_create_output(self) -> None:
        source = _build_elf(unique_count=2)
        expected = _expected_transformed(source)
        contract = _contract(source, expected, 2)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source_path = root / "source.so"
            output_path = root / "output.so"
            source_path.write_bytes(source)
            argv = [
                "elf_gnu_unique.py",
                "transform",
                "--input",
                str(source_path),
                "--output",
                str(output_path),
                "--expected-input-sha256",
                contract.input_sha256,
                "--expected-output-sha256",
                "0" * 64,
                "--expected-unique-count",
                "2",
                "--expected-dynsym-count",
                "4",
                "--expected-soname",
                contract.expected_soname,
                "--expected-version-max",
                "GLIBC=2.17",
                "--expected-version-max",
                "GLIBCXX=none",
                "--expected-version-max",
                "CXXABI=none",
                "--expected-version-max",
                "GCC=4.3.0",
            ]
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaisesRegex(SystemExit, "transformed SHA-256 mismatch"):
                    main()
            self.assertFalse(output_path.exists())

    def test_already_transformed_input_is_rejected_as_source(self) -> None:
        source = _build_elf(unique_count=2)
        transformed = _expected_transformed(source)
        contract = TransformContract(
            input_sha256=_sha(transformed),
            output_sha256=_sha(transformed),
            expected_unique_count=2,
            expected_dynsym_count=4,
            expected_soname="libfenics_cxxrt.so.1",
            expected_version_max=_contract(source, transformed, 2).expected_version_max,
        )
        with self.assertRaisesRegex(ElfContractError, "GNU_UNIQUE count mismatch"):
            transform_bytes(transformed, contract)

    def test_wrong_machine_is_rejected(self) -> None:
        with self.assertRaisesRegex(ElfContractError, "expected x86_64 ET_DYN"):
            audit_bytes(_build_elf(unique_count=1, machine=3))

    def test_abi_ceiling_violation_is_rejected(self) -> None:
        source = _build_elf(unique_count=1, glibc="2.18")
        expected = _expected_transformed(source)
        contract = TransformContract(
            input_sha256=_sha(source),
            output_sha256=_sha(expected),
            expected_unique_count=1,
            expected_dynsym_count=3,
            expected_soname="libfenics_cxxrt.so.1",
            expected_version_max={"GLIBC": (2, 18), "GLIBCXX": None, "CXXABI": None, "GCC": (4, 3, 0)},
        )
        with self.assertRaisesRegex(ElfContractError, "exceeds GLIBC_2.17"):
            transform_bytes(source, contract)

    def test_libstdcxx_dependency_is_rejected(self) -> None:
        source = _build_elf(unique_count=1, needed=("libc.so.6", "libstdc++.so.6"))
        expected = _expected_transformed(source)
        contract = _contract(source, expected, 1)
        with self.assertRaisesRegex(ElfContractError, r"must not depend on libstdc\+\+\.so\.6"):
            transform_bytes(source, contract)

    def test_proven_provider_evidence_is_pinned_as_transform_not_source_build(self) -> None:
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "linux-gnu-unique-provider-evidence.json"
        evidence = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(evidence["format_version"], 1)
        self.assertEqual(evidence["operation_kind"], "elf-symbol-binding-transform-evidence")
        self.assertIs(evidence["source_build"], False)
        providers = {item["id"]: item for item in evidence["providers"]}
        self.assertEqual(providers["gcc14-linker-prototype"]["expected_unique_count"], 139)
        self.assertEqual(
            providers["gcc14-linker-prototype"]["transformed_sha256"],
            "add12375e450fe054e0e46f05c91dcf8b5b3836ade8265a83f0deae9972b5d66",
        )
        locked = providers["locked-gcc16-linker-proof"]
        self.assertEqual(locked["expected_unique_count"], 146)
        self.assertEqual(locked["expected_dynsym_count"], 7261)
        self.assertEqual(locked["symbol_binding_scope"], "SHT_DYNSYM-runtime-exports")
        self.assertEqual(locked["non_runtime_symtab_unique_count"], 146)
        self.assertEqual(
            locked["transform_input_sha256"],
            "c7a9a4e04b175b3cd60c9a0a30e9168f9032822c6b4a1cfe737a89d190f3cd58",
        )
        self.assertEqual(
            locked["transformed_sha256"],
            "39473bb7046bd62af7fbafda82c18d6f755de28a9e32ec65734985d72ff71cb4",
        )
        self.assertEqual(locked["abi"]["version_max"], {"GLIBC": "2.17", "GLIBCXX": None, "CXXABI": None, "GCC": "4.3.0"})
        self.assertNotIn("libstdc++.so.6", locked["abi"]["needed"])
        self.assertIs(locked["source_build"], False)
        self.assertIs(locked["qualification"]["deterministic_repeat_link_and_transform"], True)
        self.assertIs(locked["limitations"]["runtime_lock_cxx_driver_present"], False)
        self.assertIs(
            locked["limitations"]["basix_dolfinx_cxx20_source_rebuild_toolchain_proven"],
            False,
        )


if __name__ == "__main__":
    unittest.main()
