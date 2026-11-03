"""Fail-closed tests for Linux conda package prefix substitutions."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest import mock

from scripts.linux import package_fenics_overlay as overlay

from scripts.linux.package_fenics_overlay import (
    cache_path_to_installed_path,
    handoff_forbidden_destination,
    replace_conda_prefix,
    staged_locked_package_relative,
    staged_runtime_relative,
    target_wheel_owned_path,
)


class CondaPrefixRelocationTests(unittest.TestCase):
    def test_noarch_cache_paths_reconstruct_cp314_prefix(self) -> None:
        link = {"noarch": {"type": "python"}}
        self.assertEqual(
            cache_path_to_installed_path(PurePosixPath("site-packages/dolfinx/a.so"), link),
            PurePosixPath("lib/python3.14/site-packages/dolfinx/a.so"),
        )
        self.assertEqual(
            cache_path_to_installed_path(PurePosixPath("python-scripts/ffcx"), link),
            PurePosixPath("bin/ffcx"),
        )
        self.assertEqual(
            cache_path_to_installed_path(PurePosixPath("site-packages/a.py"), {}),
            PurePosixPath("site-packages/a.py"),
        )

    def test_legacy_stable_abi_site_paths_stage_under_cp314(self) -> None:
        self.assertEqual(
            staged_runtime_relative(PurePosixPath("lib/python3.12/site-packages/dolfinx/a.py")),
            PurePosixPath("lib/python3.14/site-packages/dolfinx/a.py"),
        )
        self.assertEqual(
            staged_runtime_relative(PurePosixPath("lib/libdolfinx.so.0.11.0")),
            PurePosixPath("lib/libdolfinx.so.0.11.0"),
        )

    def test_target_specific_cp312_python_payload_is_never_relabelled(self) -> None:
        relative = PurePosixPath("lib/python3.12/site-packages/mpi4py/MPI.cpython-312-x86_64-linux-gnu.so")
        linux_record = {"name": "mpi4py", "subdir": "linux-64"}
        self.assertEqual(
            staged_locked_package_relative(relative, linux_record, native_rebuild_active=False),
            relative,
        )
        with self.assertRaisesRegex(SystemExit, "explicit cp314 replacement"):
            staged_locked_package_relative(relative, linux_record, native_rebuild_active=True)
        self.assertEqual(
            staged_locked_package_relative(
                PurePosixPath("lib/python3.12/site-packages/ufl/__init__.py"),
                {"name": "fenics-ufl", "subdir": "noarch"},
                native_rebuild_active=False,
            ),
            PurePosixPath("lib/python3.14/site-packages/ufl/__init__.py"),
        )

    def test_cp314_handoff_keeps_cpython_numpy_boundary_closed(self) -> None:
        for relative in (
            "bin/python3",
            "bin/python3.14-config",
            "include/python3.14/Python.h",
            "lib/libpython3.14.so.1.0",
            "lib/python3.14/site-packages/numpy/core.so",
        ):
            with self.subTest(relative=relative):
                self.assertTrue(handoff_forbidden_destination(PurePosixPath(relative)))
        self.assertFalse(
            handoff_forbidden_destination(PurePosixPath("lib/python3.14/site-packages/dolfinx/cpp.abi3.so"))
        )

    def test_cp314_rebuilt_wheel_paths_are_owner_scoped(self) -> None:
        self.assertTrue(
            target_wheel_owned_path(
                "fenics-dolfinx",
                PurePosixPath("lib/python3.14/site-packages/dolfinx/cpp.cpython-314-x86_64-linux-gnu.so"),
            )
        )
        self.assertFalse(
            target_wheel_owned_path(
                "fenics-dolfinx",
                PurePosixPath("lib/python3.14/site-packages/petsc4py/PETSc.abi3.so"),
            )
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

    def test_rebuilt_dolfinx_extension_strips_only_embedded_build_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            extension = stage / (
                "fenics-runtime/conda/lib/python3.14/site-packages/dolfinx/"
                "cpp.cpython-314-x86_64-linux-gnu.so"
            )
            extension.parent.mkdir(parents=True)
            prefix = Path("/scratch/very-long-qualified-source-prefix-name")
            original = b"\x7fELF" + str(prefix).encode() + b"/include\0unrelated\0"
            extension.write_bytes(original)
            dynamic = (["libdolfinx.so"], None, ["$ORIGIN/../../../../lib"], True)
            with mock.patch.object(overlay, "dynamic", return_value=dynamic):
                evidence = overlay.relocate_rebuilt_dolfinx_extension_prefix(
                    stage, prefix
                )
            self.assertEqual(evidence["occurrences"], 1)
            self.assertEqual(evidence["package"], "fenics-dolfinx")
            self.assertEqual(evidence["placeholder_bytes"], len(str(prefix).encode()))
            self.assertNotIn("cache_sha256", evidence)
            self.assertEqual(len(extension.read_bytes()), len(original))
            self.assertNotIn(str(prefix).encode(), extension.read_bytes())
            self.assertIn(b"unrelated\0", extension.read_bytes())
            with mock.patch.object(overlay, "dynamic", return_value=dynamic):
                self.assertIsNone(
                    overlay.relocate_rebuilt_dolfinx_extension_prefix(
                        stage, prefix
                    )
                )

    def test_dolfinx_relocation_rejects_active_absolute_rpath(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            extension = stage / (
                "fenics-runtime/conda/lib/python3.14/site-packages/dolfinx/"
                "cpp.cpython-314-x86_64-linux-gnu.so"
            )
            extension.parent.mkdir(parents=True)
            prefix = Path("/scratch/very-long-qualified-source-prefix-name")
            extension.write_bytes(b"\x7fELF" + str(prefix).encode() + b"\0")
            with mock.patch.object(
                overlay,
                "dynamic",
                return_value=([], None, [str(prefix) + "/lib"], True),
            ):
                with self.assertRaisesRegex(SystemExit, "dynamic RPATH"):
                    overlay.relocate_rebuilt_dolfinx_extension_prefix(
                        stage, prefix
                    )


if __name__ == "__main__":
    unittest.main()
