"""Deterministic native ABI stock-preflight evidence regression tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.linux.package_fenics_overlay import write_stock_abi_preflight


class LinuxStockAbiPreflightTests(unittest.TestCase):
    def test_failure_evidence_is_pinned_and_relocatable(self) -> None:
        results = []
        for _ in range(2):
            with tempfile.TemporaryDirectory(prefix="stock-abi-evidence-") as temporary:
                root = Path(temporary)
                offender = root / "fenics-runtime" / "conda" / "lib" / "libexample.so"
                offender.parent.mkdir(parents=True)
                offender.write_bytes(b"fixed binary fixture")
                destination = root / "preflight.json"
                report = write_stock_abi_preflight(
                    destination,
                    root,
                    "1" * 64,
                    "2" * 64,
                    {"GLIBC": (2, 17), "GLIBCXX": (3, 4, 31), "CXXABI": None, "GCC": (4, 3)},
                    [{
                        "path": offender.relative_to(root).as_posix(),
                        "owners": ["example", "example"],
                        "required": "GLIBCXX_3.4.31",
                        "ceiling": "GLIBCXX_3.4.19",
                    }],
                    [],
                    [{
                        "path": "fenics-runtime/conda/x86_64-conda-linux-gnu/sysroot/lib/libc.so",
                        "owners": ["sysroot_linux-64"],
                        "required": "GLIBC_PRIVATE",
                    }],
                )
                self.assertEqual(report, json.loads(destination.read_text()))
                self.assertEqual(report["status"], "fail")
                self.assertFalse(report["source_rebuilds_authorized"])
                self.assertEqual(report["violation_count"], 1)
                self.assertEqual(report["violating_packages"], ["example"])
                self.assertEqual(report["ceiling_violations"][0]["file_sha256"], hashlib.sha256(offender.read_bytes()).hexdigest())
                self.assertEqual(report["symbol_ceilings"]["GLIBCXX"], "3.4.19")
                self.assertEqual(report["sysroot_scoped_nonnumeric_requirements"][0]["required"], "GLIBC_PRIVATE")
                results.append(report)
        self.assertEqual(results[0], results[1])

    def test_clean_stock_is_not_rebuild_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = write_stock_abi_preflight(
                root / "out" / "stock.json", root, "a" * 64, "b" * 64,
                {"GLIBC": (2, 17), "GLIBCXX": (3, 4, 19), "CXXABI": (1, 3, 7), "GCC": (4, 8)},
                [], [], [],
            )
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["violation_count"], 0)
            self.assertFalse(report["source_rebuilds_authorized"])
            self.assertTrue((root / "out" / "stock.json").is_file())


if __name__ == "__main__":
    unittest.main()
