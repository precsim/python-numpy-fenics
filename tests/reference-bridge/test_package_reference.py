"""Acceptance tests for reference bridge package provenance and corruption checks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "consumer-kit" / "tools"
PACKAGE_TOOL = TOOLS / "package-reference.py"


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class PackageReferenceAcceptanceTests(unittest.TestCase):
    def make_inputs(self, root: Path, *, with_symlink: bool = False) -> dict[str, Path | str | bool]:
        producer_revision = "a" * 40
        mock_revision = "b" * 40

        mex = root / "candidate.mex"
        mex.write_bytes(b"already-qualified-mex\x00")
        mex_sha = digest_file(mex)

        runtime = root / "staged runtime"
        payload = runtime / "lib" / "python.txt"
        payload.parent.mkdir(parents=True)
        payload.write_bytes(b"qualified runtime bytes\n")
        if os.name != "nt":
            payload.chmod(0o755)

        files: list[dict[str, object]] = [
            {
                "path": "lib/python.txt",
                "bytes": payload.stat().st_size,
                "sha256": digest_file(payload),
                "mode": stat.S_IMODE(payload.stat().st_mode),
            }
        ]
        symlink_created = False
        if with_symlink:
            link = runtime / "lib" / "python-current"
            try:
                link.symlink_to("python.txt")
            except OSError:
                pass
            else:
                symlink_created = True
                files.append(
                    {
                        "path": "lib/python-current",
                        "kind": "symlink",
                        "target": "python.txt",
                        "mode": stat.S_IMODE(link.lstat().st_mode),
                    }
                )

        runtime_manifest = {
            "format_version": 1,
            "platform": "linux-x64",
            "profile": "core",
            "python_abi": "cp314",
            "producer": {"repository": "precsim/python-numpy-fenics", "revision": producer_revision},
            "files": files,
        }
        runtime_manifest_path = runtime / "runtime-manifest.json"
        write_json(runtime_manifest_path, runtime_manifest)
        runtime_manifest_sha = digest_file(runtime_manifest_path)

        receipt = root / "staged-runtime-evidence.json"
        write_json(
            receipt,
            {
                "format_version": 1,
                "producer_repository": "precsim/python-numpy-fenics",
                "platform": "linux-x64",
                "profile": "core",
                "python_abi": "cp314",
                "release_tag": "runtime-v0.1.0-rc1",
                "release_revision": producer_revision,
                "status": "pass",
                "layers": [
                    {
                        "profile": "core",
                        "producer_revision": producer_revision,
                        "artifact": "python-runtime-linux-x64-cp314-core.tar.gz",
                        "artifact_sha256": "c" * 64,
                        "manifest": {"name": "runtime-manifest.json", "sha256": runtime_manifest_sha},
                    }
                ],
            },
        )

        build = root / "build.json"
        write_json(build, {"format_version": 1, "target": "linux-x64", "mex_sha256": mex_sha})
        qualification = root / "qualification.json"
        write_json(
            qualification,
            {
                "format_version": 1,
                "platform": "linux-x64",
                "profile": "core",
                "python_abi": "cp314",
                "source_revision": mock_revision,
                "workflow_run_id": "4242",
                "mex_sha256": mex_sha,
            },
        )
        closure = root / "closure.json"
        write_json(
            closure,
            {
                "format_version": 1,
                "target": "linux-x64",
                "status": "pass",
                "summary": "exports=mexFunction; load_time_dependencies=system-only",
            },
        )
        backend = root / "mock_backend.py"
        backend.write_text("def scalar(value):\n    return 2 * value + 1\n", encoding="utf-8")
        host = root / "mock_smoke.m"
        host.write_text("disp('reference_bridge_result=pass');\n", encoding="utf-8")
        license_file = root / "LICENSE.txt"
        license_file.write_text("test license\n", encoding="utf-8")
        return {
            "producer_revision": producer_revision,
            "mock_revision": mock_revision,
            "mex": mex,
            "mex_sha": mex_sha,
            "runtime": runtime,
            "runtime_payload": payload,
            "runtime_manifest": runtime_manifest_path,
            "receipt": receipt,
            "build": build,
            "qualification": qualification,
            "closure": closure,
            "backend": backend,
            "host": host,
            "license": license_file,
            "symlink_created": symlink_created,
        }

    def package_command(self, inputs: dict[str, Path | str | bool], output: Path) -> list[str]:
        return [
            sys.executable,
            str(PACKAGE_TOOL),
            "--mex",
            str(inputs["mex"]),
            "--runtime",
            str(inputs["runtime"]),
            "--runtime-receipt",
            str(inputs["receipt"]),
            "--build-evidence",
            str(inputs["build"]),
            "--qualification-evidence",
            str(inputs["qualification"]),
            "--native-closure-evidence",
            str(inputs["closure"]),
            "--backend",
            str(inputs["backend"]),
            "--host-fixture",
            str(inputs["host"]),
            "--platform",
            "linux-x64",
            "--profile",
            "core",
            "--producer-revision",
            str(inputs["producer_revision"]),
            "--mock-revision",
            str(inputs["mock_revision"]),
            "--version",
            "v1.2.3-rc1",
            "--package-name",
            "reference-bridge-linux-x64-cp314-core-test",
            "--license",
            str(inputs["license"]),
            "--output",
            str(output),
        ]

    def package(self, inputs: dict[str, Path | str | bool], output: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(self.package_command(inputs, output), text=True, capture_output=True, check=False)

    def extract(self, archive: Path, destination: Path) -> Path:
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(destination, filter="fully_trusted")
        return destination / "reference-bridge-linux-x64-cp314-core-test"

    def test_packager_rejects_runtime_payload_changed_after_producer_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs = self.make_inputs(root)
            Path(inputs["runtime_payload"]).write_bytes(b"mutated after staging\n")
            result = self.package(inputs, root / "rejected.tar.gz")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("staged runtime file differs from embedded producer manifest", result.stderr)

    def test_packager_rejects_runtime_manifest_not_bound_by_stage_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs = self.make_inputs(root)
            receipt_path = Path(inputs["receipt"])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["layers"][0]["manifest"]["sha256"] = "f" * 64
            write_json(receipt_path, receipt)
            result = self.package(inputs, root / "rejected.tar.gz")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("runtime-manifest.json is not bound by the runtime receipt", result.stderr)

    def test_package_inventory_preserves_modes_symlinks_and_mex_alias_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            inputs = self.make_inputs(root, with_symlink=True)
            archive = root / "reference.tar.gz"
            result = self.package(inputs, archive)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            package_root = self.extract(archive, root / "extracted with spaces")

            self.assertEqual((package_root / "bridge.mex").read_bytes(), (package_root / "bridge.mexa64").read_bytes())
            manifest = json.loads((package_root / "reference-manifest.json").read_text(encoding="utf-8"))
            inventory = {entry["path"]: entry for entry in manifest["files"]}
            payload_record = inventory["runtime/lib/python.txt"]
            self.assertEqual(payload_record["sha256"], digest_file(Path(inputs["runtime_payload"])))
            self.assertEqual(payload_record["mode"], stat.S_IMODE(Path(inputs["runtime_payload"]).stat().st_mode))
            if inputs["symlink_created"]:
                link_record = inventory["runtime/lib/python-current"]
                self.assertEqual(link_record["kind"], "symlink")
                self.assertEqual(link_record["target"], "python.txt")
                self.assertIn("mode", link_record)

            verifier = subprocess.run(
                [sys.executable, str(package_root / "verify-package.py")],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(verifier.returncode, 0, verifier.stdout + verifier.stderr)
            self.assertIn("reference_package_result=pass", verifier.stdout)

            (package_root / "bridge.mexa64").write_bytes(b"corrupt alias\n")
            corrupted = subprocess.run(
                [sys.executable, str(package_root / "verify-package.py")],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(corrupted.returncode, 0)
            self.assertIn("reference_package_result=fail", corrupted.stdout)

    def test_local_launchers_verify_package_before_running_octave_and_do_not_acquire(self) -> None:
        shell = (TOOLS / "run-local-test.sh").read_text(encoding="utf-8")
        powershell = (TOOLS / "run-local-test.ps1").read_text(encoding="utf-8")
        self.assertLess(shell.index("verify-package.py"), shell.index("octave_cmd" , shell.index("verify-package.py")))
        self.assertLess(powershell.index("verify-package.py"), powershell.index("$Octave", powershell.index("verify-package.py")))
        forbidden = ("curl ", "wget ", "Invoke-WebRequest", "pip install", "conda ", "gcc ", "clang ", " cl ")
        for launcher in (shell, powershell):
            lowered = launcher.lower()
            for token in forbidden:
                self.assertNotIn(token.lower(), lowered)

    def test_macos_fenics_launcher_unifies_verified_libomp_before_octave(self) -> None:
        shell = (TOOLS / "run-local-test.sh").read_text(encoding="utf-8")
        macos = (TOOLS / "test-octave-macos.sh").read_text(encoding="utf-8")
        provider = "runtime/fenics-runtime/conda/lib/libomp.dylib"

        self.assertIn(provider, shell)
        self.assertIn("reference-bridge-libomp.XXXXXX", shell)
        self.assertIn('ln -s "$omp_provider" "$omp_override/libomp.dylib"', shell)
        self.assertIn('DYLD_LIBRARY_PATH="$omp_override"', shell)
        self.assertLess(shell.index("verify-package.py"), shell.index("DYLD_LIBRARY_PATH"))

        self.assertIn(provider, macos)
        self.assertIn('ln -s "$omp_provider" "$omp_override/libomp.dylib"', macos)
        self.assertIn('export DYLD_LIBRARY_PATH="$omp_override"', macos)
        self.assertNotIn("KMP_DUPLICATE_LIB_OK", shell)
        self.assertNotIn("KMP_DUPLICATE_LIB_OK", macos)


if __name__ == "__main__":
    unittest.main()
