#!/usr/bin/env python3
"""Fetch and verify the pinned Linux x86_64 patchelf used by FEniCS packaging."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen


EXPECTED_TARGET = {"platform": "linux", "architecture": "x86_64"}
EXPECTED_TOOL = "patchelf"
EXPECTED_ARCHIVE_MEMBER = PurePosixPath("bin/patchelf")
PUBLIC_HOSTS = {"github.com"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PatchelfPrepareError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_lock(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PatchelfPrepareError(f"cannot read patchelf lock {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise PatchelfPrepareError("unsupported Linux FEniCS patchelf lock")
    if data.get("target") != EXPECTED_TARGET:
        raise PatchelfPrepareError("patchelf lock target must be linux/x86_64")
    tool = data.get("tool")
    if not isinstance(tool, dict) or tool.get("name") != EXPECTED_TOOL:
        raise PatchelfPrepareError("patchelf lock must describe exactly the patchelf tool")
    version = tool.get("version")
    filename = tool.get("filename")
    url = tool.get("url")
    expected_sha = tool.get("sha256")
    binary_sha = tool.get("binary_sha256")
    archive_member = tool.get("archive_member")
    if not isinstance(version, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", version) is None:
        raise PatchelfPrepareError("invalid patchelf version")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise PatchelfPrepareError("invalid patchelf archive filename")
    if not isinstance(expected_sha, str) or SHA256_RE.fullmatch(expected_sha) is None:
        raise PatchelfPrepareError("invalid patchelf archive SHA-256")
    if not isinstance(binary_sha, str) or SHA256_RE.fullmatch(binary_sha) is None:
        raise PatchelfPrepareError("invalid patchelf binary SHA-256")
    if archive_member != EXPECTED_ARCHIVE_MEMBER.as_posix():
        raise PatchelfPrepareError("patchelf archive member must be bin/patchelf")
    if not isinstance(url, str):
        raise PatchelfPrepareError("invalid patchelf archive URL")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in PUBLIC_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise PatchelfPrepareError(f"patchelf URL is not an allowed public HTTPS origin: {url}")
    return data


def fetch_archive(url: str, destination: Path, expected_sha: str) -> int:
    temporary = destination.with_name(f".{destination.name}.part")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        digest = hashlib.sha256()
        size = 0
        try:
            request = Request(url, headers={"User-Agent": "precsim-linux-fenics-patchelf-fetch/1"})
            with urlopen(request, timeout=90) as response, temporary.open("wb") as stream:
                if urlparse(response.geturl()).scheme != "https":
                    raise PatchelfPrepareError(f"patchelf redirect left HTTPS: {url}")
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    digest.update(block)
                    size += len(block)
            actual_sha = digest.hexdigest()
            if actual_sha != expected_sha:
                raise PatchelfPrepareError(
                    f"patchelf archive SHA-256 mismatch: {actual_sha} != {expected_sha}"
                )
            os.replace(temporary, destination)
            return size
        except Exception as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(attempt)
    assert last_error is not None
    if isinstance(last_error, PatchelfPrepareError):
        raise last_error
    raise PatchelfPrepareError(f"failed to fetch {url}: {last_error}") from last_error


def extract_binary(archive: Path, output_dir: Path) -> Path:
    matches: list[tarfile.TarInfo] = []
    try:
        with tarfile.open(archive, mode="r:gz") as bundle:
            for member in bundle.getmembers():
                normalized = PurePosixPath(member.name.removeprefix("./"))
                if normalized == EXPECTED_ARCHIVE_MEMBER:
                    matches.append(member)
            if len(matches) != 1 or not matches[0].isfile():
                raise PatchelfPrepareError("patchelf archive must contain one regular bin/patchelf member")
            source = bundle.extractfile(matches[0])
            if source is None:
                raise PatchelfPrepareError("cannot read patchelf binary from archive")
            destination = output_dir / EXPECTED_ARCHIVE_MEMBER
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.part")
            with temporary.open("wb") as stream:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
            temporary.chmod(0o755)
            os.replace(temporary, destination)
            return destination
    except (OSError, tarfile.TarError) as exc:
        raise PatchelfPrepareError(f"cannot extract patchelf archive {archive}: {exc}") from exc


def verify_binary(binary: Path, expected_version: str) -> str:
    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PatchelfPrepareError(f"cannot execute pinned patchelf: {exc}") from exc
    observed = completed.stdout.strip()
    expected = f"patchelf {expected_version}"
    if observed != expected:
        raise PatchelfPrepareError(f"unexpected patchelf version output: {observed!r} != {expected!r}")
    return observed


def prepare(lock_path: Path, output_dir: Path, evidence_path: Path) -> tuple[Path, dict]:
    lock_path = lock_path.resolve()
    lock = load_lock(lock_path)
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PatchelfPrepareError(f"patchelf output directory must start empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    tool = lock["tool"]
    archive = output_dir / tool["filename"]
    archive_bytes = fetch_archive(tool["url"], archive, tool["sha256"])
    binary = extract_binary(archive, output_dir)
    actual_binary_sha = sha256_file(binary)
    if actual_binary_sha != tool["binary_sha256"]:
        raise PatchelfPrepareError(
            f"patchelf binary SHA-256 mismatch: {actual_binary_sha} != {tool['binary_sha256']}"
        )
    observed = verify_binary(binary, tool["version"])
    evidence = {
        "format_version": 1,
        "kind": "linux-fenics-packaging-patchelf",
        "lock": {"filename": lock_path.name, "sha256": sha256_file(lock_path)},
        "archive": {
            "filename": tool["filename"],
            "url": tool["url"],
            "sha256": sha256_file(archive),
            "bytes": archive_bytes,
        },
        "tool": {
            "name": EXPECTED_TOOL,
            "version": tool["version"],
            "version_output": observed,
            "binary_path": EXPECTED_ARCHIVE_MEMBER.as_posix(),
            "binary_sha256": actual_binary_sha,
        },
    }
    evidence_path = evidence_path.resolve()
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    archive.unlink()
    return binary, evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    try:
        binary, evidence = prepare(args.lock, args.output_dir, args.evidence)
    except PatchelfPrepareError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"patchelf={binary}")
    print(f"patchelf_version={evidence['tool']['version']}")
    print(f"patchelf_binary_sha256={evidence['tool']['binary_sha256']}")
    print(f"evidence={args.evidence.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
