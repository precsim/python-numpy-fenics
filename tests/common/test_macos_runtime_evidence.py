"""Host-independent regression tests for macOS runtime evidence helpers."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load("audit_macos_runtime", "scripts/macos/audit_macos_runtime.py")
verify = load("verify_macos_runtime_evidence", "scripts/macos/verify-runtime-evidence.py")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MacOSRuntimeAuditTests(unittest.TestCase):
    def test_version_key_treats_trailing_zero_components_equally(self) -> None:
        self.assertEqual(audit.version_key("14.0"), audit.version_key("14.0.0"))
        self.assertGreater(audit.version_key("14.1"), audit.version_key("14.0.0"))

    def test_load_command_parser_excludes_dylib_id(self) -> None:
        parsed = audit.parse_load_commands(
            """
Load command 0
          cmd LC_ID_DYLIB
         name @rpath/libexample.dylib (offset 24)
Load command 1
          cmd LC_LOAD_DYLIB
         name /usr/lib/libSystem.B.dylib (offset 24)
Load command 2
          cmd LC_LOAD_WEAK_DYLIB
         name @loader_path/libprivate.dylib (offset 24)
Load command 3
          cmd LC_RPATH
         path @loader_path/../lib (offset 12)
Load command 4
          cmd LC_BUILD_VERSION
        minos 14.0
"""
        )
        self.assertEqual(parsed["dylib_id"], "@rpath/libexample.dylib")
        self.assertEqual(
            parsed["dependencies"],
            ["/usr/lib/libSystem.B.dylib", "@loader_path/libprivate.dylib"],
        )
        self.assertEqual(parsed["rpaths"], ["@loader_path/../lib"])
        self.assertEqual(parsed["minos"], ["14.0"])

    def test_full_runtime_audit_resolves_bundled_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            python = runtime / "Python.framework/Versions/3.12/bin/python3.12"
            provider = runtime / "Python.framework/Versions/3.12/lib/libprivate.dylib"
            python.parent.mkdir(parents=True)
            provider.parent.mkdir(parents=True)
            python.write_bytes(b"python-mach-o")
            provider.write_bytes(b"provider-mach-o")
            output = runtime / "core-native-census-macos.json"

            def fake_run(*args: str) -> str:
                if args[0] == "file":
                    return "Mach-O 64-bit arm64\n"
                if args[0] == "lipo":
                    return "arm64\n"
                if args[0] == "otool":
                    path = Path(args[-1])
                    dependency = (
                        "@loader_path/../lib/libprivate.dylib"
                        if path.name == "python3.12"
                        else "/usr/lib/libSystem.B.dylib"
                    )
                    return f"""
Load command 0
          cmd LC_LOAD_DYLIB
         name {dependency} (offset 24)
Load command 1
          cmd LC_BUILD_VERSION
        minos 14.0
"""
                raise AssertionError(args)

            with mock.patch.object(audit, "run", side_effect=fake_run):
                payload = audit.audit(runtime, output, "arm64", True, "14.0")
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["macho_count"], 2)
            self.assertEqual(payload["bundled_dependency_count"], 1)
            self.assertEqual(payload["system_dependency_count"], 1)
            python_entry = next(item for item in payload["mach_o"] if item["path"].endswith("python3.12"))
            self.assertEqual(
                python_entry["dependency_resolutions"][0]["provider"],
                "Python.framework/Versions/3.12/lib/libprivate.dylib",
            )

    def test_full_runtime_audit_rejects_minos_above_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            python = runtime / "Python.framework/Versions/3.12/bin/python3.12"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"python-mach-o")

            def fake_run(*args: str) -> str:
                if args[0] == "file":
                    return "Mach-O 64-bit arm64\n"
                if args[0] == "lipo":
                    return "arm64\n"
                if args[0] == "otool":
                    return """
Load command 0
          cmd LC_BUILD_VERSION
        minos 14.1
"""
                raise AssertionError(args)

            with mock.patch.object(audit, "run", side_effect=fake_run):
                with self.assertRaisesRegex(SystemExit, "raises macOS floor above 14.0"):
                    audit.audit(runtime, runtime / "census.json", "arm64", True, "14.0")

    def test_payload_audit_accepts_explicit_runtime_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            site = runtime / "Python.framework/Versions/3.12/lib/python3.12/site-packages"
            extension = site / "numpy/_core/example.so"
            executable = runtime / "Python.framework/Versions/3.12/bin/python3.12"
            extension.parent.mkdir(parents=True)
            executable.parent.mkdir(parents=True)
            extension.write_bytes(b"numpy-mach-o")
            executable.write_bytes(b"python-mach-o")

            def fake_run(*args: str) -> str:
                if args[0] == "file":
                    return "Mach-O 64-bit arm64\n"
                if args[0] == "lipo":
                    return "arm64\n"
                if args[0] == "otool":
                    return """
Load command 0
          cmd LC_LOAD_DYLIB
         name /System/Library/Frameworks/Accelerate.framework/Versions/A/Accelerate (offset 24)
Load command 1
          cmd LC_BUILD_VERSION
        minos 14.0
"""
                raise AssertionError(args)

            with mock.patch.object(audit, "run", side_effect=fake_run):
                payload = audit.audit(
                    site,
                    root / "payload-census.json",
                    "arm64",
                    True,
                    "14.0",
                    executable,
                )
            self.assertEqual(payload["macho_count"], 1)
            self.assertEqual(payload["system_dependency_count"], 1)


class MacOSRuntimeEvidenceTests(unittest.TestCase):
    def make_runtime(self, root: Path) -> Path:
        runtime = root / "runtime"
        binary = runtime / "Python.framework/Versions/3.12/bin/python3.12"
        provider = runtime / "Python.framework/Versions/3.12/lib/libprivate.dylib"
        tcl_provider = runtime / "Python.framework/Versions/3.12/lib/libtcl8.6.dylib"
        tk_provider = runtime / "Python.framework/Versions/3.12/lib/libtk8.6.dylib"
        binary.parent.mkdir(parents=True)
        provider.parent.mkdir(parents=True)
        binary.write_bytes(b"python-mach-o")
        provider.write_bytes(b"provider-mach-o")
        tcl_provider.write_bytes(b"tcl-provider")
        tk_provider.write_bytes(b"tk-provider")
        census = {
            "format_version": 2,
            "target_arch": "arm64",
            "root": ".",
            "status": "pass",
            "max_allowed_minos": "14.0",
            "macho_count": 2,
            "bundled_dependency_count": 1,
            "system_dependency_count": 1,
            "target_measured_max_minos": "14.0",
            "mach_o": [
                {
                    "path": "Python.framework/Versions/3.12/bin/python3.12",
                    "sha256": digest(binary),
                    "architectures": ["arm64"],
                    "target_arch": "arm64",
                    "dependencies": ["@loader_path/../lib/libprivate.dylib"],
                    "dependency_resolutions": [
                        {
                            "name": "@loader_path/../lib/libprivate.dylib",
                            "resolution": "bundled",
                            "provider": "Python.framework/Versions/3.12/lib/libprivate.dylib",
                            "provider_sha256": digest(provider),
                        }
                    ],
                    "rpaths": [],
                    "dylib_id": None,
                    "minos": "14.0",
                    "file": "Mach-O",
                },
                {
                    "path": "Python.framework/Versions/3.12/lib/libprivate.dylib",
                    "sha256": digest(provider),
                    "architectures": ["arm64"],
                    "target_arch": "arm64",
                    "dependencies": ["/usr/lib/libSystem.B.dylib"],
                    "dependency_resolutions": [
                        {"name": "/usr/lib/libSystem.B.dylib", "resolution": "system"}
                    ],
                    "rpaths": [],
                    "dylib_id": "@rpath/libprivate.dylib",
                    "minos": "14.0",
                    "file": "Mach-O",
                },
            ],
        }
        census_path = runtime / "core-native-census-macos.json"
        census_path.write_text(json.dumps(census, sort_keys=True), encoding="utf-8")
        relocation = {
            "format_version": 1,
            "status": "pass",
            "profile": "core",
            "architecture": "arm64",
            "python_prefix": "Python.framework/Versions/3.12",
            "tcl": {
                "api_version": "8.6",
                "runtime_version": "8.6.16",
                "script_root": "tcl/tcl8.6",
                "provider": "Python.framework/Versions/3.12/lib/libtcl8.6.dylib",
                "provider_sha256": digest(tcl_provider),
            },
            "tk": {
                "api_version": "8.6",
                "runtime_version": "8.6.16",
                "script_root": "tcl/tk8.6",
                "provider": "Python.framework/Versions/3.12/lib/libtk8.6.dylib",
                "provider_sha256": digest(tk_provider),
            },
        }
        relocation_path = runtime / "core-relocation-qualification-macos.json"
        relocation_path.write_text(json.dumps(relocation, sort_keys=True), encoding="utf-8")
        provenance = {
            "format_version": 1,
            "runtime_profile": "core",
            "python": {"architecture": "arm64"},
            "tcl_tk": {
                "tcl_api_version": "8.6",
                "tk_api_version": "8.6",
                "tcl_runtime_version": "8.6.16",
                "tk_runtime_version": "8.6.16",
                "staged_hashes": {
                    "tcl": digest(tcl_provider),
                    "tk": digest(tk_provider),
                },
            },
            "native_closure": {
                "status": "pass",
                "census_path": census_path.name,
                "census_sha256": digest(census_path),
                "macho_count": 2,
                "target_measured_max_minos": "14.0",
            },
            "relocation_qualification": {
                "status": "pass",
                "evidence_path": relocation_path.name,
                "evidence_sha256": digest(relocation_path),
            },
        }
        (runtime / "build-provenance-macos.json").write_text(
            json.dumps(provenance, sort_keys=True), encoding="utf-8"
        )
        return runtime

    def test_verifier_binds_census_to_runtime_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self.make_runtime(Path(temporary))
            result = verify.verify(runtime, "core", "arm64")
            self.assertEqual(result["target_measured_max_minos"], "14.0")
            (runtime / "Python.framework/Versions/3.12/lib/libprivate.dylib").write_bytes(b"changed")
            with self.assertRaisesRegex(SystemExit, "hash mismatch"):
                verify.verify(runtime, "core", "arm64")


if __name__ == "__main__":
    unittest.main()
