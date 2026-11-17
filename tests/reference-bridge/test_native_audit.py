"""Focused tests for reference MEX native dependency parsing."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "consumer-kit" / "tools" / "audit-mex.py"
SPEC = importlib.util.spec_from_file_location("reference_bridge_audit_mex", AUDIT_PATH)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class NativeAuditTests(unittest.TestCase):
    def test_native_audit_records_sha256_of_audited_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            mex = root / "candidate.mex"
            mex.write_bytes(b"native audit candidate")
            output = root / "native-audit.json"
            exports = "00000001 T mexFunction\n00000002 R __octave_mex_soversion__\n"
            with patch.object(sys, "argv", [
                "audit-mex.py", "--target", "linux-x64", "--mex", str(mex),
                "--output", str(output),
            ]), patch.object(audit, "run", side_effect=["", exports, ""]):
                self.assertEqual(audit.main(), 0)
            record = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(record["mex_sha256"], hashlib.sha256(mex.read_bytes()).hexdigest())

    def test_qualification_rejects_native_audit_from_other_mex(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            mex = root / "candidate.mex"
            mex.write_bytes(b"candidate A")
            mex_sha = hashlib.sha256(mex.read_bytes()).hexdigest()
            build = root / "build.json"
            build.write_text(json.dumps({"mex_sha256": mex_sha}), encoding="utf-8")
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({"platform": "linux-x64", "profile": "core"}), encoding="utf-8")
            native = root / "native.json"
            output = root / "qualification.json"
            command = [
                sys.executable,
                str(ROOT / "consumer-kit" / "tools" / "write-qualification.py"),
                "--platform", "linux-x64", "--profile", "core", "--mex", str(mex),
                "--build-provenance", str(build), "--runtime-receipt", str(receipt),
                "--native-audit", str(native), "--output", str(output),
                "--check", "native_boundary=pass",
            ]
            for invalid in (None, "f" * 64):
                with self.subTest(invalid=invalid):
                    record = {"target": "linux-x64"}
                    if invalid is not None:
                        record["mex_sha256"] = invalid
                    native.write_text(json.dumps(record), encoding="utf-8")
                    result = subprocess.run(command, text=True, capture_output=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("native audit MEX SHA-256 differs from qualified candidate", result.stderr)
                    self.assertFalse(output.exists())

    def test_pe_exports_require_mex_entry_and_octave_metadata_only(self) -> None:
        output = r"""
Dump of file C:\agent-dev\build\bridge.mex

File Type: DLL

  Section contains the following exports for bridge.mex

    00000000 characteristics
    FFFFFFFF time date stamp
        0.00 version
           1 ordinal base
           2 number of functions
           2 number of names

    ordinal hint RVA      name

          1    0 00001000 __octave_mex_soversion__
          2    1 00002000 mexFunction

  Summary
"""
        raw = audit.pe_exports(output)
        self.assertEqual(raw, ["__octave_mex_soversion__", "mexFunction"])
        self.assertEqual(
            audit.require_exact_exports("win-x64", raw),
            ["__octave_mex_soversion__", "mexFunction"],
        )

    def test_elf_exports_reject_unexpected_default_visibility(self) -> None:
        output = """\
0000000000002000 R __octave_mex_soversion__
0000000000003000 T mexFunction
0000000000004000 T accidentalPublicApi
"""
        raw = audit.nm_exports(output)
        with self.assertRaisesRegex(ValueError, "unexpected=.*accidentalPublicApi"):
            audit.require_exact_exports("linux-x64", raw)

    def test_elf_crt_init_fini_exports_are_not_m_ex_api(self) -> None:
        raw = ["_init", "_fini", "mexFunction", "__octave_mex_soversion__"]
        self.assertEqual(
            audit.require_exact_exports("linux-x64", raw),
            ["__octave_mex_soversion__", "mexFunction"],
        )

    def test_exports_reject_missing_octave_metadata(self) -> None:
        with self.assertRaisesRegex(ValueError, "missing=.*__octave_mex_soversion__"):
            audit.require_exact_exports("linux-x64", ["mexFunction"])

    def test_macho_exports_normalize_c_leading_underscore(self) -> None:
        output = """\
0000000000001000 S ___octave_mex_soversion__
0000000000002000 T _mexFunction
"""
        raw = audit.nm_exports(output)
        self.assertEqual(raw, ["___octave_mex_soversion__", "_mexFunction"])
        self.assertEqual(
            audit.require_exact_exports("macos-arm64", raw),
            ["__octave_mex_soversion__", "mexFunction"],
        )

    def test_pe_checkout_path_named_python_is_not_a_dependency(self) -> None:
        output = r"""
Dump of file C:\agent-dev\precsim_python-numpy-fenics\build\bridge.mex

File Type: DLL

  Image has the following dependencies:

    KERNEL32.dll
    VCRUNTIME140.dll

  Summary
        1000 .data
        2000 .text
"""
        dependencies = audit.pe_dependencies(output)
        self.assertEqual(dependencies, ["KERNEL32.dll", "VCRUNTIME140.dll"])
        self.assertEqual(audit.forbidden_dependencies(dependencies), [])

    def test_pe_actual_python_import_is_rejected(self) -> None:
        output = r"""
Dump of file C:\agent-dev\build\bridge.mex

File Type: DLL

  Image has the following dependencies:

    KERNEL32.dll
    python314.dll

  Summary
"""
        dependencies = audit.pe_dependencies(output)
        self.assertEqual(
            audit.forbidden_dependencies(dependencies),
            ["python314.dll"],
        )


if __name__ == "__main__":
    unittest.main()
