"""Guard the strict public Linux native ABI compatibility contract."""

from __future__ import annotations

import unittest

from scripts.linux.package_fenics_overlay import (
    ALLOWED_NONNUMERIC_VERSION_TOKENS,
    REQUIRED_SYMBOL_CEILINGS,
    SYSROOT_ALLOWED_NONNUMERIC_VERSION_TOKENS,
    numeric_version,
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


if __name__ == "__main__":
    unittest.main()
