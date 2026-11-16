"""Focused unit tests for the public reference bridge Python backend."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
BACKEND_SOURCE = ROOT / "reference-bridge" / "python" / "mock_backend.py"


class _Vector(list[float]):
    def __mul__(self, other: object) -> "_Vector":
        if not isinstance(other, _Vector):
            return NotImplemented
        return _Vector(left * right for left, right in zip(self, other, strict=True))


class MockBackendTests(unittest.TestCase):
    def _make_package(
        self,
        root: Path,
        *,
        profile: str,
        windows_fenics_manifest: bool = False,
    ) -> tuple[types.ModuleType, Path, Path]:
        package_root = root / "reference package with spaces"
        backend_dir = package_root / "python"
        runtime_root = package_root / "runtime"
        backend_dir.mkdir(parents=True)
        runtime_root.mkdir()
        shutil.copy2(BACKEND_SOURCE, backend_dir / "mock_backend.py")
        (package_root / "profile.txt").write_text(profile + "\n", encoding="utf-8")

        base_profile = "numpy" if profile == "fenics" else profile
        (runtime_root / "runtime-manifest.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "profile": base_profile,
                    "python_abi": "cp314",
                    "producer": {"repository": "precsim/python-numpy-fenics"},
                }
            ),
            encoding="utf-8",
        )
        (runtime_root / "RUNTIME_PROFILE").write_text(base_profile + "\n", encoding="utf-8")

        if profile == "fenics":
            overlay: dict[str, object] = {
                "format_version": 1,
                "profile": "fenics",
                "python_abi": "cp314",
                "producer": {"repository": "precsim/python-numpy-fenics"},
                "numpy": {"version": "2.5.2"},
            }
            if windows_fenics_manifest:
                overlay["python"] = {"version": "3.14.10"}
            else:
                overlay["consumer_python"] = "3.14.10"
            (runtime_root / "fenics-embed-manifest.json").write_text(
                json.dumps(overlay), encoding="utf-8"
            )

        module_name = f"mock_backend_test_{id(root)}_{profile}_{int(windows_fenics_manifest)}"
        spec = importlib.util.spec_from_file_location(module_name, backend_dir / "mock_backend.py")
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, package_root, runtime_root

    def _configure(self, module: types.ModuleType, package_root: Path, runtime_root: Path, profile: str) -> str:
        flags = types.SimpleNamespace(
            isolated=1,
            ignore_environment=1,
            no_site=1,
            no_user_site=1,
            safe_path=True,
        )
        controlled_path = [
            str(runtime_root / "lib" / "python3.14"),
            str(package_root / "python"),
        ]
        with (
            mock.patch.object(module.platform, "python_version", return_value="3.14.10"),
            mock.patch.object(module.sys, "version_info", (3, 14, 10)),
            mock.patch.object(module.sys, "flags", flags),
            mock.patch.object(module.sys, "dont_write_bytecode", True),
            mock.patch.object(module.sys, "path", controlled_path),
        ):
            return module._configure(str(package_root), str(runtime_root), profile)

    def test_scalar_is_strict_and_recovers_after_backend_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            module, _, _ = self._make_package(Path(tmp), profile="core")
            self.assertEqual(module.scalar(5.0), 11.0)
            self.assertEqual(module.scalar(10.0), 21.0)
            with self.assertRaisesRegex(TypeError, "finite Python float"):
                module.scalar(5)
            with self.assertRaisesRegex(ValueError, "non-finite"):
                module.scalar(sys.float_info.max)
            self.assertEqual(module.scalar(5.0), 11.0)

    def test_core_configuration_and_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            module, package_root, runtime_root = self._make_package(Path(tmp), profile="core")
            self.assertEqual(self._configure(module, package_root, runtime_root, "core"), "core")

            with mock.patch.object(module.platform, "python_version", return_value="3.14.10"), mock.patch.object(
                module.sys, "version_info", (3, 14, 10)
            ):
                self.assertEqual(
                    module.runtime_info(),
                    ("3.14.10", "cp314", str(runtime_root.resolve()), "core"),
                )

            with self.assertRaisesRegex(module.ProfileUnavailableError, "unavailable"):
                module.numpy_sum_squares(5)
            with self.assertRaisesRegex(module.ProfileUnavailableError, "unavailable"):
                module.fenics_poisson()

    def test_numpy_operation_is_lazy_versioned_and_runtime_owned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            module, package_root, runtime_root = self._make_package(Path(tmp), profile="numpy")
            self._configure(module, package_root, runtime_root, "numpy")

            fake_numpy = types.ModuleType("numpy")
            fake_numpy.__version__ = "2.5.2"
            runtime_numpy = runtime_root / "lib" / "python3.14" / "site-packages" / "numpy" / "__init__.py"
            runtime_numpy.parent.mkdir(parents=True)
            runtime_numpy.write_text("# fake runtime NumPy\n", encoding="utf-8")
            fake_numpy.__file__ = str(runtime_numpy)
            fake_numpy.float64 = object()
            fake_numpy.arange = lambda start, stop, dtype=None: _Vector(float(value) for value in range(start, stop))
            fake_numpy.sum = lambda values, dtype=None: sum(values)
            with mock.patch.dict(sys.modules, {"numpy": fake_numpy}):
                self.assertEqual(module.numpy_sum_squares(5), 55.0)

            ambient_numpy = package_root.parent / "ambient" / "numpy" / "__init__.py"
            ambient_numpy.parent.mkdir(parents=True)
            ambient_numpy.write_text("# fake ambient NumPy\n", encoding="utf-8")
            fake_numpy.__file__ = str(ambient_numpy)
            with mock.patch.dict(sys.modules, {"numpy": fake_numpy}):
                with self.assertRaisesRegex(RuntimeError, "outside the verified runtime root"):
                    module.numpy_sum_squares(5)

    def test_fenics_configuration_accepts_linux_macos_manifest_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            module, package_root, runtime_root = self._make_package(Path(tmp), profile="fenics")
            self.assertEqual(self._configure(module, package_root, runtime_root, "fenics"), "fenics")

    def test_fenics_configuration_accepts_windows_manifest_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            module, package_root, runtime_root = self._make_package(
                Path(tmp), profile="fenics", windows_fenics_manifest=True
            )
            self.assertEqual(self._configure(module, package_root, runtime_root, "fenics"), "fenics")

    def test_fenics_configuration_rejects_wrong_numpy_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            module, package_root, runtime_root = self._make_package(Path(tmp), profile="fenics")
            manifest_path = runtime_root / "fenics-embed-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["numpy"]["version"] = "0.0"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "required NumPy 2.5.2 base"):
                self._configure(module, package_root, runtime_root, "fenics")

    def test_configuration_rejects_ambient_import_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            module, package_root, runtime_root = self._make_package(Path(tmp), profile="core")
            flags = types.SimpleNamespace(
                isolated=1,
                ignore_environment=1,
                no_site=1,
                no_user_site=1,
                safe_path=True,
            )
            with (
                mock.patch.object(module.platform, "python_version", return_value="3.14.10"),
                mock.patch.object(module.sys, "version_info", (3, 14, 10)),
                mock.patch.object(module.sys, "flags", flags),
                mock.patch.object(module.sys, "dont_write_bytecode", True),
                mock.patch.object(module.sys, "path", [str(package_root.parent / "ambient")]),
            ):
                with self.assertRaisesRegex(RuntimeError, "sys.path escaped the package"):
                    module._configure(str(package_root), str(runtime_root), "core")


if __name__ == "__main__":
    unittest.main()
