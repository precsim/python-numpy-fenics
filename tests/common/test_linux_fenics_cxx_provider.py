"""Host-only contract tests for the Linux private C++ provider builder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.linux.build_fenics_cxx_provider import (
    DEFAULT_RECIPE,
    ProviderBuildError,
    build_manifest,
    canonical_text_sha256,
    deterministic_link_environment,
    load_recipe,
    render_link_argv,
    transform_contract,
    validate_checked_in_locks,
    validate_prefix_evidence,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


class LinuxFenicsCxxProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.recipe = load_recipe(DEFAULT_RECIPE)

    def test_checked_in_recipe_binds_runtime_and_build_tool_locks(self) -> None:
        locks = validate_checked_in_locks(self.recipe)
        self.assertEqual(locks["runtime_package_lock"]["record_count"], 128)
        self.assertEqual(
            locks["runtime_package_lock"]["sha256"],
            "fb5d53e3ea6b060763de137d9b5425c7fb702eeff10c35dffc512190e212794f",
        )
        self.assertEqual(
            locks["runtime_source_explicit"]["canonical_sha256"],
            "945ad865b5f9404354a5c87bf4db9c93e97da54b77d734780ad3524296277a27",
        )
        self.assertEqual(
            locks["native_build_tool_explicit"]["canonical_sha256"],
            "763466504eb4af7a1ac9948269d8b228b6df683df5e5d0f30c434249e8529690",
        )
        self.assertFalse(locks["native_build_tool_explicit"]["used_for_provider_link"])
        self.assertEqual(
            {record["name"] for record in locks["packages"]},
            {
                "binutils_impl_linux-64",
                "conda-gcc-specs",
                "gcc_impl_linux-64",
                "kernel-headers_linux-64",
                "libgcc",
                "libgcc-devel_linux-64",
                "libstdcxx-devel_linux-64",
                "sysroot_linux-64",
            },
        )

    def test_recipe_pins_known_gcc16_provider_proof(self) -> None:
        self.assertEqual(
            self.recipe["link_inputs"]["gcc_driver"]["sha256"],
            "1b866d8450249db5231194834ff440ad20b1f5e9316c94cd6b59c2da575545e0",
        )
        self.assertEqual(
            self.recipe["link_inputs"]["libstdcxx_static"]["sha256"],
            "d98becf54a78aa4a20be3c8d8be2905d2471b804f110e983205aaed710831fe8",
        )
        contract = transform_contract(self.recipe)
        self.assertEqual(
            contract.input_sha256,
            "c7a9a4e04b175b3cd60c9a0a30e9168f9032822c6b4a1cfe737a89d190f3cd58",
        )
        self.assertEqual(
            contract.output_sha256,
            "39473bb7046bd62af7fbafda82c18d6f755de28a9e32ec65734985d72ff71cb4",
        )
        self.assertEqual(contract.expected_unique_count, 146)
        self.assertEqual(contract.expected_dynsym_count, 7261)
        self.assertEqual(contract.expected_soname, "libfenics_cxxrt.so.1")
        self.assertEqual(
            contract.expected_version_max,
            {"GLIBC": (2, 17), "GLIBCXX": None, "CXXABI": None, "GCC": (4, 3, 0)},
        )

        evidence = json.loads(
            (REPO_ROOT / "tests" / "fixtures" / "linux-gnu-unique-provider-evidence.json").read_text(
                encoding="utf-8"
            )
        )
        proof = next(item for item in evidence["providers"] if item["id"] == "locked-gcc16-linker-proof")
        self.assertEqual(contract.input_sha256, proof["transform_input_sha256"])
        self.assertEqual(contract.output_sha256, proof["transformed_sha256"])
        self.assertEqual(contract.expected_unique_count, proof["expected_unique_count"])
        self.assertEqual(contract.expected_dynsym_count, proof["expected_dynsym_count"])
        self.assertEqual(
            self.recipe["link_inputs"]["gcc_driver"]["sha256"],
            proof["provider_link_input_evidence"]["locked_gcc_sha256"],
        )
        self.assertEqual(
            self.recipe["link_inputs"]["libstdcxx_static"]["sha256"],
            proof["provider_link_input_evidence"]["locked_libstdcxx_a_sha256"],
        )

    def test_version_script_hash_is_strict(self) -> None:
        version_script = REPO_ROOT / self.recipe["link_inputs"]["version_script"]["path"]
        self.assertEqual(
            hashlib.sha256(version_script.read_bytes()).hexdigest(),
            self.recipe["link_inputs"]["version_script"]["sha256"],
        )

    def test_exact_link_argv_has_no_shell_expansion(self) -> None:
        inputs = {
            "gcc_driver": {"path": "/locked/bin/x86_64-conda-linux-gnu-gcc"},
            "libstdcxx_static": {"path": "/locked/lib/gcc/x86_64-conda-linux-gnu/16.2.0/libstdc++.a"},
            "version_script": {"path": "/workspace/scripts/linux/fenics_cxxrt.map"},
        }
        output = Path("/out/libfenics_cxxrt.so.1.linked")
        argv = render_link_argv(self.recipe, inputs, output)
        self.assertEqual(
            argv,
            [
                inputs["gcc_driver"]["path"],
                "-shared",
                "-Wl,--no-undefined",
                "-Wl,-soname,libfenics_cxxrt.so.1",
                "-Wl,--version-script=/workspace/scripts/linux/fenics_cxxrt.map",
                "-Wl,--whole-archive",
                inputs["libstdcxx_static"]["path"],
                "-Wl,--no-whole-archive",
                "-lm",
                "-ldl",
                "-lpthread",
                "-lrt",
                "-o",
                str(output),
            ],
        )

    def test_link_environment_is_minimal_and_prefix_scoped(self) -> None:
        env = deterministic_link_environment(self.recipe, Path("/locked"))
        self.assertEqual(set(env), {"LANG", "LC_ALL", "PATH", "SOURCE_DATE_EPOCH", "TZ"})
        self.assertEqual(env["PATH"], str(Path("/locked/bin").resolve()))
        self.assertEqual(env["LC_ALL"], "C")

    def test_prefix_evidence_must_bind_exact_runtime_lock(self) -> None:
        lock_sha = self.recipe["runtime_package_lock"]["sha256"]
        payload = {
            "format_version": 1,
            "architecture": "x86_64",
            "lock_sha256": lock_sha,
            "lock_record_count": 128,
            "installed_record_count": 128,
            "verified_package_archive_count": 128,
            "package_hash_verification": "cached-package-sha256-plus-installed-record-match",
            "solver_used": False,
        }
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "evidence.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            observed = validate_prefix_evidence(path, lock_sha)
            self.assertEqual(observed["lock_sha256"], lock_sha)
            payload["lock_sha256"] = "0" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ProviderBuildError, "pinned runtime lock"):
                validate_prefix_evidence(path, lock_sha)

    def test_manifest_remains_explicitly_unqualified(self) -> None:
        provider_bytes = b"provider"
        provider_sha = hashlib.sha256(provider_bytes).hexdigest()
        transform = {"after": {"sha256": provider_sha}}
        with tempfile.TemporaryDirectory() as raw:
            provider = Path(raw) / "libfenics_cxxrt.so.1"
            provider.write_bytes(provider_bytes)
            manifest = build_manifest(
                recipe_path=DEFAULT_RECIPE,
                recipe=self.recipe,
                locks={},
                prefix_evidence={},
                archives=[],
                inputs={},
                toolchain={},
                link_record={},
                transform_record=transform,
                provider=provider,
            )
        self.assertEqual(manifest["kind"], "fenics-private-cxx-runtime")
        self.assertEqual(manifest["qualification_status"], "unqualified-reproduction-only")
        self.assertFalse(manifest["source_build"])
        self.assertFalse(manifest["limitations"]["hosted_glibc217_poisson_qualified"])
        self.assertFalse(manifest["limitations"]["source_rebuild_provenance_eligible"])
        self.assertEqual(manifest["library"]["symbol_version"], "FENICS_CXXRT_1.0")

    def test_runtime_explicit_digest_uses_canonical_line_endings(self) -> None:
        expected = self.recipe["runtime_source_explicit"]["canonical_sha256"]
        path = REPO_ROOT / self.recipe["runtime_source_explicit"]["path"]
        self.assertEqual(canonical_text_sha256(path), expected)


if __name__ == "__main__":
    unittest.main()
