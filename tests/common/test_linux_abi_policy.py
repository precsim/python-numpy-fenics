"""Guard the strict public Linux native ABI compatibility contract."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import tempfile
import unittest

from scripts.linux.package_fenics_overlay import (
    ALLOWED_NONNUMERIC_VERSION_TOKENS,
    LIBGCC_COMPILER_PRIVATE_RELATIVE,
    LIBGCC_RUNTIME_RELATIVE,
    LIBSTDCXX_COMPILER_PRIVATE_RELATIVE,
    LIBSTDCXX_RUNTIME_RELATIVE,
    REQUIRED_SYMBOL_CEILINGS,
    SYSROOT_ALLOWED_NONNUMERIC_VERSION_TOKENS,
    _resolve_bundled_provider,
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

    def test_pruned_mpich_cxx_binding_cannot_leave_alias(self) -> None:
        self.assertEqual(
            path_exclusion_reason(PurePosixPath("lib/libmpichcxx.so")),
            "unused-mpich-cxx-binding",
        )
        self.assertIsNone(path_exclusion_reason(PurePosixPath("lib/libmpi.so.12")))

    def test_prune_icu_data_build_tools_without_discarding_runtime(self) -> None:
        for path in ("bin/genrb", "bin/icuexportdata"):
            self.assertEqual(
                path_exclusion_reason(PurePosixPath(path)),
                "unused-icu-data-build-tool",
            )
        for path in ("lib/libicuuc.so.78.3", "lib/libicudata.so.78.3", "bin/icuinfo"):
            self.assertIsNone(path_exclusion_reason(PurePosixPath(path)))

    def test_locked_libgcc_duplicate_resolves_by_rpath_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stage_root = Path(raw)
            private_prefix = stage_root / "fenics-runtime" / "conda"
            runtime = private_prefix / LIBGCC_RUNTIME_RELATIVE
            compiler_private = private_prefix / LIBGCC_COMPILER_PRIVATE_RELATIVE
            runtime.parent.mkdir(parents=True)
            compiler_private.parent.mkdir(parents=True)
            runtime.write_bytes(b"runtime")
            compiler_private.write_bytes(b"compiler")
            candidates = {runtime.resolve(), compiler_private.resolve()}

            self.assertEqual(
                _resolve_bundled_provider(
                    stage_root,
                    private_prefix,
                    "lib/libbasix.so",
                    "libgcc_s.so.1",
                    candidates,
                    [runtime.parent.resolve(), compiler_private.parent.resolve()],
                ),
                runtime.resolve(),
            )
            self.assertEqual(
                _resolve_bundled_provider(
                    stage_root,
                    private_prefix,
                    LIBGCC_COMPILER_PRIVATE_RELATIVE.as_posix(),
                    "libgcc_s.so.1",
                    candidates,
                    [compiler_private.parent.resolve(), runtime.parent.resolve()],
                ),
                compiler_private.resolve(),
            )

            other = private_prefix / "other" / "libgcc_s.so.1"
            other.parent.mkdir()
            other.write_bytes(b"other")
            with self.assertRaisesRegex(SystemExit, "ambiguous bundled ELF provider"):
                _resolve_bundled_provider(
                    stage_root,
                    private_prefix,
                    "lib/libbasix.so",
                    "libgcc_s.so.1",
                    candidates | {other.resolve()},
                    [runtime.parent.resolve(), compiler_private.parent.resolve(), other.parent.resolve()],
                )

            outside = stage_root / "outside" / "libgcc_s.so.1"
            outside.parent.mkdir()
            outside.write_bytes(b"outside")
            with self.assertRaisesRegex(SystemExit, "ambiguous bundled ELF provider"):
                _resolve_bundled_provider(
                    stage_root,
                    private_prefix,
                    "lib/libbasix.so",
                    "libgcc_s.so.1",
                    candidates | {outside.resolve()},
                    [runtime.parent.resolve(), compiler_private.parent.resolve(), outside.parent.resolve()],
                )

    def test_locked_libstdcxx_duplicate_resolves_by_rpath_order(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stage_root = Path(raw)
            private_prefix = stage_root / "fenics-runtime" / "conda"
            runtime = private_prefix / LIBSTDCXX_RUNTIME_RELATIVE
            compiler_private = private_prefix / LIBSTDCXX_COMPILER_PRIVATE_RELATIVE
            runtime.parent.mkdir(parents=True)
            compiler_private.parent.mkdir(parents=True)
            runtime.write_bytes(b"runtime")
            compiler_private.write_bytes(b"compiler")
            candidates = {runtime.resolve(), compiler_private.resolve()}

            self.assertEqual(
                _resolve_bundled_provider(
                    stage_root,
                    private_prefix,
                    "lib/libhdf5_cpp.so.310.0.6",
                    "libstdc++.so.6",
                    candidates,
                    [runtime.parent.resolve(), compiler_private.parent.resolve()],
                ),
                runtime.resolve(),
            )
            self.assertEqual(
                _resolve_bundled_provider(
                    stage_root,
                    private_prefix,
                    LIBSTDCXX_COMPILER_PRIVATE_RELATIVE.as_posix(),
                    "libstdc++.so.6",
                    candidates,
                    [compiler_private.parent.resolve(), runtime.parent.resolve()],
                ),
                compiler_private.resolve(),
            )

    def test_exact_needed_filename_wins_over_soname_only_competitor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            stage_root = Path(raw)
            private_prefix = stage_root / "fenics-runtime" / "conda"
            library_dir = private_prefix / "lib"
            library_dir.mkdir(parents=True)
            exact = library_dir / "libgomp.so.1"
            soname_only = library_dir / "libomp.so"
            exact.write_bytes(b"gomp")
            soname_only.write_bytes(b"omp")

            self.assertEqual(
                _resolve_bundled_provider(
                    stage_root,
                    private_prefix,
                    "bin/ucx_perftest",
                    "libgomp.so.1",
                    {exact.resolve(), soname_only.resolve()},
                    [library_dir.resolve()],
                ),
                exact.resolve(),
            )


if __name__ == "__main__":
    unittest.main()
