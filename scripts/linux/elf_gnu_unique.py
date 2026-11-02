#!/usr/bin/env python3
"""Deterministically rewrite ELF64 GNU_UNIQUE dynamic bindings to GLOBAL.

This is a byte-level post-link transform, not a source build.  It is deliberately
fail closed: callers must bind the exact input/output bytes and the expected ELF
ABI/symbol shape before any output is written.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
from typing import Any


ELF_MAGIC = b"\x7fELF"
ELFCLASS64 = 2
ELFDATA2LSB = 1
EV_CURRENT = 1
ELFOSABI_GNU = 3
ET_DYN = 3
EM_X86_64 = 62

SHT_STRTAB = 3
SHT_DYNAMIC = 6
SHT_DYNSYM = 11
SHT_GNU_VERNEED = 0x6FFFFFFE

DT_NULL = 0
DT_NEEDED = 1
DT_SONAME = 14

STB_GLOBAL = 1
STB_GNU_UNIQUE = 10

VERSION_NAMESPACES = ("GLIBC", "GLIBCXX", "CXXABI", "GCC")
RELEASE_SYMBOL_CEILINGS = {
    "GLIBC": (2, 17),
    "GLIBCXX": (3, 4, 19),
    "CXXABI": (1, 3, 7),
    "GCC": (4, 8, 0),
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ElfContractError(ValueError):
    """Raised when an ELF input does not satisfy the pinned transform contract."""


@dataclass(frozen=True)
class Section:
    index: int
    sh_type: int
    offset: int
    size: int
    link: int
    entsize: int


@dataclass(frozen=True)
class DynamicSymbol:
    index: int
    name: str
    binding: int
    symbol_type: int
    st_info_offset: int


@dataclass(frozen=True)
class TransformContract:
    input_sha256: str
    output_sha256: str
    expected_unique_count: int
    expected_dynsym_count: int
    expected_soname: str
    expected_version_max: dict[str, tuple[int, ...] | None]


@dataclass(frozen=True)
class ElfAudit:
    sha256: str
    dynsym_count: int
    unique_symbols: tuple[DynamicSymbol, ...]
    soname: str
    needed: tuple[str, ...]
    required_versions: tuple[str, ...]
    version_max: dict[str, tuple[int, ...] | None]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _slice(data: bytes | bytearray, offset: int, size: int, label: str) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ElfContractError(
            f"{label} lies outside file bounds: offset={offset} size={size} file={len(data)}"
        )
    return bytes(data[offset : offset + size])


def _c_string(table: bytes, offset: int, label: str) -> str:
    if offset < 0 or offset >= len(table):
        raise ElfContractError(f"{label} string offset is outside table: {offset}")
    end = table.find(b"\0", offset)
    if end < 0:
        raise ElfContractError(f"{label} string is not NUL terminated")
    try:
        return table[offset:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ElfContractError(f"{label} string is not valid UTF-8") from exc


def _numeric_version(token: str, namespace: str) -> tuple[int, ...] | None:
    prefix = namespace + "_"
    if not token.startswith(prefix):
        return None
    suffix = token[len(prefix) :]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", suffix):
        return None
    return tuple(int(part) for part in suffix.split("."))


class Elf64Image:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.sections = self._parse_header_and_sections()
        self.dynsym = self._single_section(SHT_DYNSYM, "SHT_DYNSYM")
        self.dynamic = self._single_section(SHT_DYNAMIC, "SHT_DYNAMIC")

    def _parse_header_and_sections(self) -> tuple[Section, ...]:
        if len(self.data) < 64:
            raise ElfContractError("ELF input is shorter than the ELF64 header")
        ident = self.data[:16]
        if ident[:4] != ELF_MAGIC:
            raise ElfContractError("input is not an ELF file")
        if ident[4] != ELFCLASS64:
            raise ElfContractError(f"expected ELF64 class, got {ident[4]}")
        if ident[5] != ELFDATA2LSB:
            raise ElfContractError(f"expected little-endian ELF, got data encoding {ident[5]}")
        if ident[6] != EV_CURRENT:
            raise ElfContractError(f"unexpected ELF ident version: {ident[6]}")
        if ident[7] != ELFOSABI_GNU or ident[8] != 0:
            raise ElfContractError(
                f"expected GNU OSABI 3 ABI-version 0, got osabi={ident[7]} abiversion={ident[8]}"
            )

        e_type, e_machine, e_version = struct.unpack_from("<HHI", self.data, 16)
        e_flags = struct.unpack_from("<I", self.data, 48)[0]
        e_ehsize = struct.unpack_from("<H", self.data, 52)[0]
        e_shoff = struct.unpack_from("<Q", self.data, 40)[0]
        e_shentsize = struct.unpack_from("<H", self.data, 58)[0]
        e_shnum = struct.unpack_from("<H", self.data, 60)[0]
        if e_type != ET_DYN or e_machine != EM_X86_64 or e_version != EV_CURRENT or e_flags != 0:
            raise ElfContractError(
                "expected x86_64 ET_DYN ELFv1 with zero flags, got "
                f"type={e_type} machine={e_machine} version={e_version} flags={e_flags}"
            )
        if e_ehsize != 64 or e_shentsize != 64:
            raise ElfContractError(
                f"unexpected ELF64 header/section sizes: ehsize={e_ehsize} shentsize={e_shentsize}"
            )
        if e_shoff == 0 or e_shnum == 0:
            raise ElfContractError("sectionless or extended-section-numbering ELF is unsupported")
        _slice(self.data, e_shoff, e_shnum * e_shentsize, "section table")

        sections: list[Section] = []
        for index in range(e_shnum):
            offset = e_shoff + index * e_shentsize
            sh_type = struct.unpack_from("<I", self.data, offset + 4)[0]
            sh_offset = struct.unpack_from("<Q", self.data, offset + 24)[0]
            sh_size = struct.unpack_from("<Q", self.data, offset + 32)[0]
            sh_link = struct.unpack_from("<I", self.data, offset + 40)[0]
            sh_entsize = struct.unpack_from("<Q", self.data, offset + 56)[0]
            if sh_size:
                _slice(self.data, sh_offset, sh_size, f"section {index}")
            sections.append(
                Section(index, sh_type, sh_offset, sh_size, sh_link, sh_entsize)
            )
        return tuple(sections)

    def _single_section(self, sh_type: int, label: str) -> Section:
        matches = [section for section in self.sections if section.sh_type == sh_type]
        if len(matches) != 1:
            raise ElfContractError(f"expected exactly one {label} section, found {len(matches)}")
        return matches[0]

    def _linked_string_table(self, section: Section, label: str) -> bytes:
        if section.link >= len(self.sections):
            raise ElfContractError(f"{label} links invalid section index {section.link}")
        strings = self.sections[section.link]
        if strings.sh_type != SHT_STRTAB:
            raise ElfContractError(f"{label} does not link to SHT_STRTAB")
        return _slice(self.data, strings.offset, strings.size, f"{label} string table")

    def dynamic_symbols(self) -> tuple[DynamicSymbol, ...]:
        section = self.dynsym
        if section.entsize != 24 or section.size % section.entsize:
            raise ElfContractError(
                f"unexpected ELF64 dynsym layout: size={section.size} entsize={section.entsize}"
            )
        strings = self._linked_string_table(section, "dynsym")
        result: list[DynamicSymbol] = []
        for index in range(section.size // section.entsize):
            offset = section.offset + index * section.entsize
            st_name = struct.unpack_from("<I", self.data, offset)[0]
            st_info = self.data[offset + 4]
            result.append(
                DynamicSymbol(
                    index=index,
                    name=_c_string(strings, st_name, f"dynsym[{index}]") if st_name else "",
                    binding=st_info >> 4,
                    symbol_type=st_info & 0x0F,
                    st_info_offset=offset + 4,
                )
            )
        return tuple(result)

    def dynamic_identity(self) -> tuple[str, tuple[str, ...]]:
        section = self.dynamic
        if section.entsize != 16 or section.size % section.entsize:
            raise ElfContractError(
                f"unexpected ELF64 dynamic layout: size={section.size} entsize={section.entsize}"
            )
        strings = self._linked_string_table(section, "dynamic")
        sonames: list[str] = []
        needed: list[str] = []
        saw_null = False
        for offset in range(section.offset, section.offset + section.size, section.entsize):
            tag, value = struct.unpack_from("<qQ", self.data, offset)
            if tag == DT_NULL:
                saw_null = True
                break
            if tag == DT_SONAME:
                sonames.append(_c_string(strings, value, "DT_SONAME"))
            elif tag == DT_NEEDED:
                needed.append(_c_string(strings, value, "DT_NEEDED"))
        if not saw_null:
            raise ElfContractError("dynamic section has no DT_NULL terminator")
        if len(sonames) != 1 or not sonames[0]:
            raise ElfContractError(f"expected exactly one nonempty DT_SONAME, found {sonames!r}")
        return sonames[0], tuple(needed)

    def required_versions(self) -> tuple[str, ...]:
        versions: set[str] = set()
        for section in self.sections:
            if section.sh_type != SHT_GNU_VERNEED:
                continue
            strings = self._linked_string_table(section, "GNU verneed")
            cursor = 0
            visited: set[int] = set()
            while cursor < section.size:
                if cursor in visited:
                    raise ElfContractError("GNU verneed chain contains a cycle")
                visited.add(cursor)
                record = section.offset + cursor
                _slice(self.data, record, 16, "GNU verneed record")
                vn_version, vn_cnt, _vn_file, vn_aux, vn_next = struct.unpack_from(
                    "<HHIII", self.data, record
                )
                if vn_version != 1 or vn_cnt == 0 or vn_aux == 0:
                    raise ElfContractError(
                        f"invalid GNU verneed record: version={vn_version} count={vn_cnt} aux={vn_aux}"
                    )
                aux_cursor = cursor + vn_aux
                seen_aux: set[int] = set()
                for aux_index in range(vn_cnt):
                    if aux_cursor in seen_aux:
                        raise ElfContractError("GNU vernaux chain contains a cycle")
                    seen_aux.add(aux_cursor)
                    aux = section.offset + aux_cursor
                    _slice(self.data, aux, 16, "GNU vernaux record")
                    _hash, _flags, _other, vna_name, vna_next = struct.unpack_from(
                        "<IHHII", self.data, aux
                    )
                    versions.add(_c_string(strings, vna_name, "GNU vernaux"))
                    if aux_index + 1 < vn_cnt:
                        if vna_next == 0:
                            raise ElfContractError("GNU vernaux chain ended before vn_cnt entries")
                        aux_cursor += vna_next
                if vn_next == 0:
                    break
                cursor += vn_next
                if cursor >= section.size:
                    raise ElfContractError("GNU verneed next offset escapes section")
        return tuple(sorted(versions))


def audit_bytes(data: bytes) -> ElfAudit:
    image = Elf64Image(data)
    symbols = image.dynamic_symbols()
    unique = tuple(symbol for symbol in symbols if symbol.binding == STB_GNU_UNIQUE)
    soname, needed = image.dynamic_identity()
    required_versions = image.required_versions()
    maxima: dict[str, tuple[int, ...] | None] = {namespace: None for namespace in VERSION_NAMESPACES}
    for token in required_versions:
        if token == "GLIBC_PRIVATE":
            raise ElfContractError("private C++ provider must not require GLIBC_PRIVATE")
        for namespace in VERSION_NAMESPACES:
            if not token.startswith(namespace + "_"):
                continue
            value = _numeric_version(token, namespace)
            if value is None:
                raise ElfContractError(f"unsupported nonnumeric ABI version requirement: {token}")
            current = maxima[namespace]
            if current is None or value > current:
                maxima[namespace] = value
    return ElfAudit(
        sha256=sha256_bytes(data),
        dynsym_count=len(symbols),
        unique_symbols=unique,
        soname=soname,
        needed=needed,
        required_versions=required_versions,
        version_max=maxima,
    )


def _version_text(value: tuple[int, ...] | None) -> str | None:
    return None if value is None else ".".join(str(part) for part in value)


def validate_audit(audit: ElfAudit, contract: TransformContract, *, state: str) -> None:
    if state not in {"source", "transformed"}:
        raise ValueError(f"invalid audit state: {state!r}")
    expected_sha = contract.input_sha256 if state == "source" else contract.output_sha256
    expected_unique = contract.expected_unique_count if state == "source" else 0
    if audit.sha256 != expected_sha:
        raise ElfContractError(
            f"{state} SHA-256 mismatch: {audit.sha256} != {expected_sha}"
        )
    if audit.dynsym_count != contract.expected_dynsym_count:
        raise ElfContractError(
            f"{state} dynsym count mismatch: {audit.dynsym_count} != {contract.expected_dynsym_count}"
        )
    if len(audit.unique_symbols) != expected_unique:
        raise ElfContractError(
            f"{state} GNU_UNIQUE count mismatch: {len(audit.unique_symbols)} != {expected_unique}"
        )
    if audit.soname != contract.expected_soname:
        raise ElfContractError(
            f"{state} SONAME mismatch: {audit.soname!r} != {contract.expected_soname!r}"
        )
    if "libstdc++.so.6" in audit.needed:
        raise ElfContractError("private C++ provider must not depend on libstdc++.so.6")
    for namespace, ceiling in RELEASE_SYMBOL_CEILINGS.items():
        actual = audit.version_max[namespace]
        expected = contract.expected_version_max[namespace]
        if actual != expected:
            raise ElfContractError(
                f"{state} {namespace} maximum mismatch: {_version_text(actual)!r} != {_version_text(expected)!r}"
            )
        if actual is not None and actual > ceiling:
            raise ElfContractError(
                f"{state} exceeds {namespace}_{_version_text(ceiling)}: {_version_text(actual)}"
            )


def transform_bytes(data: bytes, contract: TransformContract) -> tuple[bytes, ElfAudit, ElfAudit]:
    source = audit_bytes(data)
    validate_audit(source, contract, state="source")
    mutable = bytearray(data)
    changed_offsets: set[int] = set()
    for symbol in source.unique_symbols:
        offset = symbol.st_info_offset
        old_info = mutable[offset]
        if old_info >> 4 != STB_GNU_UNIQUE:
            raise ElfContractError(
                f"dynsym[{symbol.index}] {symbol.name!r} lost GNU_UNIQUE binding before transform"
            )
        mutable[offset] = (STB_GLOBAL << 4) | (old_info & 0x0F)
        changed_offsets.add(offset)
    if len(changed_offsets) != contract.expected_unique_count:
        raise ElfContractError(
            f"changed binding-byte count mismatch: {len(changed_offsets)} != {contract.expected_unique_count}"
        )
    transformed_data = bytes(mutable)
    transformed = audit_bytes(transformed_data)
    validate_audit(transformed, contract, state="transformed")
    if source.dynsym_count != transformed.dynsym_count or source.soname != transformed.soname:
        raise ElfContractError("ELF identity changed outside symbol binding transformation")
    return transformed_data, source, transformed


def write_output(path: Path, data: bytes, mode: int) -> None:
    """Atomically install already-validated output bytes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _audit_json(audit: ElfAudit) -> dict[str, Any]:
    return {
        "symbol_binding_scope": "SHT_DYNSYM-runtime-exports",
        "sha256": audit.sha256,
        "dynsym_count": audit.dynsym_count,
        "gnu_unique_count": len(audit.unique_symbols),
        "gnu_unique_symbols": [symbol.name for symbol in audit.unique_symbols],
        "soname": audit.soname,
        "needed": list(audit.needed),
        "required_versions": list(audit.required_versions),
        "version_max": {
            namespace: _version_text(audit.version_max[namespace])
            for namespace in VERSION_NAMESPACES
        },
    }


def _checked_sha(value: str, label: str) -> str:
    value = value.lower()
    if not SHA256_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"{label} must be a 64-digit SHA-256")
    return value


def _parse_expected_versions(values: list[str]) -> dict[str, tuple[int, ...] | None]:
    parsed: dict[str, tuple[int, ...] | None] = {}
    for raw in values:
        if "=" not in raw:
            raise ElfContractError(f"invalid --expected-version-max value: {raw!r}")
        namespace, text = raw.split("=", 1)
        if namespace not in VERSION_NAMESPACES or namespace in parsed:
            raise ElfContractError(f"invalid or duplicate ABI namespace: {namespace!r}")
        if text == "none":
            parsed[namespace] = None
        elif re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", text):
            parsed[namespace] = tuple(int(part) for part in text.split("."))
        else:
            raise ElfContractError(f"invalid ABI maximum for {namespace}: {text!r}")
    missing = sorted(set(VERSION_NAMESPACES) - set(parsed))
    if missing:
        raise ElfContractError(f"missing --expected-version-max values for: {missing}")
    return parsed


def _contract_from_args(args: argparse.Namespace) -> TransformContract:
    if args.expected_unique_count <= 0:
        raise ElfContractError("expected GNU_UNIQUE count must be positive")
    if args.expected_dynsym_count <= args.expected_unique_count:
        raise ElfContractError("expected dynsym count must exceed expected GNU_UNIQUE count")
    return TransformContract(
        input_sha256=args.expected_input_sha256,
        output_sha256=args.expected_output_sha256,
        expected_unique_count=args.expected_unique_count,
        expected_dynsym_count=args.expected_dynsym_count,
        expected_soname=args.expected_soname,
        expected_version_max=_parse_expected_versions(args.expected_version_max),
    )


def _add_contract_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expected-input-sha256", required=True, type=lambda value: _checked_sha(value, "input SHA-256"))
    parser.add_argument("--expected-output-sha256", required=True, type=lambda value: _checked_sha(value, "output SHA-256"))
    parser.add_argument("--expected-unique-count", required=True, type=int)
    parser.add_argument("--expected-dynsym-count", required=True, type=int)
    parser.add_argument("--expected-soname", required=True)
    parser.add_argument(
        "--expected-version-max",
        action="append",
        required=True,
        metavar="NAMESPACE=VERSION|none",
        help="repeat exactly once for GLIBC, GLIBCXX, CXXABI and GCC",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="fail-closed ELF64 GNU_UNIQUE-to-GLOBAL byte transform/auditor"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    transform = subparsers.add_parser("transform")
    transform.add_argument("--input", required=True, type=Path)
    transform.add_argument("--output", required=True, type=Path)
    _add_contract_args(transform)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--input", required=True, type=Path)
    audit.add_argument("--state", choices=("source", "transformed"), required=True)
    _add_contract_args(audit)
    args = parser.parse_args()
    contract = _contract_from_args(args)

    input_path = args.input.resolve()
    if not input_path.is_file():
        raise SystemExit(f"ELF input does not exist: {input_path}")
    source_mode = stat.S_IMODE(input_path.stat().st_mode)
    data = input_path.read_bytes()
    try:
        if args.command == "audit":
            observed = audit_bytes(data)
            validate_audit(observed, contract, state=args.state)
            report = {
                "format_version": 1,
                "operation_kind": "elf-symbol-binding-audit",
                "source_build": False,
                "state": args.state,
                "audit": _audit_json(observed),
            }
        else:
            output_path = args.output.resolve()
            if output_path == input_path:
                raise ElfContractError("transform output must differ from input path")
            transformed_data, before, after = transform_bytes(data, contract)
            write_output(output_path, transformed_data, source_mode)
            report = {
                "format_version": 1,
                "operation_kind": "elf-symbol-binding-transform",
                "source_build": False,
                "changed_binding_bytes": contract.expected_unique_count,
                "before": _audit_json(before),
                "after": _audit_json(after),
            }
    except ElfContractError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
