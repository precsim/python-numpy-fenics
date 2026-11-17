#!/usr/bin/env python3
"""Install verified macOS Octave/OpenSSL archives with a solved dependency closure.

Micromamba cannot mix local .conda paths with named dependency specifications in
a single solve. Resolve the ordinary channel environment in a disposable prefix,
then replay its explicit package list, replacing Octave and OpenSSL with the
verified local archives. Only the final prefix is used to build/test the MEX.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def pinned_explicit_spec(source: str, archives: dict[str, Path]) -> str:
    """Replace exactly one resolved package of each pinned name with its file URL."""
    lines = source.splitlines()
    require("@EXPLICIT" in lines, "micromamba did not export an explicit package list")
    start = lines.index("@EXPLICIT")
    records = [line.strip() for line in lines[start + 1 :] if line.strip() and not line.startswith("#")]
    require(bool(records), "micromamba explicit package list is empty")
    seen: set[str] = set()
    output = ["@EXPLICIT"]
    for record in records:
        parsed = urlsplit(record)
        name = unquote(parsed.path.rsplit("/", 1)[-1])
        matched = next((key for key in archives if name.startswith(key + "-")), None)
        if matched is None:
            require(parsed.scheme in {"https", "file"}, f"unexpected dependency package URL: {record}")
            output.append(record)
            continue
        require(matched not in seen, f"duplicate {matched} in resolved environment")
        require(name == archives[matched].name, f"unexpected resolved {matched} package: {name}")
        seen.add(matched)
        output.append(archives[matched].as_uri())
    require(seen == set(archives), f"resolved environment omitted pinned packages: {sorted(set(archives) - seen)}")
    return "\n".join(output) + "\n"


def resolved_explicit_spec(prefix: Path) -> str:
    """Capture the solved closure from installed conda-meta records, not CLI output.

    Micromamba's `list --explicit` output is not an explicit spec on all
    supported versions; conda-meta URL records are the installed package inputs.
    """
    metadata = prefix / "conda-meta"
    require(metadata.is_dir(), "resolver conda metadata is missing")
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(metadata.glob("*.json"))]
    require(bool(records), "resolver environment contains no package metadata")
    urls: list[str] = []
    for record in records:
        source = record.get("url")
        require(isinstance(source, str) and urlsplit(source).scheme in {"https", "file"},
                f"resolver package has no usable archive URL: {record.get('name')}")
        urls.append(source)
    require(len(urls) == len(set(urls)), "resolver package list contains duplicate archives")
    return "@EXPLICIT\n" + "\n".join(urls) + "\n"


def verify_installed(prefix: Path, archives: dict[str, Path], sha256s: dict[str, str],
                     versions: dict[str, str], builds: dict[str, str]) -> None:
    metadata = prefix / "conda-meta"
    require(metadata.is_dir(), "installed conda metadata is missing")
    records = [json.loads(path.read_text(encoding="utf-8")) for path in metadata.glob("*.json")]
    for name, archive in archives.items():
        matched = [record for record in records if record.get("name") == name]
        require(len(matched) == 1, f"expected one installed {name} package")
        record = matched[0]
        require(record.get("version") == versions[name] and
                record.get("build_string", record.get("build")) == builds[name],
                f"installed {name} version/build differs from pin")
        source = urlsplit(str(record.get("url", "")))
        require(source.scheme == "file" and
                Path(url2pathname(source.path)).resolve() == archive,
                f"installed {name} did not originate from the verified local archive")
        recorded_sha = record.get("sha256")
        if recorded_sha is not None:
            require(recorded_sha == sha256s[name], f"installed {name} archive SHA-256 differs from pin")
        print(f"reference_octave_pinned_{name}_archive=pass")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--micromamba", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--subdir", choices=("osx-arm64", "osx-64"), required=True)
    for name in ("octave", "openssl"):
        parser.add_argument(f"--{name}-archive", type=Path, required=True)
        parser.add_argument(f"--{name}-sha256", required=True)
        parser.add_argument(f"--{name}-version", required=True)
        parser.add_argument(f"--{name}-build", required=True)
    args = parser.parse_args()
    prefix = args.prefix.resolve()
    archives = {name: getattr(args, f"{name}_archive").resolve() for name in ("octave", "openssl")}
    hashes = {name: getattr(args, f"{name}_sha256") for name in archives}
    versions = {name: getattr(args, f"{name}_version") for name in archives}
    builds = {name: getattr(args, f"{name}_build") for name in archives}
    for name, archive in archives.items():
        require(archive.is_file(), f"missing pinned {name} archive")
        require(digest(archive) == hashes[name], f"pinned {name} archive SHA-256 mismatch")
        require(archive.name == f"{name}-{versions[name]}-{builds[name]}.conda",
                f"unexpected pinned {name} archive filename")
    require(not prefix.exists(), f"target prefix already exists: {prefix}")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CONDA_SUBDIR=args.subdir)
    with tempfile.TemporaryDirectory(prefix="reference-octave-resolve-", dir=prefix.parent) as raw:
        resolver = Path(raw) / "resolver"
        subprocess.run([
            str(args.micromamba), "create", "-y", "-p", str(resolver),
            "-c", "conda-forge", "--strict-channel-priority",
            *(f"{name}={versions[name]}={builds[name]}" for name in archives),
        ], env=env, check=True)
        spec = Path(raw) / "verified-explicit.txt"
        spec.write_text(pinned_explicit_spec(resolved_explicit_spec(resolver), archives), encoding="utf-8")
        subprocess.run([
            str(args.micromamba), "create", "-y", "-p", str(prefix),
            "--file", str(spec),
        ], env=env, check=True)
        verify_installed(prefix, archives, hashes, versions, builds)
    print("reference_octave_explicit_archive_install=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
