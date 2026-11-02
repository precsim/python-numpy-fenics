"""Fail-closed tests for Linux conda package prefix substitutions."""

from __future__ import annotations

from pathlib import PurePosixPath
import unittest

from scripts.linux.package_fenics_overlay import (
    cache_path_to_installed_path,
    replace_conda_prefix,
)


class CondaPrefixRelocationTests(unittest.TestCase):
    def test_noarch_python_paths_are_mapped_to_cp312(self) -> None:
        link = {"noarch": {"type": "python"}}
        self.assertEqual(
            cache_path_to_installed_path(PurePosixPath("site-packages/dolfinx/a.so"), link),
            PurePosixPath("lib/python3.12/site-packages/dolfinx/a.so"),
        )
        self.assertEqual(
            cache_path_to_installed_path(PurePosixPath("python-scripts/ffcx"), link),
            PurePosixPath("bin/ffcx"),
        )
        self.assertEqual(
            cache_path_to_installed_path(PurePosixPath("site-packages/a.py"), {}),
            PurePosixPath("site-packages/a.py"),
        )

    def test_text_replaces_each_exact_placeholder(self) -> None:
        result, count = replace_conda_prefix(b"/old/a /old/b", b"/old", b"@ROOT@", "text")
        self.assertEqual((result, count), (b"@ROOT@/a @ROOT@/b", 2))

    def test_binary_preserves_length_and_nul_padding(self) -> None:
        data = b"prefix=/placeholder/one and /placeholder/two\0other\0"
        result, count = replace_conda_prefix(data, b"/placeholder", b"@R@", "binary")
        self.assertEqual(count, 2)
        self.assertEqual(len(result), len(data))
        self.assertIn(b"prefix=@R@/one and @R@/two", result)
        self.assertEqual(result.split(b"\0")[-2], b"other")
        self.assertNotIn(b"/placeholder", result)

    def test_binary_rejects_overflow_and_unknown_mode(self) -> None:
        with self.assertRaises(SystemExit):
            replace_conda_prefix(b"abc\0", b"a", b"larger", "binary")
        with self.assertRaises(SystemExit):
            replace_conda_prefix(b"abc", b"a", b"x", "invented")

    def test_empty_or_nul_placeholder_is_rejected(self) -> None:
        for placeholder in (b"", b"a\0b"):
            with self.subTest(placeholder=placeholder), self.assertRaises(SystemExit):
                replace_conda_prefix(b"abc", placeholder, b"x", "text")


if __name__ == "__main__":
    unittest.main()
