from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

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
        lock = mod.verify_runtime_lock(contract, RUNTIME_LOCK)
        self.assertEqual(lock["record_count"], 128)
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

    def test_dt_needed_delta_must_remove_only_libpsl(self) -> None:
        stock = {"needed": ["libc.so.6", "libnghttp2.so.14", "libpsl.so.5", "libssl.so.3"]}
        candidate = {"needed": ["libc.so.6", "libnghttp2.so.14", "libssl.so.3"]}
        self.assertEqual(mod.validate_needed_delta(stock, candidate), {"removed": ["libpsl.so.5"], "added": []})

        with self.assertRaisesRegex(mod.ProbeError, "introduced new DT_NEEDED"):
            mod.validate_needed_delta(stock, {"needed": candidate["needed"] + ["libidn2.so.0"]})
        with self.assertRaisesRegex(mod.ProbeError, "remove only a libpsl"):
            mod.validate_needed_delta(stock, {"needed": ["libc.so.6", "libssl.so.3"]})

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

    def test_curl_edge_and_independent_mpich_icu_control_are_both_required(self) -> None:
        contract = mod.load_contract(CONTRACT)
        stock_curl = closure_names(
            "libcurl.so.4",
            "libpsl.so.5",
            "libicuuc.so.78",
            "libicudata.so.78",
        )
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
        mod.validate_closure_contract(contract, stock_curl, candidate_curl, candidate_hdf5, mpich_control)

        candidate_with_icu = copy.deepcopy(candidate_curl)
        candidate_with_icu["reached_names"].append("libicuuc.so.78")
        with self.assertRaisesRegex(mod.ProbeError, "still reaches"):
            mod.validate_closure_contract(contract, stock_curl, candidate_with_icu, candidate_hdf5, mpich_control)

        broken_control = closure_names("libmpi.so.12", "libhwloc.so.15", "libxml2.so.16")
        with self.assertRaisesRegex(mod.ProbeError, "control disappeared"):
            mod.validate_closure_contract(contract, stock_curl, candidate_curl, candidate_hdf5, broken_control)

    def test_stock_libcurl_must_first_prove_the_documented_icu_edge(self) -> None:
        contract = mod.load_contract(CONTRACT)
        incomplete_stock = closure_names("libcurl.so.4", "libpsl.so.5")
        candidate_curl = closure_names("libcurl.so.4")
        candidate_hdf5 = closure_names("libhdf5.so.310", "libcurl.so.4", "libmpi.so.12", "libicuuc.so.78")
        control = closure_names(
            "libmpi.so.12", "libhwloc.so.15", "libxml2.so.16", "libicuuc.so.78", "libicudata.so.78"
        )
        with self.assertRaisesRegex(mod.ProbeError, "stock libcurl closure no longer proves"):
            mod.validate_closure_contract(contract, incomplete_stock, candidate_curl, candidate_hdf5, control)

    def test_hosted_evidence_binds_verified_runtime_build_tools_and_stock_failure(self) -> None:
        prefix = {
            "solver_used": False,
            "lock_record_count": 128,
            "installed_record_count": 128,
            "verified_package_archive_count": 128,
            "package_hash_verification": "cached-package-sha256-plus-installed-record-match",
            "replay_input_format": "@EXPLICIT-url-plus-sha256",
            "packages": [{} for _ in range(128)],
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
