from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.linux import probe_fenics_icu_edge as mod


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "locks" / "linux" / "linux-x86_64-fenics-icu-edge-probe.json"
RUNTIME_LOCK = ROOT / "locks" / "linux" / "linux-x86_64-fenics-package-lock.json"


def closure_names(*names: str) -> dict:
    return {"reached_names": sorted(names)}


class LinuxFenicsIcuEdgeProbeTests(unittest.TestCase):
    def test_contract_pins_public_curl_feedstock_and_unchanged_runtime_lock(self) -> None:
        contract = mod.load_contract(CONTRACT)
        self.assertEqual(contract["curl_source"]["sha256"], mod.EXPECTED_CURL_SOURCE_SHA256)
        self.assertEqual(contract["feedstock"]["commit"], mod.EXPECTED_FEEDSTOCK_COMMIT)
        self.assertEqual(
            {name: record["git_blob_sha1"] for name, record in contract["feedstock"]["files"].items()},
            mod.EXPECTED_FEEDSTOCK_BLOBS,
        )
        self.assertEqual(
            {name: record["sha256"] for name, record in contract["feedstock"]["files"].items()},
            mod.EXPECTED_FEEDSTOCK_SHA256,
        )
        lock = mod.verify_runtime_lock(contract, RUNTIME_LOCK)
        self.assertEqual(lock["record_count"], 124)
        self.assertEqual(lock["packages"]["libcurl"]["version"], "8.23.0")
        self.assertEqual(lock["packages"]["icu"]["version"], "78.3")
        self.assertIn("disabled", contract["capability_impact"]["libcurl_public_suffix_list"])

    def test_contract_requires_complete_independent_configure_argv(self) -> None:
        data = json.loads(CONTRACT.read_text(encoding="utf-8"))
        data["probe"]["configure_flags"].append("--disable-ros3")
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "contract.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(mod.ProbeError, "complete pinned argv"):
                mod.load_contract(path)
        data = json.loads(CONTRACT.read_text(encoding="utf-8"))
        data["probe"]["configure_delta_from_feedstock"] = {"other_changes": []}
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "contract.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(mod.ProbeError, "must not assert a feedstock configure delta"):
                mod.load_contract(path)

    def test_contract_rejects_feedstock_content_or_origin_drift(self) -> None:
        data = json.loads(CONTRACT.read_text(encoding="utf-8"))
        data["feedstock"]["files"]["recipe/build.sh"]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "contract.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(mod.ProbeError, "SHA-256 identity changed"):
                mod.load_contract(path)

        data = json.loads(CONTRACT.read_text(encoding="utf-8"))
        data["feedstock"]["files"]["recipe/build.sh"]["url"] = (
            "https://raw.githubusercontent.com/example/curl-feedstock/"
            f"{mod.EXPECTED_FEEDSTOCK_COMMIT}/recipe/build.sh"
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "contract.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(mod.ProbeError, "URL is not pinned to the exact commit"):
                mod.load_contract(path)

    def test_dt_needed_delta_must_remove_only_libpsl(self) -> None:
        stock = {"needed": ["libc.so.6", "libnghttp2.so.14", "libpsl.so.5", "libssl.so.3"]}
        candidate = {"needed": ["libc.so.6", "libnghttp2.so.14", "libssl.so.3"]}
        self.assertEqual(mod.validate_needed_delta(stock, candidate), {"removed": ["libpsl.so.5"], "added": []})

        with self.assertRaisesRegex(mod.ProbeError, "introduced new DT_NEEDED"):
            mod.validate_needed_delta(stock, {"needed": candidate["needed"] + ["libidn2.so.0"]})
        with self.assertRaisesRegex(mod.ProbeError, "remove only a libpsl"):
            mod.validate_needed_delta(stock, {"needed": ["libc.so.6", "libssl.so.3"]})

    def test_provider_index_accepts_byte_identical_regular_soname_aliases_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prefix = Path(raw)
            library_root = prefix / "lib"
            library_root.mkdir()
            soname = library_root / "libsame.so.1"
            versioned = library_root / "libsame.so.1.2.3"
            soname.write_bytes(b"\x7fELFsame-provider")
            versioned.write_bytes(soname.read_bytes())

            def fake_read_elf(path, _readelf, strict_abi=False):
                return {
                    "path": str(path),
                    "sha256": mod.sha256_file(path),
                    "soname": "libsame.so.1",
                    "needed": [],
                    "rpath": [],
                    "required_versions": [],
                    "version_max": {},
                }

            with mock.patch.object(mod, "read_elf", side_effect=fake_read_elf):
                providers, _ = mod.build_provider_index(prefix, Path("readelf"))
            self.assertEqual(providers["libsame.so.1"], soname.resolve())

            versioned.write_bytes(b"\x7fELFdifferent-provider")
            with mock.patch.object(mod, "read_elf", side_effect=fake_read_elf):
                with self.assertRaisesRegex(mod.ProbeError, "multiple locked providers"):
                    mod.build_provider_index(prefix, Path("readelf"))

    def test_provider_index_exact_symlink_overrides_soname_only_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prefix = Path(raw)
            library_root = prefix / "lib"
            library_root.mkdir()
            exact = library_root / "libsame.so.1"
            versioned = library_root / "libsame.so.1.2.3"
            exact.write_bytes(b"\x7fELFsame-provider")
            versioned.write_bytes(exact.read_bytes())
            alias = library_root / "libalias.so.1"
            try:
                alias.symlink_to(versioned.name)
            except OSError as exc:
                self.skipTest(f"host cannot create symlinks: {exc}")

            def fake_read_elf(path, _readelf, strict_abi=False):
                return {
                    "path": str(path),
                    "sha256": mod.sha256_file(path),
                    "soname": "libsame.so.1",
                    "needed": [],
                    "rpath": [],
                    "required_versions": [],
                    "version_max": {},
                }

            with mock.patch.object(mod, "read_elf", side_effect=fake_read_elf):
                providers, _ = mod.build_provider_index(prefix, Path("readelf"))
            self.assertEqual(providers["libalias.so.1"], versioned.resolve())

    def test_find_root_collapses_only_byte_identical_regular_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            prefix = Path(raw)
            library_root = prefix / "lib"
            library_root.mkdir()
            soname = library_root / "libroot.so.1"
            versioned = library_root / "libroot.so.1.2.3"
            soname.write_bytes(b"\x7fELFroot")
            versioned.write_bytes(soname.read_bytes())
            self.assertEqual(mod.find_root(prefix, "libroot.so.*"), soname.resolve())

            versioned.write_bytes(b"\x7fELFdifferent-root")
            with self.assertRaisesRegex(mod.ProbeError, "expected one ELF root identity"):
                mod.find_root(prefix, "libroot.so.*")

    def test_strict_abi_and_rpath_checks_fail_closed(self) -> None:
        maxima = mod.audit_version_tokens(
            ["GLIBC_2.17", "GLIBCXX_3.4.19", "CXXABI_1.3.7", "GCC_4.8.0"], strict=True
        )
        self.assertEqual(maxima["GLIBC"], "2.17")
        mod.audit_rpaths(["$ORIGIN:$ORIGIN/../lib"], strict=True)

        with self.assertRaisesRegex(mod.ProbeError, "ABI ceiling exceeded"):
            mod.audit_version_tokens(["GLIBC_2.18"], strict=True)
        with self.assertRaisesRegex(mod.ProbeError, "unsupported nonnumeric"):
            mod.audit_version_tokens(["GLIBC_PRIVATE"], strict=True)
        with self.assertRaisesRegex(mod.ProbeError, "absolute/non-relative RPATH"):
            mod.audit_rpaths(["/tmp/build/lib:$ORIGIN"], strict=True)

    def test_curl_probe_relocation_removes_only_known_build_tool_rpath(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            install = root / "install"
            (install / "lib").mkdir(parents=True)
            (install / "bin").mkdir()
            library = install / "lib" / "libcurl.so.4.8.0"
            binary = install / "bin" / "curl"
            for path in (library, binary):
                path.write_bytes(b"\x7fELFtest")
            # Use a drive-free path so the POSIX RPATH separator is not
            # confused with a Windows drive letter during local tests.
            tools = Path("build-tools")
            bad_path = str(tools / "lib")
            calls = []

            def fake_read_elf(path, _readelf, strict_abi=False):
                return {
                    "path": str(path), "rpath": ["$ORIGIN"] if strict_abi else [f"{bad_path}:$ORIGIN"],
                    "sha256": mod.sha256_file(path),
                }

            with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/patchelf"), \
                 mock.patch.object(mod, "read_elf", side_effect=fake_read_elf), \
                 mock.patch.object(mod, "run", side_effect=lambda argv: calls.append(argv)):
                records = mod.relocate_curl_probe(install, tools)
            self.assertEqual(len(records), 2)
            self.assertEqual(len(calls), 2)
            self.assertEqual({argv[2] for argv in calls}, {"$ORIGIN", "$ORIGIN/../lib"})

            def unexpected_read_elf(path, _readelf, strict_abi=False):
                return {"rpath": ["/unexpected/lib"]}

            with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/patchelf"), \
                 mock.patch.object(mod, "read_elf", side_effect=unexpected_read_elf):
                with self.assertRaisesRegex(mod.ProbeError, "unexpected curl probe build RPATH"):
                    mod.relocate_curl_probe(install, tools)

    def test_curl_edge_and_independent_mpich_icu_control_are_both_required(self) -> None:
        contract = mod.load_contract(CONTRACT)
        stock_curl = closure_names(
            "libcurl.so.4",
            "libpsl.so.5",
            "libicuuc.so.78",
            "libicudata.so.78",
        )
        stock_hdf5 = closure_names(
            "libhdf5.so.310",
            "libcurl.so.4",
            "libpsl.so.5",
            "libicuuc.so.78",
            "libicudata.so.78",
        )
        stock_libpsl = closure_names("libpsl.so.5", "libicuuc.so.78", "libicudata.so.78")
        candidate_curl = closure_names("libcurl.so.4", "libssl.so.3")
        candidate_hdf5 = closure_names(
            "libhdf5.so.310", "libcurl.so.4", "libmpi.so.12", "libicuuc.so.78", "libicudata.so.78"
        )
        mpich_control = closure_names(
            "libmpi.so.12",
            "libhwloc.so.15",
            "libxml2.so.16",
            "libicuuc.so.78",
            "libicudata.so.78",
        )
        mod.validate_closure_contract(
            contract, stock_curl, stock_hdf5, stock_libpsl, candidate_curl, candidate_hdf5, mpich_control
        )

        candidate_with_icu = copy.deepcopy(candidate_curl)
        candidate_with_icu["reached_names"].append("libicuuc.so.78")
        with self.assertRaisesRegex(mod.ProbeError, "still reaches"):
            mod.validate_closure_contract(
                contract, stock_curl, stock_hdf5, stock_libpsl, candidate_with_icu, candidate_hdf5, mpich_control
            )

        broken_control = closure_names("libmpi.so.12", "libhwloc.so.15", "libxml2.so.16")
        with self.assertRaisesRegex(mod.ProbeError, "control disappeared"):
            mod.validate_closure_contract(
                contract, stock_curl, stock_hdf5, stock_libpsl, candidate_curl, candidate_hdf5, broken_control
            )

    def test_stock_libcurl_must_first_prove_the_documented_icu_edge(self) -> None:
        contract = mod.load_contract(CONTRACT)
        incomplete_stock = closure_names("libcurl.so.4", "libpsl.so.5")
        stock_hdf5 = closure_names(
            "libhdf5.so.310", "libcurl.so.4", "libpsl.so.5", "libicuuc.so.78", "libicudata.so.78"
        )
        stock_libpsl = closure_names("libpsl.so.5", "libicuuc.so.78", "libicudata.so.78")
        candidate_curl = closure_names("libcurl.so.4")
        candidate_hdf5 = closure_names("libhdf5.so.310", "libcurl.so.4", "libmpi.so.12", "libicuuc.so.78")
        control = closure_names(
            "libmpi.so.12", "libhwloc.so.15", "libxml2.so.16", "libicuuc.so.78", "libicudata.so.78"
        )
        with self.assertRaisesRegex(mod.ProbeError, "stock libcurl closure no longer proves"):
            mod.validate_closure_contract(
                contract, incomplete_stock, stock_hdf5, stock_libpsl, candidate_curl, candidate_hdf5, control
            )

    def test_stock_hdf5_must_prove_the_documented_curl_psl_icu_edge(self) -> None:
        contract = mod.load_contract(CONTRACT)
        stock_curl = closure_names("libcurl.so.4", "libpsl.so.5", "libicuuc.so.78", "libicudata.so.78")
        incomplete_hdf5 = closure_names("libhdf5.so.310", "libcurl.so.4")
        stock_libpsl = closure_names("libpsl.so.5", "libicuuc.so.78", "libicudata.so.78")
        candidate_curl = closure_names("libcurl.so.4")
        candidate_hdf5 = closure_names("libhdf5.so.310", "libcurl.so.4", "libmpi.so.12", "libicuuc.so.78")
        control = closure_names(
            "libmpi.so.12", "libhwloc.so.15", "libxml2.so.16", "libicuuc.so.78", "libicudata.so.78"
        )
        with self.assertRaisesRegex(mod.ProbeError, "stock HDF5 closure no longer proves"):
            mod.validate_closure_contract(
                contract, stock_curl, incomplete_hdf5, stock_libpsl, candidate_curl, candidate_hdf5, control
            )

    def test_hdf5_direct_curl_and_libpsl_icu_segments_are_required(self) -> None:
        contract = mod.load_contract(CONTRACT)
        mod.validate_hdf5_needed_contract(contract, {"needed": ["libcurl.so.4", "libmpi.so.12"]})
        with self.assertRaisesRegex(mod.ProbeError, "directly needs the curl provider"):
            mod.validate_hdf5_needed_contract(contract, {"needed": ["libmpi.so.12"]})

        stock_curl = closure_names("libcurl.so.4", "libpsl.so.5", "libicuuc.so.78", "libicudata.so.78")
        stock_hdf5 = closure_names(
            "libhdf5.so.310", "libcurl.so.4", "libpsl.so.5", "libicuuc.so.78", "libicudata.so.78"
        )
        incomplete_libpsl = closure_names("libpsl.so.5")
        candidate_curl = closure_names("libcurl.so.4")
        candidate_hdf5 = closure_names("libhdf5.so.310", "libcurl.so.4", "libmpi.so.12", "libicuuc.so.78")
        control = closure_names(
            "libmpi.so.12", "libhwloc.so.15", "libxml2.so.16", "libicuuc.so.78", "libicudata.so.78"
        )
        with self.assertRaisesRegex(mod.ProbeError, "PSL->ICU edge"):
            mod.validate_closure_contract(
                contract,
                stock_curl,
                stock_hdf5,
                incomplete_libpsl,
                candidate_curl,
                candidate_hdf5,
                control,
            )

    def test_ldd_resolution_must_use_the_rebuilt_libcurl_provider(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            candidate_lib = root / "candidate" / "lib"
            candidate_lib.mkdir(parents=True)
            expected = candidate_lib / "libcurl.so.4"
            expected.write_bytes(b"candidate")
            output = f"\tlibcurl.so.4 => {expected} (0x00007f00)\n\tlibc.so.6 => /lib64/libc.so.6 (0x00007e00)\n"
            self.assertEqual(mod.validate_linker_resolution_output(output, candidate_lib), str(expected.resolve()))

            stock = root / "locked" / "libcurl.so.4"
            stock.parent.mkdir(parents=True)
            stock.write_bytes(b"stock")
            wrong_output = f"\tlibcurl.so.4 => {stock} (0x00007f00)\n"
            with self.assertRaisesRegex(mod.ProbeError, "outside rebuilt candidate"):
                mod.validate_linker_resolution_output(wrong_output, candidate_lib)

            with self.assertRaisesRegex(mod.ProbeError, "exactly one libcurl.so.4 provider"):
                mod.validate_linker_resolution_output("\tlibc.so.6 => /lib64/libc.so.6 (0x00007e00)\n", candidate_lib)

    def test_hosted_evidence_binds_verified_runtime_build_tools_and_stock_failure(self) -> None:
        prefix = {
            "solver_used": False,
            "lock_record_count": 124,
            "installed_record_count": 124,
            "verified_package_archive_count": 124,
            "package_hash_verification": "cached-package-sha256-plus-installed-record-match",
            "replay_input_format": "@EXPLICIT-url-plus-sha256",
            "packages": [{} for _ in range(124)],
        }
        build_tools = {
            "purpose": "linux-fenics-cxx20-build-toolchain-only",
            "build_only": True,
            "runtime_lock_modified": False,
            "dependency_solver_used": False,
            "package_count": 41,
            "source_prefix_record_count": 41,
            "exact_prefix": True,
            "roots": {
                "gcc_linux-64": {"version": "14.4.0", "build": "hc469d41_2"},
                "gxx_linux-64": {"version": "14.4.0", "build": "h0047a55_2"},
                "sysroot_linux-64": {"version": "2.17", "build": "h0157908_18"},
                "pkg-config": {"version": "0.29.2", "build": "h1114479_1013"},
                "make": {"version": "4.4.1", "build": "h1114479_4"},
            },
        }
        preflight = {
            "format_version": 1,
            "stage": "stock-binary-closure-before-selective-rebuild",
            "status": "fail",
            "source_rebuilds_authorized": False,
            "violating_packages": ["icu"],
        }
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            prefix_path = root / "prefix.json"
            tools_path = root / "tools.json"
            preflight_path = root / "preflight.json"
            prefix_path.write_text(json.dumps(prefix), encoding="utf-8")
            tools_path.write_text(json.dumps(build_tools), encoding="utf-8")
            preflight_path.write_text(json.dumps(preflight), encoding="utf-8")
            evidence = mod.validate_hosted_evidence(prefix_path, tools_path, preflight_path)
            self.assertEqual(set(evidence), {"prefix_verification", "build_tool_verification", "stock_abi_preflight"})

            bad_tools = copy.deepcopy(build_tools)
            bad_tools["roots"]["gcc_linux-64"]["version"] = "15.0.0"
            tools_path.write_text(json.dumps(bad_tools), encoding="utf-8")
            with self.assertRaisesRegex(mod.ProbeError, "root mismatch: gcc_linux-64"):
                mod.validate_hosted_evidence(prefix_path, tools_path, preflight_path)

            tools_path.write_text(json.dumps(build_tools), encoding="utf-8")
            bad_preflight = copy.deepcopy(preflight)
            bad_preflight["violating_packages"] = ["fenics-dolfinx"]
            preflight_path.write_text(json.dumps(bad_preflight), encoding="utf-8")
            with self.assertRaisesRegex(mod.ProbeError, "does not prove ICU"):
                mod.validate_hosted_evidence(prefix_path, tools_path, preflight_path)


if __name__ == "__main__":
    unittest.main()
