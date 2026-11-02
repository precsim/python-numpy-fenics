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

    def test_contract_rejects_any_broader_configure_delta(self) -> None:
        data = json.loads(CONTRACT.read_text(encoding="utf-8"))
        data["probe"]["feedstock_delta"]["other_changes"] = ["--disable-ros3"]
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "contract.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(mod.ProbeError, "one-flag feedstock delta"):
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

    def test_hdf5_edge_and_independent_mpich_icu_control_are_both_required(self) -> None:
        contract = mod.load_contract(CONTRACT)
        stock_hdf5 = closure_names(
            "libhdf5.so.310",
            "libcurl.so.4",
            "libpsl.so.5",
            "libicuuc.so.78",
            "libicudata.so.78",
        )
        candidate_hdf5 = closure_names("libhdf5.so.310", "libcurl.so.4", "libssl.so.3")
        mpich_control = closure_names(
            "libmpi.so.12",
            "libhwloc.so.15",
            "libxml2.so.16",
            "libicuuc.so.78",
            "libicudata.so.78",
        )
        mod.validate_closure_contract(contract, stock_hdf5, candidate_hdf5, mpich_control)

        candidate_with_icu = copy.deepcopy(candidate_hdf5)
        candidate_with_icu["reached_names"].append("libicuuc.so.78")
        with self.assertRaisesRegex(mod.ProbeError, "still reaches"):
            mod.validate_closure_contract(contract, stock_hdf5, candidate_with_icu, mpich_control)

        broken_control = closure_names("libmpi.so.12", "libhwloc.so.15", "libxml2.so.16")
        with self.assertRaisesRegex(mod.ProbeError, "control disappeared"):
            mod.validate_closure_contract(contract, stock_hdf5, candidate_hdf5, broken_control)

    def test_stock_hdf5_must_first_prove_the_documented_icu_edge(self) -> None:
        contract = mod.load_contract(CONTRACT)
        incomplete_stock = closure_names("libhdf5.so.310", "libcurl.so.4", "libpsl.so.5")
        candidate = closure_names("libhdf5.so.310", "libcurl.so.4")
        control = closure_names(
            "libmpi.so.12", "libhwloc.so.15", "libxml2.so.16", "libicuuc.so.78", "libicudata.so.78"
        )
        with self.assertRaisesRegex(mod.ProbeError, "stock HDF5 closure no longer proves"):
            mod.validate_closure_contract(contract, incomplete_stock, candidate, control)


if __name__ == "__main__":
    unittest.main()
