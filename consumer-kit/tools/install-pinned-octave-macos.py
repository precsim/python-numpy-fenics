#!/usr/bin/env python3
"""Install verified macOS Octave/OpenSSL archives with a solved dependency closure.

Micromamba cannot mix local .conda paths with named dependency specifications in
a single solve. Dry-run the channel solve without installing a resolver prefix,
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
import time
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


def resolved_explicit_spec(transaction: str) -> str:
    """Read the exact package URLs from a micromamba dry-run LINK transaction."""
    try:
        payload = json.loads(transaction)
    except (ValueError, TypeError) as exc:
        raise SystemExit("micromamba dry-run did not return JSON") from exc
    require(isinstance(payload, dict) and payload.get("success") is True and payload.get("dry_run") is True,
            "micromamba dry-run did not succeed")
    actions = payload.get("actions")
    require(isinstance(actions, dict), "micromamba dry-run omitted actions")
    records = actions.get("LINK")
    require(isinstance(records, list) and bool(records), "micromamba dry-run omitted LINK packages")
    urls: list[str] = []
    for record in records:
        require(isinstance(record, dict), "micromamba dry-run has invalid package entry")
        source = record.get("url")
        require(isinstance(source, str) and urlsplit(source).scheme in {"https", "file"},
                f"dry-run package has no usable archive URL: {record.get('name')}")
        urls.append(source)
    require(len(urls) == len(set(urls)), "dry-run package list contains duplicate archives")
    return "@EXPLICIT\n" + "\n".join(urls) + "\n"


def verify_reused_resolution(source: str, subdir: str) -> None:
    """Fail closed if a transferred closure contains alien or wrong-arch packages."""
    lines = source.splitlines()
    require(lines and lines[0] == "@EXPLICIT", "reused resolution lacks @EXPLICIT header")
    urls = lines[1:]
    require(bool(urls) and len(urls) == len(set(urls)), "reused resolution has no packages or duplicates")
    for url in urls:
        parsed = urlsplit(url)
        require(parsed.scheme == "https" and parsed.netloc == "conda.anaconda.org" and
                parsed.path.startswith((f"/conda-forge/{subdir}/", "/conda-forge/noarch/")) and
                parsed.path.endswith((".conda", ".tar.bz2")) and not parsed.username and not parsed.password,
                f"reused resolution contains an unexpected package source: {url}")


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
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--export-resolution", type=Path,
                       help="write the solved remote package URLs for same-run qualification jobs")
    group.add_argument("--resolution-file", type=Path,
                       help="reuse the matching build job's solved remote package URLs")
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
    with tempfile.TemporaryDirectory(prefix="reference-octave-solve-", dir=prefix.parent) as raw:
        solve_start = time.monotonic()
        if args.resolution_file is not None:
            require(args.resolution_file.is_file(), "same-run resolution artifact is missing")
            remote_spec = args.resolution_file.read_text(encoding="utf-8")
            verify_reused_resolution(remote_spec, args.subdir)
        else:
            result = subprocess.run([
                str(args.micromamba), "create", "--dry-run", "--json", "-y", "-p", str(Path(raw) / "resolver"),
                "-c", "conda-forge", "--strict-channel-priority",
                *(f"{name}={versions[name]}={builds[name]}" for name in archives),
            ], env=env, check=True, capture_output=True, text=True)
            remote_spec = resolved_explicit_spec(result.stdout)
            verify_reused_resolution(remote_spec, args.subdir)
        spec = Path(raw) / "verified-explicit.txt"
        spec.write_text(pinned_explicit_spec(remote_spec, archives), encoding="utf-8")
        if args.export_resolution is not None:
            require(not args.export_resolution.exists(), "resolution output already exists")
            args.export_resolution.parent.mkdir(parents=True, exist_ok=True)
            args.export_resolution.write_text(remote_spec, encoding="utf-8")
        print(f"reference_octave_resolution_seconds={time.monotonic() - solve_start:.3f}", flush=True)
        install_start = time.monotonic()
        subprocess.run([
            str(args.micromamba), "create", "-y", "-p", str(prefix),
            "--file", str(spec),
        ], env=env, check=True)
        print(f"reference_octave_installation_seconds={time.monotonic() - install_start:.3f}", flush=True)
        verify_start = time.monotonic()
        verify_installed(prefix, archives, hashes, versions, builds)
        print(f"reference_octave_verification_seconds={time.monotonic() - verify_start:.3f}", flush=True)
    print("reference_octave_explicit_archive_install=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
