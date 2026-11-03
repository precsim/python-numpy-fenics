"""Host-only contract tests for the Linux private C++ provider builder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
import struct

from scripts.linux.build_fenics_cxx_provider import (
    DEFAULT_RECIPE,
    ProviderBuildError,
    build_manifest,
    canonical_text_sha256,
    deterministic_link_environment,
    load_recipe,
    render_link_argv,
    relocate_provider_rpath,
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
        self.assertEqual(locks["runtime_package_lock"]["record_count"], 124)
        self.assertEqual(
            locks["runtime_package_lock"]["sha256"],
            "ac633e9aa4d23c83af79d078958eec75ff6ab5b08786d25c75c8a21d33123380",
        )
        self.assertEqual(
            locks["runtime_source_explicit"]["canonical_sha256"],
            "739bd3ca25ff63eff57f95291f41871465789e2340bfd984b237702f08fc14b5",
        )
        self.assertEqual(
            locks["native_build_tool_explicit"]["canonical_sha256"],
            "1f4a80d51a0fb1dc4a0ed91c76cfc9927aad9ffd456bf3c127b54e18f042ed4b",
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

    def test_recipe_pins_cp314_hosted_gcc16_provider_candidate(self) -> None:
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
            "1670dd51ae0dd8852b8efdb14fb57ffb9d02801f1eb9f4155c7cd8492f471edb",
        )
        self.assertEqual(
            contract.output_sha256,
            "43ae5b863ef35269e022559019d54c621868fb135c44d4fb52fd9920cc8533ef",
        )
        self.assertEqual(contract.expected_unique_count, 146)
        self.assertEqual(contract.expected_dynsym_count, 7253)
        self.assertEqual(contract.expected_soname, "libfenics_cxxrt.so.1")
        self.assertEqual(
            contract.expected_version_max,
            {"GLIBC": (2, 17), "GLIBCXX": None, "CXXABI": None, "GCC": (4, 2, 0)},
        )

        evidence = json.loads(
            (REPO_ROOT / "tests" / "fixtures" / "linux-gnu-unique-provider-evidence.json").read_text(
                encoding="utf-8"
            )
        )
        # Keep the earlier Poisson-qualified cp312 proof intact.  The cp314
        # candidate is a separately observed hosted link pending the complete
        # overlay and Poisson qualification run.
        historical = next(item for item in evidence["providers"] if item["id"] == "locked-gcc16-linker-proof")
        self.assertEqual(historical["expected_dynsym_count"], 7261)
        proof = next(item for item in evidence["providers"] if item["id"] == "cp314-hosted-gcc16-link-candidate")
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
        lock_sha = self.recipe["runtime_package_lock"]["canonical_sha256"]
        payload = {
            "format_version": 1,
            "architecture": "x86_64",
            "lock_sha256": lock_sha,
            "lock_record_count": 124,
            "installed_record_count": 124,
            "verified_package_archive_count": 124,
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

    def test_relocation_rewrites_only_the_pinned_provider_rpath(self) -> None:
        prefix = Path("/locked-prefix")
        old = b"/locked-prefix/lib"
        data = bytearray(128)
        struct.pack_into("<qQ", data, 0, 15, 0)
        struct.pack_into("<qQ", data, 16, 0, 0)
        data[32:32 + len(old) + 1] = old + b"\0"
        image = SimpleNamespace(
            dynamic=SimpleNamespace(offset=0, size=32, entsize=16, link=1),
            sections=[None, SimpleNamespace(offset=32, size=64, sh_type=3)],
        )
        with mock.patch("scripts.linux.build_fenics_cxx_provider.Elf64Image", return_value=image):
            result = relocate_provider_rpath(bytes(data), prefix)
            self.assertEqual(result[32:32 + len(old)], b"$ORIGIN" + b"\0" * (len(old) - 7))
            self.assertEqual(result[:32], data[:32])
            with self.assertRaisesRegex(ProviderBuildError, "differs from locked prefix"):
                relocate_provider_rpath(bytes(data), Path("/different-prefix"))

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
