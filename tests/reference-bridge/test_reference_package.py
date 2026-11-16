"""Tests for deterministic, exact-byte reference bridge packaging."""

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
import zipfile


ROOT = Path(__file__).resolve().parents[2]
PACKAGE_TOOL = ROOT / "consumer-kit" / "tools" / "package-reference.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReferencePackageTests(unittest.TestCase):
    def make_inputs(self, root: Path) -> dict[str, Path | str]:
        producer_revision = "a" * 40
        mock_revision = "b" * 40
        mex = root / "candidate.mex"
        mex.write_bytes(b"qualified-mex-bytes\x00\x01")
        mex_sha = digest(mex)

        runtime = root / "runtime-source"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "lib" / "python3.14").mkdir(parents=True)
        (runtime / "bin" / "python3.14").write_bytes(b"python-runtime-binary")
        (runtime / "lib" / "python3.14" / "os.py").write_text(
            "RUNTIME_SENTINEL = 'exact'\n", encoding="utf-8"
        )
        runtime_manifest = {
            "format_version": 1,
            "platform": "linux-x64",
            "profile": "core",
            "python_abi": "cp314",
            "producer": {"repository": "precsim/python-numpy-fenics", "revision": producer_revision},
        }
        (runtime / "runtime-manifest.json").write_text(
            json.dumps(runtime_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        receipt = root / "receipt.json"
        receipt.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "platform": "linux-x64",
                    "profile": "core",
                    "release_tag": "runtime-v0.1.0-rc2",
                    "release_revision": producer_revision,
                    "status": "pass",
                    "layers": [
                        {
                            "profile": "core",
                            "tag": "runtime-v0.1.0-rc2",
                            "producer_revision": producer_revision,
                            "artifact": "python-runtime-linux-x64-cp314-core.tar.gz",
                            "artifact_sha256": "c" * 64,
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        build = root / "build.json"
        build.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "target": "linux-x64",
                    "mex_sha256": mex_sha,
                    "toolchain": "test-cc",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        qualification = root / "qualification.json"
        qualification.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "platform": "linux-x64",
                    "profile": "core",
                    "python_abi": "cp314",
                    "source_revision": mock_revision,
                    "workflow_run_id": "test-run-123",
                    "mex_sha256": mex_sha,
                    "checks": {"octave_smoke": "pass", "relocation": "pass"},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        closure = root / "native-closure.json"
        closure.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "target": "linux-x64",
                    "checks": {
                        "exports": "mexFunction",
                        "load_time_dependencies": "system-only",
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        backend = root / "mock_backend.py"
        backend.write_text("def scalar(value):\n    return 2 * value + 1\n", encoding="utf-8")
        host = root / "mock_smoke.m"
        host.write_text("disp('reference_bridge_result=pass');\n", encoding="utf-8")
        license_file = root / "LICENSE.mock.txt"
        license_file.write_text("test redistribution license\n", encoding="utf-8")
        return {
            "mex": mex,
            "mex_sha": mex_sha,
            "runtime": runtime,
            "receipt": receipt,
            "build": build,
            "qualification": qualification,
            "closure": closure,
            "backend": backend,
            "host": host,
            "license": license_file,
            "producer_revision": producer_revision,
            "mock_revision": mock_revision,
        }

    def package_command(self, inputs: dict[str, Path | str], output: Path) -> list[str]:
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
            "v9.8.7-rc1",
            "--package-name",
            "reference-bridge-linux-x64-cp314-core-test",
            "--license",
            str(inputs["license"]),
            "--output",
            str(output),
        ]

    def extract_package(self, archive: Path, destination: Path) -> Path:
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(destination, filter="fully_trusted")
        return destination / "reference-bridge-linux-x64-cp314-core-test"

    def test_package_is_deterministic_exact_and_offline_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.make_inputs(root)
            first = root / "first.tar.gz"
            second = root / "second.tar.gz"

            first_run = subprocess.run(
                self.package_command(inputs, first), text=True, capture_output=True, check=False
            )
            self.assertEqual(first_run.returncode, 0, first_run.stdout + first_run.stderr)
            second_run = subprocess.run(
                self.package_command(inputs, second), text=True, capture_output=True, check=False
            )
            self.assertEqual(second_run.returncode, 0, second_run.stdout + second_run.stderr)
            self.assertEqual(first.read_bytes(), second.read_bytes())

            extracted = self.extract_package(first, root / "extracted with spaces")
            self.assertEqual((extracted / "bridge.mex").read_bytes(), Path(inputs["mex"]).read_bytes())
            self.assertEqual((extracted / "bridge.mexa64").read_bytes(), Path(inputs["mex"]).read_bytes())
            self.assertEqual(
                (extracted / "runtime" / "bin" / "python3.14").read_bytes(),
                (Path(inputs["runtime"]) / "bin" / "python3.14").read_bytes(),
            )
            self.assertEqual((extracted / "profile.txt").read_text(encoding="utf-8"), "core\n")
            self.assertTrue((extracted / "README-LOCAL-TEST.md").is_file())
            self.assertTrue((extracted / "evidence" / "runtime-receipt.json").is_file())
            self.assertTrue((extracted / "evidence" / "build-evidence.json").is_file())
            self.assertTrue((extracted / "evidence" / "qualification-evidence.json").is_file())
            self.assertTrue((extracted / "evidence" / "native-closure-evidence.json").is_file())
            self.assertTrue((extracted / "licenses" / "LICENSE.mock.txt").is_file())

            manifest = json.loads((extracted / "reference-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                set(manifest),
                {
                    "format_version",
                    "package",
                    "version",
                    "platform",
                    "profile",
                    "python_abi",
                    "producer",
                    "mock",
                    "qualification",
                    "build",
                    "native_closure",
                    "runtime",
                    "mex",
                    "local_host",
                    "files",
                },
            )
            self.assertEqual(manifest["version"], "v9.8.7-rc1")
            self.assertEqual(manifest["producer"]["revision"], inputs["producer_revision"])
            self.assertEqual(manifest["producer"]["archive_sha256s"], ["c" * 64])
            self.assertEqual(manifest["mock"]["source_revision"], inputs["mock_revision"])
            self.assertEqual(manifest["qualification"]["run_identity"], "github-actions:test-run-123")
            self.assertEqual(manifest["mex"]["path"], "bridge.mex")
            self.assertEqual(manifest["mex"]["sha256"], inputs["mex_sha"])
            self.assertEqual(manifest["runtime"]["path"], "runtime")
            self.assertEqual(manifest["local_host"]["launcher"], "run-local-test.sh")
            self.assertIsInstance(manifest["native_closure"]["summary"], str)
            self.assertTrue(all("kind" in entry and "mode" in entry for entry in manifest["files"]))

            verify = subprocess.run(
                [sys.executable, str(extracted / "verify-package.py")],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self.assertIn("reference_package_result=pass", verify.stdout)
            self.assertIn(f"qualified_mex_sha256={inputs['mex_sha']}", verify.stdout)

            (extracted / "python" / "mock_backend.py").write_text("tampered = True\n", encoding="utf-8")
            tampered = subprocess.run(
                [sys.executable, str(extracted / "verify-package.py")],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("reference_package_result=fail", tampered.stdout)

    def test_packaging_rejects_unqualified_mex_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.make_inputs(root)
            qualification = Path(inputs["qualification"])
            payload = json.loads(qualification.read_text(encoding="utf-8"))
            payload["mex_sha256"] = "f" * 64
            qualification.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = subprocess.run(
                self.package_command(inputs, root / "rejected.tar.gz"),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("qualification evidence does not bind the supplied qualified MEX SHA-256", result.stderr)

    def test_windows_zip_is_deterministic_and_uses_matlab_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.make_inputs(root)

            runtime_manifest_path = Path(inputs["runtime"]) / "runtime-manifest.json"
            runtime_manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
            runtime_manifest["platform"] = "win-x64"
            runtime_manifest_path.write_text(
                json.dumps(runtime_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

            receipt_path = Path(inputs["receipt"])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["platform"] = "win-x64"
            receipt["layers"][0]["artifact"] = "python-runtime-win-x64-cp314-core.zip"
            receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            build_path = Path(inputs["build"])
            build = json.loads(build_path.read_text(encoding="utf-8"))
            build["target"] = "win-x64"
            build_path.write_text(json.dumps(build, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            qualification_path = Path(inputs["qualification"])
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
            qualification["platform"] = "win-x64"
            qualification_path.write_text(
                json.dumps(qualification, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

            closure_path = Path(inputs["closure"])
            closure = json.loads(closure_path.read_text(encoding="utf-8"))
            closure["target"] = "win-x64"
            closure_path.write_text(json.dumps(closure, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            first = root / "first.zip"
            second = root / "second.zip"
            commands = []
            for output in (first, second):
                command = self.package_command(inputs, output)
                command[command.index("--platform") + 1] = "win-x64"
                command[command.index("--package-name") + 1] = "reference-bridge-win-x64-cp314-core-test"
                commands.append(command)
            for command in commands:
                result = subprocess.run(command, text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(first.read_bytes(), second.read_bytes())

            extract_root = root / "zip extracted with spaces"
            with zipfile.ZipFile(first) as archive:
                archive.extractall(extract_root)
            package_root = extract_root / "reference-bridge-win-x64-cp314-core-test"
            self.assertEqual((package_root / "bridge.mexw64").read_bytes(), Path(inputs["mex"]).read_bytes())
            verify = subprocess.run(
                [sys.executable, str(package_root / "verify-package.py")],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)
            self.assertIn("reference_package_result=pass", verify.stdout)

    def test_verifier_rejects_added_unattested_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.make_inputs(root)
            archive = root / "package.tar.gz"
            result = subprocess.run(
                self.package_command(inputs, archive), text=True, capture_output=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            extracted = self.extract_package(archive, root / "extract")
            (extracted / "unexpected.txt").write_text("not attested\n", encoding="utf-8")
            verify = subprocess.run(
                [sys.executable, str(extracted / "verify-package.py")],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(verify.returncode, 0)
            self.assertIn("manifest inventory differs", verify.stdout)

    @unittest.skipIf(os.name == "nt", "requires POSIX tar permission semantics")
    def test_tar_extraction_umask_preserves_strict_inventory_and_reports_mode_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.make_inputs(root)
            runtime = Path(inputs["runtime"])
            license_path = runtime / "LICENSES" / "OpenSSL.txt"
            license_path.parent.mkdir()
            license_path.write_text("OpenSSL redistribution notice\n", encoding="utf-8")
            license_path.chmod(0o664)

            manifest_path = runtime / "runtime-manifest.json"
            runtime_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = []
            for path in sorted(runtime.rglob("*")):
                if not path.is_file() or path == manifest_path:
                    continue
                files.append(
                    {
                        "path": path.relative_to(runtime).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": digest(path),
                        "mode": stat.S_IMODE(path.stat().st_mode),
                    }
                )
            runtime_manifest["files"] = files
            manifest_path.write_text(
                json.dumps(runtime_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

            archive = root / "package.tar.gz"
            packaged = subprocess.run(
                self.package_command(inputs, archive), text=True, capture_output=True, check=False
            )
            self.assertEqual(packaged.returncode, 0, packaged.stdout + packaged.stderr)
            with tarfile.open(archive, "r:gz") as bundle:
                member = bundle.getmember(
                    "reference-bridge-linux-x64-cp314-core-test/runtime/LICENSES/OpenSSL.txt"
                )
                self.assertEqual(member.mode & 0o7777, 0o664)

            extract_root = root / "gnu tar extraction"
            extract_root.mkdir()
            original_umask = os.umask(0o022)
            try:
                extracted = subprocess.run(
                    ["tar", "-xzf", str(archive), "-C", str(extract_root)],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(extracted.returncode, 0, extracted.stdout + extracted.stderr)
                package_root = extract_root / "reference-bridge-linux-x64-cp314-core-test"
                extracted_license = package_root / "runtime" / "LICENSES" / "OpenSSL.txt"
                self.assertEqual(stat.S_IMODE(extracted_license.stat().st_mode), 0o644)

                verify = subprocess.run(
                    [sys.executable, str(package_root / "verify-package.py")],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

                extracted_license.chmod(0o755)
                bad_mode = subprocess.run(
                    [sys.executable, str(package_root / "verify-package.py")],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(bad_mode.returncode, 0)
                self.assertIn("runtime/LICENSES/OpenSSL.txt", bad_mode.stdout)
                self.assertIn("expected=", bad_mode.stdout)
                self.assertIn("observed=", bad_mode.stdout)
            finally:
                os.umask(original_umask)


if __name__ == "__main__":
    unittest.main()
