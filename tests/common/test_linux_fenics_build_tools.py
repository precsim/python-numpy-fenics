"""Policy tests for the standalone Linux FEniCS C++ build-tool lock."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.linux.verify_fenics_build_tools import (
    BuildToolLockError,
    EXPECTED_EXPLICIT_SHA256,
    EXPECTED_PACKAGE_COUNT,
    EXPECTED_ROOTS,
    ExplicitEntry,
    archive_path,
    canonical_text_sha256,
    dependency_closure,
    locate_archive,
    parse_explicit,
    verify_archives,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPLICIT = REPO_ROOT / "locks" / "linux" / "linux-x86_64-fenics-build-tools-explicit.txt"
SOURCE_EVIDENCE = REPO_ROOT / "tests" / "fixtures" / "linux-fenics-cxx-prototype-source-evidence.json"


class LinuxFenicsBuildToolLockTests(unittest.TestCase):
    def test_checked_in_explicit_transaction_is_fixed(self) -> None:
        entries = parse_explicit(EXPLICIT)
        self.assertEqual(len(entries), EXPECTED_PACKAGE_COUNT)
        self.assertEqual(canonical_text_sha256(EXPLICIT), EXPECTED_EXPLICIT_SHA256)
        urls = [entry.url for entry in entries]
        self.assertEqual(len(urls), len(set(urls)))
        text = EXPLICIT.read_text(encoding="utf-8")
        self.assertIn("gxx_linux-64-14.4.0-h0047a55_2.conda", text)
        self.assertIn("gcc_linux-64-14.4.0-hc469d41_2.conda", text)
        self.assertIn("cmake-4.4.2-hff7ac6b_2.conda", text)
        self.assertIn("ninja-1.13.2-h171cf75_1.conda", text)
        self.assertNotIn("patchelf-", text)
        self.assertNotIn("libtool-", text)
        self.assertNotIn("python-3.10", text)

    def test_root_identities_are_gcc14_cxx20_build_roots(self) -> None:
        self.assertEqual(
            EXPECTED_ROOTS,
            {
                "gcc_linux-64": ("14.4.0", "hc469d41_2"),
                "gxx_linux-64": ("14.4.0", "h0047a55_2"),
                "sysroot_linux-64": ("2.17", "h0157908_18"),
                "cmake": ("4.4.2", "hff7ac6b_2"),
                "ninja": ("1.13.2", "h171cf75_1"),
                "pkg-config": ("0.29.2", "h1114479_1013"),
                "make": ("4.4.1", "h1114479_4"),
            },
        )

    def test_dependency_closure_rejects_missing_provider(self) -> None:
        records = {
            name: {
                "name": name,
                "version": version,
                "build": build,
                "depends": [],
            }
            for name, (version, build) in EXPECTED_ROOTS.items()
        }
        records["cmake"]["depends"] = ["missing >=1"]
        with self.assertRaisesRegex(BuildToolLockError, "no installed provider"):
            dependency_closure(records)

    def test_archive_path_is_exact_url_cache_path(self) -> None:
        cache = Path("/cache/pkgs")
        url = "https://conda.anaconda.org/conda-forge/linux-64/cmake-4.4.2-hff7ac6b_2.conda"
        self.assertEqual(
            archive_path(cache, url),
            cache
            / "https"
            / "conda.anaconda.org"
            / "conda-forge"
            / "linux-64"
            / "cmake-4.4.2-hff7ac6b_2.conda",
        )

    def test_archive_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw)
            url = "https://conda.anaconda.org/conda-forge/linux-64/fake-1.0-0.conda"
            archive = archive_path(cache, url)
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"archive")
            extracted = archive.with_name("fake-1.0-0") / "info"
            extracted.mkdir(parents=True)
            record = {
                "name": "fake",
                "version": "1.0",
                "build": "0",
                "build_number": 0,
                "subdir": "linux-64",
                "url": url,
                "sha256": "0" * 64,
                "depends": [],
            }
            (extracted / "index.json").write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(BuildToolLockError, "SHA-256 mismatch"):
                verify_archives(cache, [ExplicitEntry(url, "0" * 64)], {"fake": record})

    def test_flat_micromamba_archive_cache_is_supported_without_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw)
            url = "https://conda.anaconda.org/conda-forge/linux-64/fake-1.0-0.conda"
            archive = cache / "fake-1.0-0.conda"
            archive.write_bytes(b"archive")
            self.assertEqual(locate_archive(cache, url), archive)
            nested = cache / "other"
            nested.mkdir()
            (nested / archive.name).write_bytes(b"archive")
            with self.assertRaisesRegex(BuildToolLockError, "expected exactly one cached archive"):
                locate_archive(cache, url)

    def test_explicit_digest_is_sha256_of_canonical_text(self) -> None:
        text = EXPLICIT.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), EXPECTED_EXPLICIT_SHA256)

    def test_preserved_source_pins_are_recipe_backed_but_not_a_rebuild_claim(self) -> None:
        evidence = json.loads(SOURCE_EVIDENCE.read_text(encoding="utf-8"))
        self.assertEqual(evidence["format_version"], 1)
        self.assertEqual(evidence["scope"], "worker10-prototype-source-integrity-only")
        self.assertIs(evidence["source_rebuilds_claimed"], False)
        self.assertEqual(
            {item["id"] for item in evidence["sources"]},
            {"basix", "dolfinx", "fmt", "spdlog", "pugixml"},
        )
        for item in evidence["sources"]:
            self.assertEqual(item["source_sha256"], item["recipe_source_sha256"])
            self.assertEqual(
                item["recipe_package_sha256"], item["verified_recipe_package_archive_sha256"]
            )
        self.assertEqual(
            evidence["patches"]["spdlog_fix_patch_sha256"],
            "b58437661aac1bb5375aa42710feae3b061958431fc2d431d5b97fd894b77f10",
        )
        self.assertIs(evidence["patches"]["spdlog_fix_patch_applied_by_worker10"], True)
        self.assertIs(evidence["patches"]["dolfinx_remove_symlink_patch_applied_by_worker10"], False)


if __name__ == "__main__":
    unittest.main()
