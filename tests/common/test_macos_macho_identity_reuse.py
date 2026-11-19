"""Confirm relocation dependency parsing exposes its already-read dylib ID."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[2] / "scripts/macos/package_fenics_overlay.py"


class MachOIdentityTests(unittest.TestCase):
    def test_identity_is_reused_without_another_metadata_subprocess(self) -> None:
        names = {"macho_identity", "macho_dependencies_and_identity", "macho_dependencies"}
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        self.assertEqual(len(functions), len(names))
        invocations = []

        def fake_run(*argv, **kwargs):
            invocations.append(argv)
            if argv[1] == "-D":
                return SimpleNamespace(stdout="file:\n/old/libsample.dylib\n")
            if argv[1] == "-L":
                return SimpleNamespace(stdout=(
                    "file:\n"
                    "\t/old/libsample.dylib (compatibility version 1.0.0, current version 1.0.0)\n"
                    "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1.0.0)\n"
                ))
            raise AssertionError(argv)

        scope = {"run": fake_run, "Path": Path}
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"), scope)
        dependencies, identity = scope["macho_dependencies_and_identity"](Path("file"))
        self.assertEqual(identity, "/old/libsample.dylib")
        self.assertEqual(dependencies, ["/usr/lib/libSystem.B.dylib"])
        self.assertEqual(invocations, [("otool", "-D", "file"), ("otool", "-L", "file")])
        self.assertEqual(scope["macho_dependencies"](Path("file")), dependencies)


if __name__ == "__main__":
    unittest.main()
