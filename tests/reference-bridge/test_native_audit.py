"""Focused tests for reference MEX native dependency parsing."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "consumer-kit" / "tools" / "audit-mex.py"
SPEC = importlib.util.spec_from_file_location("reference_bridge_audit_mex", AUDIT_PATH)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class NativeAuditTests(unittest.TestCase):
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
