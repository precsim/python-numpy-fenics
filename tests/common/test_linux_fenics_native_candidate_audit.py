from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.linux import audit_fenics_native_candidate as audit
from scripts.linux.rebuild_fenics_native import (
    canonical_json_sha,
    sha256,
    validate_native_closure_report,
)


class NativeCandidateAuditTests(unittest.TestCase):
    def make_fixture(self, root: Path):
        candidate_root = root / "candidate"
        roots = {name: candidate_root / name for name in ("prefix", "audit", "wheels", "build", "evidence")}
        for path in roots.values():
            path.mkdir(parents=True, exist_ok=True)
        cache = roots["build"] / "dolfinx" / "CMakeCache.txt"
        cache.parent.mkdir(parents=True)
        cache.write_text(
            "DOLFINX_ENABLE_ADIOS2:BOOL=OFF\nDOLFINX_ENABLE_KAHIP:BOOL=OFF\n",
            encoding="utf-8",
        )

        locked = root / "locked"
        (locked / "conda-meta").mkdir(parents=True)
        metadata = {}

        def elf(relative: str, owner: str, *, content: bytes | None = None):
            path = locked / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content or (b"\x7fELF" + relative.encode("utf-8")))
            metadata[path.resolve()] = {"owner": owner}
            return path

        def package(name: str, paths: list[str]):
            payload = {
                "name": name,
                "paths_data": {"paths": [{"_path": item} for item in paths]},
            }
            (locked / "conda-meta" / f"{name}-1.json").write_text(json.dumps(payload), encoding="utf-8")

        stock_paths = {
            "fenics-libdolfinx": ["lib/libdolfinx.so.0.11.0"],
            "libxml2-16": ["lib/libxml2.so.16"],
            "petsc": ["lib/libpetsc.so.3.25"],
            "mpich": ["lib/libmpi.so.12"],
            "libhwloc": ["lib/libhwloc.so.15"],
            "hdf5": ["lib/libhdf5.so.310"],
            "libcurl": ["lib/libcurl.so.4"],
            "libpsl": ["lib/libpsl.so.5"],
            "icu": ["lib/libicuuc.so.78", "lib/libicudata.so.78"],
            "kahip": ["lib/libkahip.so.1", "share/kahip/data.txt"],
            "libadios2": ["lib/libadios2_core.so.2"],
            "zeromq": ["lib/libzmq.so.5"],
            "optional-plugin": ["lib/liboptional.so.1"],
        }
        for owner, paths in stock_paths.items():
            package(owner, paths)
            for relative in paths:
                if relative.endswith(".txt"):
                    path = locked / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("data", encoding="utf-8")
                else:
                    elf(relative, owner)

        candidate_files = {}
        for relative, owner in (
            ("lib/libdolfinx.so.0.11.0", "fenics-libdolfinx"),
            ("lib/libxml2.so.16", "libxml2-16"),
        ):
            path = roots["prefix"] / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x7fELFcandidate:" + relative.encode("utf-8"))
            candidate_files[relative] = (path, owner)
            metadata[path.resolve()] = {"owner": owner}

        replacements = []
        elf_records = []
        for relative, (path, owner) in sorted(candidate_files.items()):
            replacements.append(
                {
                    "destination": relative,
                    "owner": owner,
                    "sha256": sha256(path),
                    "bytes": path.stat().st_size,
                }
            )
            elf_records.append(
                {"path": f"prefix/{relative}", "sha256": sha256(path)}
            )
        binding = {"replacements": replacements, "elf": elf_records, "wheels": []}
        binding_sha = canonical_json_sha(binding)
        candidate = {
            "sha256": "a" * 64,
            "roots": roots,
            "locked_prefix": locked,
            "dolfinx_build_configuration": {
                "cmake_cache": {
                    "path": str((roots["build"] / "dolfinx/CMakeCache.txt").resolve()),
                    "sha256": sha256(roots["build"] / "dolfinx/CMakeCache.txt"),
                },
                "flags": {
                    "DOLFINX_ENABLE_ADIOS2": "OFF",
                    "DOLFINX_ENABLE_KAHIP": "OFF",
                },
            },
            "candidate_output_binding": binding,
            "candidate_output_binding_sha256": binding_sha,
        }

        specs = {
            "libdolfinx.so.0.11.0": ("libdolfinx.so.0.11.0", ["libpetsc.so.3.25", "libmpi.so.12"], ["GLIBC_2.17"]),
            "libxml2.so.16": ("libxml2.so.16", [], ["GLIBC_2.17"]),
            "libpetsc.so.3.25": ("libpetsc.so.3.25", ["libmpi.so.12", "libhdf5.so.310"], ["GLIBC_2.17"]),
            "libmpi.so.12": ("libmpi.so.12", ["libhwloc.so.15"], ["GLIBC_2.17"]),
            "libhwloc.so.15": ("libhwloc.so.15", ["libxml2.so.16"], ["GLIBC_2.17"]),
            "libhdf5.so.310": ("libhdf5.so.310", ["libcurl.so.4"], ["GLIBC_2.17"]),
            "libcurl.so.4": ("libcurl.so.4", ["libpsl.so.5"], ["GLIBC_2.17"]),
            "libpsl.so.5": ("libpsl.so.5", ["libicuuc.so.78"], ["GLIBC_2.17"]),
            "libicuuc.so.78": ("libicuuc.so.78", ["libicudata.so.78"], ["GLIBC_2.17", "GLIBCXX_3.4.30"]),
            "libicudata.so.78": ("libicudata.so.78", [], ["GLIBC_2.17"]),
            "libkahip.so.1": ("libkahip.so.1", [], ["GLIBC_2.17", "GLIBCXX_3.4.19"]),
            "libadios2_core.so.2": ("libadios2_core.so.2", ["libzmq.so.5"], ["GLIBC_2.17"]),
            "libzmq.so.5": ("libzmq.so.5", [], ["GLIBC_2.17"]),
            "liboptional.so.1": ("liboptional.so.1", ["libkahip.so.1"], ["GLIBC_2.17"]),
        }

        def fake_inspect(path: Path, _readelf: Path):
            soname, needed, versions = specs[path.name]
            return {
                "sha256": sha256(path),
                "soname": soname,
                "needed": sorted(needed),
                "required_versions": sorted(versions),
                "abi_maxima": audit.abi_maxima(sorted(versions)),
            }

        return candidate, fake_inspect, specs

    def test_writes_bound_closure_and_analyzes_mpich_icu_branch(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate, fake_inspect, _ = self.make_fixture(root)
            output = root / "native-closure.json"
            handoff = root / "candidate-handoff.json"
            handoff.write_text("{}", encoding="utf-8")
            with mock.patch.object(audit, "validate_candidate_build_handoff", return_value=candidate), mock.patch.object(
                audit, "inspect_elf", side_effect=fake_inspect
            ):
                report = audit.audit_candidate(handoff, output, Path("readelf"))
            self.assertEqual(report["unresolved_non_system"], [])
            self.assertEqual(report["candidate_output_binding"], candidate["candidate_output_binding"])
            self.assertEqual(
                report["candidate_output_binding_sha256"],
                candidate["candidate_output_binding_sha256"],
            )
            for name in ("kahip", "libadios2", "zeromq"):
                self.assertEqual(report["candidates"][name]["reached_paths"], [])
                self.assertTrue(
                    all(
                        item["in_closure"] is False
                        for item in report["candidates"][name]["reverse_direct_noncandidate"]
                    )
                )
            kahip_reverse = report["candidates"]["kahip"]["reverse_direct_noncandidate"]
            self.assertEqual([item["consumer"] for item in kahip_reverse], ["lib/liboptional.so.1"])
            self.assertEqual(report["candidates"]["kahip"]["abi_maxima"]["GLIBCXX"], "3.4.19")
            self.assertEqual(report["candidates"]["icu"]["reached_paths"], ["lib/libicudata.so.78", "lib/libicuuc.so.78"])
            branch = report["candidates"]["icu"]["mpich_hwloc_libxml2_branch"]
            self.assertIn("libhwloc", branch["reached_owners"])
            self.assertIn("libxml2-16", branch["reached_owners"])
            self.assertEqual(branch["icu_reached_paths"], [])
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)
            validated = validate_native_closure_report(output)
            self.assertEqual(
                validated["candidate_output_binding_sha256"],
                candidate["candidate_output_binding_sha256"],
            )

    def test_rejects_live_candidate_tampering_after_handoff_validation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate, fake_inspect, _ = self.make_fixture(root)
            target = candidate["roots"]["prefix"] / "lib/libdolfinx.so.0.11.0"
            target.write_bytes(target.read_bytes() + b"tampered")
            with mock.patch.object(audit, "inspect_elf", side_effect=fake_inspect):
                with self.assertRaisesRegex(audit.AuditError, "candidate ELF changed"):
                    audit.build_report(candidate, Path("readelf"))

    def test_rejects_unresolved_dependency(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate, fake_inspect, specs = self.make_fixture(root)
            specs["libdolfinx.so.0.11.0"] = (
                "libdolfinx.so.0.11.0",
                ["libmissing.so.1"],
                ["GLIBC_2.17"],
            )
            with mock.patch.object(audit, "inspect_elf", side_effect=fake_inspect):
                with self.assertRaisesRegex(audit.AuditError, "unresolved non-system"):
                    audit.build_report(candidate, Path("readelf"))

    def test_prefers_exact_filename_over_conflicting_soname_only_provider(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate, fake_inspect, specs = self.make_fixture(root)
            locked = candidate["locked_prefix"]
            duplicate = locked / "lib/libpetsc-duplicate.so"
            duplicate.write_bytes(b"\x7fELFduplicate-petsc")
            (locked / "conda-meta" / "duplicate-petsc-1.json").write_text(
                json.dumps(
                    {
                        "name": "duplicate-petsc",
                        "paths_data": {"paths": [{"_path": "lib/libpetsc-duplicate.so"}]},
                    }
                ),
                encoding="utf-8",
            )
            specs["libpetsc-duplicate.so"] = (
                "libpetsc.so.3.25",
                [],
                ["GLIBC_2.17"],
            )
            with mock.patch.object(audit, "inspect_elf", side_effect=fake_inspect):
                report = audit.build_report(candidate, Path("readelf"))
            self.assertEqual(report["unresolved_non_system"], [])

    def test_rejects_dolfinx_build_flag_disagreement(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate, fake_inspect, _ = self.make_fixture(root)
            cache = candidate["roots"]["build"] / "dolfinx/CMakeCache.txt"
            cache.write_text(
                "DOLFINX_ENABLE_ADIOS2:BOOL=ON\nDOLFINX_ENABLE_KAHIP:BOOL=OFF\n",
                encoding="utf-8",
            )
            with mock.patch.object(audit, "inspect_elf", side_effect=fake_inspect):
                with self.assertRaisesRegex(audit.AuditError, "CMake cache changed before closure audit"):
                    audit.build_report(candidate, Path("readelf"))

    def test_readelf_parser_uses_dynamic_needed_and_version_needs(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "libsample.so.1"
            path.write_bytes(b"\x7fELFsample")

            def fake_readelf(_readelf, *args, path):
                if args == ("-hW",):
                    return "Class: ELF64\nMachine: Advanced Micro Devices X86-64\n"
                if args == ("-dW",):
                    return (
                        " 0x0000000000000001 (NEEDED) Shared library: [libm.so.6]\n"
                        " 0x000000000000000e (SONAME) Library soname: [libsample.so.1]\n"
                    )
                self.assertEqual(args, ("--version-info", "-W"))
                return (
                    "Version definition section '.gnu.version_d' contains 1 entry:\n"
                    "  Name: GLIBCXX_9.9.9\n"
                    "Version needs section '.gnu.version_r' contains 1 entry:\n"
                    "  File: libc.so.6  Cnt: 1\n"
                    "  Name: GLIBC_2.17  Flags: none  Version: 2\n"
                )

            with mock.patch.object(audit, "_run_readelf", side_effect=fake_readelf):
                record = audit.inspect_elf(path, Path("readelf"))
            self.assertEqual(record["needed"], ["libm.so.6"])
            self.assertEqual(record["soname"], "libsample.so.1")
            self.assertEqual(record["required_versions"], ["GLIBC_2.17"])

    def test_provider_index_collapses_only_byte_identical_regular_soname_aliases(self):
        first = {
            "path": "lib/libsame.so.1",
            "sha256": "a" * 64,
            "soname": "libsame.so.1",
            "aliases": {"libsame.so.1"},
        }
        versioned = {
            "path": "lib/libsame.so.1.2.3",
            "sha256": "a" * 64,
            "soname": "libsame.so.1",
            "aliases": {"libsame.so.1.2.3"},
        }
        index = audit._provider_index([versioned, first])
        self.assertEqual([item["path"] for item in index["libsame.so.1"]], [first["path"]])

        conflict = dict(versioned, sha256="b" * 64)
        index = audit._provider_index([first, conflict])
        self.assertEqual(
            audit._resolve_provider("libsame.so.1", index, "consumer.so")["path"],
            first["path"],
        )

        other_exact = {
            "path": "alt/libsame.so.1",
            "sha256": "c" * 64,
            "soname": "libsame.so.1",
            "aliases": {"libsame.so.1"},
        }
        index = audit._provider_index([first, other_exact])
        with self.assertRaisesRegex(audit.AuditError, "ambiguous provider"):
            audit._resolve_provider("libsame.so.1", index, "consumer.so")

    def test_provider_index_exact_alias_overrides_conflicting_soname_only_provider(self):
        gnu = {
            "path": "lib/libgomp.so.1.0.0",
            "sha256": "a" * 64,
            "soname": "libgomp.so.1",
            "aliases": {"libgomp.so.1.0.0"},
        }
        llvm = {
            "path": "lib/libomp.so.5",
            "sha256": "b" * 64,
            "soname": "libomp.so.5",
            "aliases": {"libomp.so.5", "libgomp.so.1"},
        }
        index = audit._provider_index([gnu, llvm])
        self.assertEqual(
            audit._resolve_provider("libgomp.so.1", index, "consumer.so")["path"],
            llvm["path"],
        )

    def test_libgcc_runtime_provider_precedes_compiler_private_copy(self):
        runtime = {"path": "lib/libgcc_s.so.1", "sha256": "a" * 64,
                   "soname": "libgcc_s.so.1", "aliases": {"libgcc_s.so.1"}}
        compiler = {"path": "lib/gcc/x86_64-conda-linux-gnu/16.2.0/libgcc_s.so.1",
                    "sha256": "b" * 64, "soname": "libgcc_s.so.1", "aliases": {"libgcc_s.so.1"}}
        index = audit._provider_index([compiler, runtime])
        self.assertEqual(audit._resolve_provider("libgcc_s.so.1", index, "lib/libbasix.so")["path"], runtime["path"])
        index["libgcc_s.so.1"].append(dict(compiler, path="other/libgcc_s.so.1"))
        with self.assertRaisesRegex(audit.AuditError, "ambiguous provider"):
            audit._resolve_provider("libgcc_s.so.1", index, "lib/libbasix.so")
        compiler_only = audit._provider_index(
            [
                compiler,
                dict(
                    compiler,
                    path="lib/gcc/x86_64-conda-linux-gnu/17.0.0/libgcc_s.so.1",
                    sha256="c" * 64,
                ),
            ]
        )
        with self.assertRaisesRegex(audit.AuditError, "ambiguous provider"):
            audit._resolve_provider("libgcc_s.so.1", compiler_only, "lib/libbasix.so")


if __name__ == "__main__":
    unittest.main()
