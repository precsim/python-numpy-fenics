#!/usr/bin/env python3
"""Create deterministic ZIP or tar.gz archives from a runtime directory."""

from __future__ import annotations

import argparse
import gzip
import os
from pathlib import Path
import stat
import tarfile
import zipfile


ZIP_TIME = (2020, 1, 1, 0, 0, 0)


def iter_paths(root: Path):
    return sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())


def write_zip(root: Path, output: Path) -> None:
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in iter_paths(root):
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                info = zipfile.ZipInfo(rel, ZIP_TIME)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, os.readlink(path).encode("utf-8"))
                continue
            if path.is_dir():
                continue
            info = zipfile.ZipInfo(rel, ZIP_TIME)
            info.create_system = 3
            info.external_attr = (stat.S_IMODE(path.stat().st_mode) & 0xFFFF) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


def write_tar_gz(root: Path, output: Path) -> None:
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for path in iter_paths(root):
                    rel = path.relative_to(root).as_posix()
                    info = archive.gettarinfo(str(path), arcname=rel)
                    info.mtime = 0
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    if info.isfile():
                        with path.open("rb") as stream:
                            archive.addfile(info, stream)
                    else:
                        archive.addfile(info)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--format", choices=("zip", "tar.gz"), required=True)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    output = Path(args.output).resolve()
    if not root.is_dir():
        raise SystemExit(f"runtime root does not exist: {root}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    if args.format == "zip":
        write_zip(root, output)
    else:
        write_tar_gz(root, output)
    print(f"archive={output}")
    print(f"archive_bytes={output.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
