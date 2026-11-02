"""Guard the strict public Linux native ABI compatibility contract."""

from __future__ import annotations

from pathlib import PurePosixPath
import unittest

from scripts.linux.package_fenics_overlay import (
    ALLOWED_NONNUMERIC_VERSION_TOKENS,
    REQUIRED_SYMBOL_CEILINGS,
    SYSROOT_ALLOWED_NONNUMERIC_VERSION_TOKENS,
    numeric_version,
    path_exclusion_reason,
)


class LinuxAbiPolicyTests(unittest.TestCase):
    def test_symbol_ceilings_are_fixed(self) -> None:
        self.assertEqual(
            REQUIRED_SYMBOL_CEILINGS,
            {
                "GLIBC": (2, 17),
                "GLIBCXX": (3, 4, 19),
                "CXXABI": (1, 3, 7),
                "GCC": (4, 8, 0),
            },
        )

    def test_glibc_private_is_only_sysroot_scoped(self) -> None:
        self.assertNotIn("GLIBC_PRIVATE", ALLOWED_NONNUMERIC_VERSION_TOKENS)
        self.assertEqual(SYSROOT_ALLOWED_NONNUMERIC_VERSION_TOKENS, {"GLIBC_PRIVATE"})

    def test_nonnumeric_version_is_not_misparsed_as_numeric(self) -> None:
        self.assertIsNone(numeric_version("GLIBC_PRIVATE", "GLIBC"))
        self.assertEqual(numeric_version("GLIBC_2.17", "GLIBC"), (2, 17))

    def test_pruned_debugger_plugin_cannot_leave_dangling_alias(self) -> None:
        self.assertEqual(
            path_exclusion_reason(PurePosixPath("lib/gcc/x86_64-conda-linux-gnu/16.2.0/libcc1.so")),
            "unused-gcc-debugger-plugin",
        )
        self.assertIsNone(
            path_exclusion_reason(PurePosixPath("lib/gcc/x86_64-conda-linux-gnu/16.2.0/libstdc++.a"))
        )


if __name__ == "__main__":
    unittest.main()
