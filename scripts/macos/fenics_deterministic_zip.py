"""Deterministic parallel ZIP construction for the macOS FEniCS overlay."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import struct
from typing import NamedTuple
import zlib


ZIP32_MAX = (1 << 32) - 1
ZIP32_MAX_ENTRIES = (1 << 16) - 1
ZIP_DEFLATED = 8
ZIP_VERSION_NEEDED = 20
ZIP_VERSION_MADE_BY_UNIX = (3 << 8) | 20
ZIP_UTF8_FLAG = 0x0800
ZIP_DOS_TIME = 0
ZIP_DOS_DATE = (1 << 5) | 1  # 1980-01-01

LOCAL_FILE_HEADER = struct.Struct("<IHHHHHIIIHH")
CENTRAL_DIRECTORY_HEADER = struct.Struct("<IHHHHHHIIIHHHHHII")
END_CENTRAL_DIRECTORY = struct.Struct("<IHHHHIIH")

LOCAL_FILE_HEADER_SIGNATURE = 0x04034B50
CENTRAL_DIRECTORY_SIGNATURE = 0x02014B50
END_CENTRAL_DIRECTORY_SIGNATURE = 0x06054B50


class _PreparedMember(NamedTuple):
    filename: bytes
    flag_bits: int
    external_attr: int
    crc32: int
    file_size: int
    compressed: bytes


def _encoded_filename(relative: str) -> tuple[bytes, int]:
    try:
        return relative.encode("ascii"), 0
    except UnicodeEncodeError:
        return relative.encode("utf-8"), ZIP_UTF8_FLAG


def _prepare_member(
    stage_root: Path,
    path: Path,
    compression_level: int,
) -> _PreparedMember:
    relative = path.relative_to(stage_root).as_posix()
    filename, flag_bits = _encoded_filename(relative)
    if len(filename) > ZIP32_MAX_ENTRIES:
        raise RuntimeError(f"ZIP member filename is too long: {relative}")

    mode = stat.S_IMODE(os.lstat(path).st_mode)
    if path.is_symlink():
        data = os.readlink(path).encode("utf-8")
        external_attr = (stat.S_IFLNK | mode) << 16
    else:
        data = path.read_bytes()
        external_attr = (stat.S_IFREG | mode) << 16

    compressor = zlib.compressobj(compression_level, zlib.DEFLATED, -15)
    compressed = compressor.compress(data) + compressor.flush()
    if len(data) > ZIP32_MAX or len(compressed) > ZIP32_MAX:
        raise RuntimeError(f"ZIP member exceeds ZIP32 size limit: {relative}")
    return _PreparedMember(
        filename=filename,
        flag_bits=flag_bits,
        external_attr=external_attr,
        crc32=zlib.crc32(data) & ZIP32_MAX,
        file_size=len(data),
        compressed=compressed,
    )


def deterministic_zip(
    stage_root: Path,
    destination: Path,
    *,
    compression_level: int,
    workers: int,
) -> None:
    """Write a byte-deterministic ZIP while compressing members concurrently."""
    if workers < 1:
        raise ValueError("ZIP compression worker count must be positive")

    paths = [
        path
        for path in sorted(stage_root.rglob("*"))
        if not (path.is_dir() and not path.is_symlink())
    ]
    if len(paths) > ZIP32_MAX_ENTRIES:
        raise RuntimeError("deterministic ZIP exceeds ZIP32 member-count limit")

    central_entries: list[tuple[_PreparedMember, int]] = []
    with destination.open("wb") as archive:
        with ThreadPoolExecutor(max_workers=min(workers, max(1, len(paths)))) as pool:
            prepared = pool.map(
                lambda path: _prepare_member(stage_root, path, compression_level),
                paths,
            )
            for member in prepared:
                offset = archive.tell()
                if offset > ZIP32_MAX:
                    raise RuntimeError("deterministic ZIP exceeds ZIP32 offset limit")
                archive.write(
                    LOCAL_FILE_HEADER.pack(
                        LOCAL_FILE_HEADER_SIGNATURE,
                        ZIP_VERSION_NEEDED,
                        member.flag_bits,
                        ZIP_DEFLATED,
                        ZIP_DOS_TIME,
                        ZIP_DOS_DATE,
                        member.crc32,
                        len(member.compressed),
                        member.file_size,
                        len(member.filename),
                        0,
                    )
                )
                archive.write(member.filename)
                archive.write(member.compressed)
                central_entries.append((member, offset))

        central_offset = archive.tell()
        if central_offset > ZIP32_MAX:
            raise RuntimeError("deterministic ZIP exceeds ZIP32 central-directory offset limit")
        for member, offset in central_entries:
            archive.write(
                CENTRAL_DIRECTORY_HEADER.pack(
                    CENTRAL_DIRECTORY_SIGNATURE,
                    ZIP_VERSION_MADE_BY_UNIX,
                    ZIP_VERSION_NEEDED,
                    member.flag_bits,
                    ZIP_DEFLATED,
                    ZIP_DOS_TIME,
                    ZIP_DOS_DATE,
                    member.crc32,
                    len(member.compressed),
                    member.file_size,
                    len(member.filename),
                    0,
                    0,
                    0,
                    0,
                    member.external_attr,
                    offset,
                )
            )
            archive.write(member.filename)

        central_size = archive.tell() - central_offset
        if central_size > ZIP32_MAX:
            raise RuntimeError("deterministic ZIP exceeds ZIP32 central-directory size limit")
        archive.write(
            END_CENTRAL_DIRECTORY.pack(
                END_CENTRAL_DIRECTORY_SIGNATURE,
                0,
                0,
                len(central_entries),
                len(central_entries),
                central_size,
                central_offset,
                0,
            )
        )
