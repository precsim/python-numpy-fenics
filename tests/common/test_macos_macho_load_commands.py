"""Exercise reusable macOS Mach-O load-command parsing without a macOS runner."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "macos" / "package_fenics_overlay.py"
NAMES = {"macho_rpaths", "macho_minos"}


def load_parsers(run):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    methods = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]
    assert len(methods) == len(NAMES)
    scope = {"Path": Path, "run": run}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope["macho_rpaths"], scope["macho_minos"]


class MacOSLoadCommandTests(unittest.TestCase):
    def test_reuses_single_post_signing_snapshot_for_both_audits(self) -> None:
        def unused(*args):
            raise AssertionError("reused load commands must not launch otool")

        rpaths, minos = load_parsers(unused)
        lines = [
            "Mach header", "cmd LC_RPATH", "cmdsize 48",
            "path @loader_path/../lib (offset 12)",
            "cmd LC_BUILD_VERSION", "minos 14.0", "sdk 15.0",
            "cmd LC_RPATH", "path /forbidden/path (offset 12)",
        ]
        self.assertEqual(rpaths(Path("binary"), load_commands=lines),
                         ["@loader_path/../lib", "/forbidden/path"])
        self.assertEqual(minos(Path("binary"), load_commands=lines), "14.0")

    def test_default_and_legacy_minimum_version_still_work(self) -> None:
        invocations = []
        lines = ["cmd LC_VERSION_MIN_MACOSX", "cmdsize 16", "version 13.0", "sdk 14.0"]

        def fake_run(*args):
            invocations.append(args)
            return SimpleNamespace(stdout="\n".join(lines))

        rpaths, minos = load_parsers(fake_run)
        self.assertEqual(minos(Path("binary")), "13.0")
        self.assertEqual(rpaths(Path("binary")), [])
        self.assertEqual(invocations, [("otool", "-l", "binary")] * 2)


if __name__ == "__main__":
    unittest.main()
