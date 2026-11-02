#!/usr/bin/env python3
"""Fetch the pinned public source inputs for the Linux FEniCS rebuild prototype."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen


EXPECTED_SOURCES = ("basix", "dolfinx", "fmt", "spdlog", "pugixml", "libxml2")
PUBLIC_SOURCE_HOSTS = {"github.com", "gitlab.gnome.org"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceFetchError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceFetchError(f"cannot read source manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise SourceFetchError("unsupported FEniCS rebuild source manifest")
    sources = data.get("sources")
    if not isinstance(sources, dict) or tuple(sources) != EXPECTED_SOURCES:
        raise SourceFetchError(f"source manifest keys must be exactly {EXPECTED_SOURCES!r}")
    for name in EXPECTED_SOURCES:
        record = sources[name]
        if not isinstance(record, dict):
            raise SourceFetchError(f"invalid source record: {name}")
        filename = record.get("filename")
        expected_sha = record.get("sha256")
        url = record.get("url")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise SourceFetchError(f"invalid source filename: {name}")
        if not isinstance(expected_sha, str) or SHA256_RE.fullmatch(expected_sha) is None:
            raise SourceFetchError(f"invalid source SHA-256: {name}")
        if not isinstance(url, str):
            raise SourceFetchError(f"invalid source URL: {name}")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in PUBLIC_SOURCE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise SourceFetchError(f"source URL is not an allowed public HTTPS origin: {name}: {url}")
    return data


def fetch_one(url: str, destination: Path, expected_sha: str) -> dict:
    temporary = destination.with_name(f".{destination.name}.part")
    last_error: Exception | None = None
    for attempt in range(1, 4):
        digest = hashlib.sha256()
        size = 0
        try:
            request = Request(url, headers={"User-Agent": "precsim-linux-fenics-rebuild-input-fetch/1"})
            with urlopen(request, timeout=90) as response, temporary.open("wb") as stream:
                resolved = response.geturl()
                if urlparse(resolved).scheme != "https":
                    raise SourceFetchError(f"source redirect left HTTPS: {url} -> {resolved}")
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    digest.update(block)
                    size += len(block)
            actual_sha = digest.hexdigest()
            if actual_sha != expected_sha:
                raise SourceFetchError(
                    f"source SHA-256 mismatch for {destination.name}: {actual_sha} != {expected_sha}"
                )
            os.replace(temporary, destination)
            return {
                "url": url,
                "resolved_url": resolved,
                "filename": destination.name,
                "sha256": actual_sha,
                "size": size,
            }
        except Exception as exc:  # network failures are retried, integrity failures remain fatal after retry budget
            last_error = exc
            temporary.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(attempt)
    assert last_error is not None
    if isinstance(last_error, SourceFetchError):
        raise last_error
    raise SourceFetchError(f"failed to fetch {url}: {last_error}") from last_error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()

    try:
        manifest_path = args.manifest.resolve()
        manifest = load_manifest(manifest_path)
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        unexpected = [path for path in output_dir.iterdir() if path.name != args.evidence.name]
        if unexpected:
            raise SourceFetchError(f"source output directory must start empty: {output_dir}")
        records = []
        for name in EXPECTED_SOURCES:
            source = manifest["sources"][name]
            destination = output_dir / source["filename"]
            record = fetch_one(source["url"], destination, source["sha256"])
            records.append({"component": name, **record})
        evidence = {
            "format_version": 1,
            "kind": "linux-fenics-native-rebuild-public-source-inputs",
            "private_sources_used": False,
            "manifest": {"path": str(manifest_path), "sha256": sha256_file(manifest_path)},
            "sources": records,
        }
        evidence_path = args.evidence.resolve()
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except SourceFetchError as exc:
        raise SystemExit(str(exc)) from exc

    print(f"source_count={len(records)}")
    print("private_sources_used=false")
    print(f"evidence={evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
