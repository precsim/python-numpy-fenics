from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.linux.prepare_fenics_libxml2_build_deps import (
    BuildDepsError,
    DEVELOPMENT_PACKAGES,
    IDENTITY_FIELDS,
    RUNTIME_PACKAGES,
    prepare,
    render_explicit,
    validate_lock,
)


ROOT = Path(__file__).resolve().parents[2]
BUILD_DEPS_LOCK = ROOT / "locks" / "linux" / "linux-x86_64-fenics-libxml2-build-deps.json"
FENICS_LOCK = ROOT / "locks" / "linux" / "linux-x86_64-fenics-package-lock.json"


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def md5(payload: bytes) -> str:
    return hashlib.md5(payload).hexdigest()  # noqa: S324 - package identity fixture only


def identity(name: str, version: str, build: str, build_number: int, archive: bytes) -> dict:
    filename = f"{name}-{version}-{build}.conda"
    return {
        "name": name,
        "version": version,
        "build": build,
        "build_number": build_number,
        "subdir": "linux-64",
        "channel": "conda-forge",
        "url": f"https://conda.anaconda.org/conda-forge/linux-64/{filename}",
        "sha256": digest(archive),
        "md5": md5(archive),
    }


class LinuxFenicsLibxml2BuildDepsTests(unittest.TestCase):
    def test_checked_in_lock_binds_exact_runtime_packages_and_headers(self) -> None:
        lock, development, runtime = validate_lock(BUILD_DEPS_LOCK, FENICS_LOCK)
        self.assertEqual(tuple(development), DEVELOPMENT_PACKAGES)
        self.assertEqual(tuple(runtime), RUNTIME_PACKAGES)
        self.assertEqual(runtime["libzlib"]["sha256"], "eb8a0db0aa570124f7d2a93d7c7f596e3390df5e047818d873baad32985fc736")
        self.assertEqual(runtime["liblzma"]["sha256"], "9787df8c22a59c9a70d3e5a10db9ad663485e75e9ccc3f09bd092cb7b95e0dab")
        self.assertEqual(len(lock["output"]["headers"]), 17)
        self.assertEqual(lock["output"]["libraries"]["zlib"]["source_path"], "lib/libz.so.1.3.2")
        self.assertEqual(lock["output"]["libraries"]["lzma"]["source_path"], "lib/liblzma.so.5.8.3")
        self.assertFalse(lock["policy"]["dependency_solver_used"])
        self.assertFalse(lock["policy"]["index_access"])

    def test_rendered_development_transaction_is_exact_url_plus_sha(self) -> None:
        _, development, _ = validate_lock(BUILD_DEPS_LOCK, FENICS_LOCK)
        lines = [line for line in render_explicit(development).splitlines() if line and not line.startswith("#")]
        self.assertEqual(lines[0], "@EXPLICIT")
        self.assertEqual(len(lines), 3)
        self.assertEqual(
            lines[1:],
            [f"{development[name]['url']}#sha256:{development[name]['sha256']}" for name in DEVELOPMENT_PACKAGES],
        )

    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
        zlib_header = b"zlib-header\n"
        zconf_header = b"zconf-header\n"
        lzma_header = b"lzma-header\n"
        lzma_base = b"lzma-base\n"
        zlib_library = b"ELF-zlib-fixture\n"
        lzma_library = b"ELF-lzma-fixture\n"
        zlib_archive = b"zlib-devel-archive-fixture"
        lzdev_archive = b"lzma-devel-archive-fixture"
        libz_archive = b"libz-runtime-archive-fixture"
        liblzma_archive = b"liblzma-runtime-archive-fixture"

        dev_zlib = identity("zlib", "1.3.2", "fixture_3", 3, zlib_archive)
        dev_zlib["files"] = {
            "include/zconf.h": {"sha256": digest(zconf_header), "size": len(zconf_header)},
            "include/zlib.h": {"sha256": digest(zlib_header), "size": len(zlib_header)},
        }
        dev_lzma = identity("liblzma-devel", "5.8.3", "fixture_1", 1, lzdev_archive)
        dev_lzma["files"] = {
            "include/lzma.h": {"sha256": digest(lzma_header), "size": len(lzma_header)},
            "include/lzma/base.h": {"sha256": digest(lzma_base), "size": len(lzma_base)},
        }
        runtime_zlib = identity("libzlib", "1.3.2", "fixture_3", 3, libz_archive)
        runtime_zlib["files"] = {
            "lib/libz.so.1.3.2": {"sha256": digest(zlib_library), "size": len(zlib_library)},
        }
        runtime_lzma = identity("liblzma", "5.8.3", "fixture_1", 1, liblzma_archive)
        runtime_lzma["files"] = {
            "lib/liblzma.so.5.8.3": {"sha256": digest(lzma_library), "size": len(lzma_library)},
        }
        # The development/runtime build string is intentionally identical for liblzma.
        dev_lzma["build"] = runtime_lzma["build"]

        build_lock = {
            "schema_version": 1,
            "target": {"architecture": "x86_64", "glibc_floor": "2.17"},
            "development_packages": [dev_zlib, dev_lzma],
            "runtime_packages": [runtime_zlib, runtime_lzma],
            "output": {
                "headers": {
                    relative: record["sha256"]
                    for package in (dev_zlib, dev_lzma)
                    for relative, record in package["files"].items()
                },
                "libraries": {
                    "zlib": {
                        "source_package": "libzlib",
                        "source_path": "lib/libz.so.1.3.2",
                        "relative_path": "lib/libz.so.1.3.2",
                        "sha256": digest(zlib_library),
                    },
                    "lzma": {
                        "source_package": "liblzma",
                        "source_path": "lib/liblzma.so.5.8.3",
                        "relative_path": "lib/liblzma.so.5.8.3",
                        "sha256": digest(lzma_library),
                    },
                },
            },
            "policy": {
                "dependency_solver_used": False,
                "development_replay_input_format": "@EXPLICIT-url-plus-sha256",
                "index_access": False,
                "network_access_after_replay": False,
                "runtime_packages_from_verified_fenics_prefix": True,
                "output_files_regular": True,
            },
        }
        build_lock_path = root / "build-deps-lock.json"
        build_lock_path.write_text(json.dumps(build_lock), encoding="utf-8")

        fenics_lock = {
            "format_version": 1,
            "architecture": "x86_64",
            "record_count": 2,
            "packages": [
                {key: runtime_zlib[key] for key in IDENTITY_FIELDS},
                {key: runtime_lzma[key] for key in IDENTITY_FIELDS},
            ],
        }
        fenics_lock_path = root / "fenics-lock.json"
        fenics_lock_path.write_text(json.dumps(fenics_lock), encoding="utf-8")

        dev_prefix = root / "dev-prefix"
        runtime_prefix = root / "runtime-prefix"
        cache = root / "cache"
        for prefix in (dev_prefix, runtime_prefix):
            (prefix / "conda-meta").mkdir(parents=True)
        cache.mkdir()

        def install(prefix: Path, record: dict, file_bytes: dict[str, bytes]) -> None:
            meta = {key: record[key] for key in IDENTITY_FIELDS}
            meta["files"] = list(file_bytes)
            (prefix / "conda-meta" / f"{record['name']}.json").write_text(json.dumps(meta), encoding="utf-8")
            for relative, payload in file_bytes.items():
                path = prefix / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)

        install(dev_prefix, dev_zlib, {"include/zconf.h": zconf_header, "include/zlib.h": zlib_header})
        install(dev_prefix, dev_lzma, {"include/lzma.h": lzma_header, "include/lzma/base.h": lzma_base})
        install(runtime_prefix, runtime_zlib, {"lib/libz.so.1.3.2": zlib_library})
        install(runtime_prefix, runtime_lzma, {"lib/liblzma.so.5.8.3": lzma_library})
        for record, payload in (
            (dev_zlib, zlib_archive),
            (dev_lzma, lzdev_archive),
            (runtime_zlib, libz_archive),
            (runtime_lzma, liblzma_archive),
        ):
            (cache / Path(record["url"]).name).write_bytes(payload)

        packages = sorted(
            ({key: record[key] for key in IDENTITY_FIELDS} for record in (runtime_zlib, runtime_lzma)),
            key=lambda record: record["name"],
        )
        prefix_evidence = {
            "format_version": 1,
            "architecture": "x86_64",
            "lock_sha256": digest(fenics_lock_path.read_bytes()),
            "solver_used": False,
            "package_hash_verification": "cached-package-sha256-plus-installed-record-match",
            "replay_input_format": "@EXPLICIT-url-plus-sha256",
            "installed_record_count": 2,
            "packages": packages,
            "verified_package_archives": [
                {
                    "name": record["name"],
                    "filename": Path(record["url"]).name,
                    "sha256": record["sha256"],
                    "cache_paths": [Path(record["url"]).name],
                }
                for record in (runtime_zlib, runtime_lzma)
            ],
        }
        prefix_evidence_path = root / "prefix-evidence.json"
        prefix_evidence_path.write_text(json.dumps(prefix_evidence), encoding="utf-8")
        return build_lock_path, fenics_lock_path, prefix_evidence_path, runtime_prefix, dev_prefix, cache, root / "out"

    def test_prepare_copies_only_pinned_regular_headers_and_libraries(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            build_lock, fenics_lock, evidence, runtime_prefix, dev_prefix, cache, output = self._fixture(root)
            manifest_path = output / "manifest.json"
            manifest = prepare(
                lock_path=build_lock,
                fenics_package_lock_path=fenics_lock,
                fenics_prefix_evidence_path=evidence,
                fenics_prefix=runtime_prefix,
                development_prefix=dev_prefix,
                package_cache=cache,
                output_dir=output,
                manifest_path=manifest_path,
            )
            self.assertTrue(manifest["production_frozen"])
            self.assertEqual(manifest["prefix"], "prefix")
            self.assertEqual(manifest["tree"]["file_count"], 6)
            self.assertFalse(manifest["input_evidence"]["dependency_solver_used"])
            self.assertEqual(
                {path.relative_to(output / "prefix").as_posix() for path in (output / "prefix").rglob("*") if path.is_file()},
                set(manifest["headers"]) | {record["relative_path"] for record in manifest["libraries"].values()},
            )

    def test_prepare_rejects_extra_development_package(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            build_lock, fenics_lock, evidence, runtime_prefix, dev_prefix, cache, output = self._fixture(root)
            (dev_prefix / "conda-meta" / "unexpected.json").write_text(
                json.dumps({"name": "unexpected", "files": []}), encoding="utf-8"
            )
            with self.assertRaisesRegex(BuildDepsError, "package set must be exactly"):
                prepare(
                    lock_path=build_lock,
                    fenics_package_lock_path=fenics_lock,
                    fenics_prefix_evidence_path=evidence,
                    fenics_prefix=runtime_prefix,
                    development_prefix=dev_prefix,
                    package_cache=cache,
                    output_dir=output,
                    manifest_path=output / "manifest.json",
                )


if __name__ == "__main__":
    unittest.main()
