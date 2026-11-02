from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "linux" / "rebuild_fenics_native.py"
SPEC = importlib.util.spec_from_file_location("rebuild_fenics_native", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FenicsNativeRebuildTests(unittest.TestCase):
    def test_committed_sources_match_worker10_recipe_evidence(self) -> None:
        data = json.loads((ROOT / "locks" / "linux" / "linux-x86_64-fenics-rebuild-sources.json").read_text())
        self.assertEqual(tuple(data["sources"]), mod.REQUIRED_SOURCES)
        self.assertEqual(data["rebuild_package_closure"], list(mod.REQUIRED_PACKAGES))
        self.assertEqual(data["sources"]["basix"]["sha256"], "bb4355f4cd3c98d5e944c2b8596f3240225d0a8ab3bd1344133d2056a7b2c388")
        self.assertEqual(data["sources"]["dolfinx"]["sha256"], "4df167823d316da519c1b2f8d5d0afc20de1405de601dab51da67a405b25cc2f")
        self.assertEqual(data["sources"]["libxml2"]["sha256"], "a569ee9c59eabb197a0d5047046d757ee14c86fb2d4a734555b633cb5584ad25")
        self.assertEqual(data["patches"]["spdlog_fix"]["sha256"], "b58437661aac1bb5375aa42710feae3b061958431fc2d431d5b97fd894b77f10")

    def test_wrong_source_hash_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "src"
            source.mkdir()
            manifest = {"schema_version": 1, "sources": {}}
            for key in mod.REQUIRED_SOURCES:
                name = f"{key}.tar.gz"
                payload = key.encode()
                (source / name).write_bytes(payload)
                manifest["sources"][key] = {"filename": name, "sha256": digest(payload), "url": "https://example.invalid/source"}
            manifest["sources"]["basix"]["sha256"] = "0" * 64
            manifest_path = root / "sources.json"
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(mod.InputError, "source basix SHA-256 mismatch"):
                mod.validate_sources(manifest_path, source)

    def test_python_builder_rejects_wrong_executable_hash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            python = root / "python"
            python.write_bytes(b"#!/bin/sh\nexit 0\n")
            python.chmod(0o755)
            manifest = root / "python-builder.json"
            manifest.write_text(json.dumps({
                "format_version": 1,
                "kind": "fenics-python-wheel-builder",
                "python": {"path": str(python), "sha256": "f" * 64},
                "modules": {name: "0" for name in mod.PYTHON_BUILD_MODULES},
            }))
            with self.assertRaisesRegex(mod.InputError, "Python wheel-builder executable SHA-256 mismatch"):
                mod.validate_python_builder(manifest)

    def test_package_closure_rejects_missing_or_wrong_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "cache"
            cache.mkdir()
            packages = []
            for name in mod.REQUIRED_PACKAGES:
                filename = f"{name}-1.conda"
                payload = name.encode()
                packages.append({"name": name, "version": "1", "build": "0", "url": f"https://example.invalid/{filename}", "sha256": digest(payload)})
                (cache / filename).write_bytes(payload)
            lock = root / "lock.json"
            lock.write_text(json.dumps({"packages": packages}))
            source_lock = {"rebuild_package_closure": list(mod.REQUIRED_PACKAGES)}
            evidence = mod.validate_package_closure(lock, cache, source_lock)
            self.assertEqual([entry["name"] for entry in evidence], list(mod.REQUIRED_PACKAGES))
            (cache / "fmt-1.conda").write_bytes(b"changed")
            with self.assertRaisesRegex(mod.InputError, "package fmt SHA-256 mismatch"):
                mod.validate_package_closure(lock, cache, source_lock)

    def test_scratch_must_be_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            scratch = Path(raw) / "scratch"
            scratch.mkdir()
            (scratch / "unexpected").write_text("x")
            with self.assertRaisesRegex(mod.InputError, "scratch root must be absent or empty"):
                mod.prepare_scratch(scratch, [], Path(raw) / "patch", Path(raw) / "provider")

    def test_spdlog_patch_is_exact_and_not_reapplied(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw)
            target = source / "include" / "spdlog" / "tweakme.h"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"prefix\n// #define SPDLOG_FMT_EXTERNAL\nsuffix\n")
            evidence = mod.apply_spdlog_patch(source)
            self.assertNotEqual(evidence["input_sha256"], evidence["output_sha256"])
            self.assertIn(b"#ifndef SPDLOG_FMT_EXTERNAL", target.read_bytes())
            with self.assertRaisesRegex(mod.InputError, "exact locked patch context"):
                mod.apply_spdlog_patch(source)

    def test_native_closure_omission_gate_and_icu_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = {
                "dolfinx_build_flags": {"DOLFINX_ENABLE_ADIOS2": "OFF", "DOLFINX_ENABLE_KAHIP": "OFF"},
                "unresolved_non_system": [],
                "closure_elf_count": 113,
                "candidates": {
                    name: {"reached_paths": [], "reverse_direct_noncandidate": [], "staged_paths": [name], "abi_maxima": {}}
                    for name in ("kahip", "libadios2", "zeromq")
                },
            }
            report["candidates"]["icu"] = {"reached_paths": ["lib/libicuuc.so.78.3"], "reverse_direct_noncandidate": []}
            path = root / "closure.json"
            path.write_text(json.dumps(report))
            evidence = mod.validate_native_closure_report(path)
            self.assertTrue(evidence["icu_resolution_required"])
            report["candidates"]["kahip"]["reached_paths"] = ["lib/libkahip.so"]
            path.write_text(json.dumps(report))
            with self.assertRaisesRegex(mod.InputError, "not proven for kahip"):
                mod.validate_native_closure_report(path)

    def test_stock_abi_preflight_binds_exact_selective_rebuild_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lock = root / "lock.json"
            lock.write_text("{}", encoding="utf-8")
            violations = [
                {
                    "path": f"lib/offender-{index}.so",
                    "owners": [mod.EXPECTED_STOCK_ABI_VIOLATING_PACKAGES[index % len(mod.EXPECTED_STOCK_ABI_VIOLATING_PACKAGES)]],
                    "required": "GLIBCXX_3.4.21",
                    "ceiling": "GLIBCXX_3.4.19",
                    "file_sha256": "1" * 64,
                }
                for index in range(11)
            ]
            report = {
                "format_version": 1,
                "stage": "stock-binary-closure-before-selective-rebuild",
                "package_lock_sha256": mod.sha256(lock),
                "prefix_verification_sha256": "2" * 64,
                "symbol_ceilings": {"GLIBC": "2.17", "GLIBCXX": "3.4.19", "CXXABI": "1.3.7", "GCC": "4.8.0"},
                "measured_maximum_versions": {"GLIBC": "2.17", "GLIBCXX": "3.4.31", "CXXABI": "1.3.15", "GCC": "4.8.0"},
                "status": "fail",
                "violation_count": len(violations),
                "violating_file_count": len(violations),
                "violating_packages": list(mod.EXPECTED_STOCK_ABI_VIOLATING_PACKAGES),
                "ceiling_violations": violations,
                "unexpected_nonnumeric_requirements": [],
                "sysroot_scoped_nonnumeric_requirements": [],
                "source_rebuilds_authorized": False,
            }
            report["selection_fingerprint_sha256"] = digest(
                json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
            )
            path = root / "preflight.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            evidence = mod.validate_stock_abi_preflight(path, lock)
            self.assertEqual(evidence["violating_packages"], list(mod.EXPECTED_STOCK_ABI_VIOLATING_PACKAGES))
            self.assertFalse(evidence["source_rebuilds_authorized"])

            report["violating_packages"] = report["violating_packages"][:-1]
            report["selection_fingerprint_sha256"] = digest(
                json.dumps({key: value for key, value in report.items() if key != "selection_fingerprint_sha256"}, sort_keys=True, separators=(",", ":")).encode()
            )
            path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(mod.InputError, "violating package set"):
                mod.validate_stock_abi_preflight(path, lock)

    def test_libxml2_build_deps_require_complete_hashed_headers(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prefix = Path(raw) / "deps"
            (prefix / "include" / "lzma").mkdir(parents=True)
            (prefix / "lib").mkdir()
            files = {
                "include/zlib.h": b"zlib",
                "include/zconf.h": b"zconf",
                "include/lzma.h": b"lzma",
                "include/lzma/base.h": b"base",
                "lib/libz.so.1": b"z",
                "lib/liblzma.so.5": b"lz",
            }
            for relative, payload in files.items():
                path = prefix / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            manifest = {
                "format_version": 1,
                "kind": "fenics-libxml2-build-deps",
                "prefix": str(prefix),
                "headers": {relative: digest(payload) for relative, payload in files.items() if relative.startswith("include/")},
                "libraries": {
                    "zlib": {"relative_path": "lib/libz.so.1", "sha256": digest(files["lib/libz.so.1"])},
                    "lzma": {"relative_path": "lib/liblzma.so.5", "sha256": digest(files["lib/liblzma.so.5"])},
                },
                "production_frozen": False,
            }
            manifest_path = Path(raw) / "deps.json"
            manifest_path.write_text(json.dumps(manifest))
            evidence, include, zlib, lzma = mod.validate_libxml2_build_deps(manifest_path)
            self.assertFalse(evidence["production_frozen"])
            self.assertEqual(include, prefix / "include")
            self.assertEqual(zlib, (prefix / "lib/libz.so.1").resolve())
            self.assertEqual(lzma, (prefix / "lib/liblzma.so.5").resolve())
            del manifest["headers"]["include/lzma/base.h"]
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(mod.InputError, "header set is incomplete"):
                mod.validate_libxml2_build_deps(manifest_path)

    def test_provenance_never_claims_completed_source_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "provenance.json"
            mod.write_provenance(out, inputs={}, outputs={}, executed=True)
            data = json.loads(out.read_text())
            self.assertEqual(data["status"], "candidate-unqualified")
            self.assertFalse(data["source_rebuild_provenance_eligible"])
            self.assertEqual(data["qualification"]["poisson"], "not-run")


if __name__ == "__main__":
    unittest.main()
