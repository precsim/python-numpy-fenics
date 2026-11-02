from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from scripts.linux.prepare_fenics_python_builder import (
    BuilderInputError,
    EXPECTED_ACTIVE_DEPENDENCIES,
    EXPECTED_MODULES,
    EXPECTED_ORDER,
    active_dependency_names,
    inspect_wheel,
    safe_extract_wheels,
    tree_evidence,
    validate_base_runtime,
    validate_lock,
)


ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "locks" / "linux" / "linux-x86_64-fenics-python-builder-wheels.json"
NUMPY_SOURCES = ROOT / "locks" / "linux" / "linux-glibc217-numpy-sources.json"
FENICS_LOCK = ROOT / "locks" / "linux" / "linux-x86_64-fenics-package-lock.json"


def write_wheel(path: Path, name: str, version: str, requires: list[str], extra_members: dict[str, bytes] | None = None) -> str:
    dist = name.replace("-", "_")
    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
    ]
    metadata.extend(f"Requires-Dist: {value}" for value in requires)
    metadata.append("")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist}-{version}.dist-info/METADATA", "\n".join(metadata).encode())
        archive.writestr(f"{dist}/__init__.py", b"")
        for member, payload in (extra_members or {}).items():
            archive.writestr(member, payload)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LinuxFenicsPythonBuilderTests(unittest.TestCase):
    def test_checked_in_wheel_lock_reuses_existing_public_pins(self) -> None:
        lock, records = validate_lock(LOCK, NUMPY_SOURCES, FENICS_LOCK)
        self.assertEqual(tuple(records), EXPECTED_ORDER)
        self.assertEqual(lock["modules"], EXPECTED_MODULES)
        self.assertEqual(records["pip"]["pin_source"], "linux-glibc217-numpy-sources.json#python_build_wheels:pip")
        self.assertEqual(records["packaging"]["pin_source"], "linux-glibc217-numpy-sources.json#python_build_wheels:packaging")
        self.assertEqual(
            records["pyproject-metadata"]["pin_source"],
            "linux-glibc217-numpy-sources.json#python_build_wheels:pyproject-metadata",
        )
        self.assertEqual(records["scikit-build-core"]["selected_extras"], ["pyproject"])
        self.assertEqual(records["scikit-build-core"]["extra_compatibility_dependencies"], ["pyproject-metadata"])
        self.assertEqual(records["wheel"]["version"], "0.48.0")
        self.assertFalse(lock["policy"]["dependency_resolution"])
        self.assertFalse(lock["policy"]["index_access"])

    def test_python312_dependency_marker_reduction_is_fail_closed(self) -> None:
        requirements = [
            "packaging>=23.2",
            "pathspec>=0.10.1",
            'exceptiongroup>=1.0; python_version < "3.11"',
            'pytest>=7; extra == "test"',
        ]
        self.assertEqual(active_dependency_names(requirements), ("packaging", "pathspec"))
        with self.assertRaisesRegex(BuilderInputError, "unsupported Python 3.12 dependency marker"):
            active_dependency_names(['mystery>=1; sys_platform == "linux"'])

    def test_wheel_metadata_cannot_expand_the_locked_dependency_closure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            wheel = Path(raw) / "scikit_build_core-0.11.6-py3-none-any.whl"
            digest = write_wheel(
                wheel,
                "scikit-build-core",
                "0.11.6",
                ["packaging>=23.2", "pathspec>=0.10.1", "mystery>=1"],
            )
            record = {
                "name": "scikit-build-core",
                "version": "0.11.6",
                "sha256": digest,
                "active_dependencies": list(EXPECTED_ACTIVE_DEPENDENCIES["scikit-build-core"]),
            }
            with self.assertRaisesRegex(BuilderInputError, "active dependency closure mismatch"):
                inspect_wheel(wheel, record)

    def test_deterministic_extraction_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wheel_dir = root / "wheels"
            wheel_dir.mkdir()
            records = {}
            for name in EXPECTED_ORDER:
                version = EXPECTED_MODULES[name]
                filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
                wheel = wheel_dir / filename
                extras = {"../escape.py": b"x"} if name == "pip" else None
                digest = write_wheel(wheel, name, version, [], extras)
                records[name] = {"filename": filename, "sha256": digest}
            with self.assertRaisesRegex(BuilderInputError, "escapes builder site-packages"):
                safe_extract_wheels(records, wheel_dir, root / "site")
            self.assertFalse((root / "escape.py").exists())

    def test_tree_identity_depends_only_on_relative_paths_and_file_bytes(self) -> None:
        identities = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                (root / "pkg").mkdir()
                (root / "pkg" / "__init__.py").write_bytes(b"value = 1\n")
                (root / "pkg-1.0.dist-info").mkdir()
                (root / "pkg-1.0.dist-info" / "METADATA").write_bytes(b"Name: pkg\nVersion: 1.0\n")
                evidence = tree_evidence(root)
                identities.append((evidence["tree_sha256"], evidence["file_count"]))
        self.assertEqual(identities[0], identities[1])

    def test_base_runtime_archive_and_qualification_are_bound_together(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = root / "runtime"
            python = runtime / "bin" / "python3.12"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"qualified-cpython-fixture")
            stdlib = runtime / "lib" / "python3.12" / "site.py"
            stdlib.parent.mkdir(parents=True)
            stdlib.write_bytes(b"qualified-stdlib-fixture")
            revision = "1" * 40
            manifest = {
                "format_version": 1,
                "platform": "linux-x64",
                "profile": "numpy",
                "python_abi": "cp312",
                "producer": {"repository": "precsim/python-numpy-fenics", "revision": revision},
                "files": [{
                    "path": "bin/python3.12",
                    "kind": "file",
                    "bytes": python.stat().st_size,
                    "sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
                    "owners": ["python-numpy-fenics"],
                }, {
                    "path": "lib/python3.12/site.py",
                    "kind": "file",
                    "bytes": stdlib.stat().st_size,
                    "sha256": hashlib.sha256(stdlib.read_bytes()).hexdigest(),
                    "owners": ["python-numpy-fenics"],
                }],
            }
            manifest_path = runtime / "runtime-manifest.json"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
            archive = root / "python-runtime-v0.1.0-linux-x64-cp312-numpy.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                # Match the workflow's `tar -C "$runtime_root" .` member layout,
                # including the leading ./ on files beneath the archive root.
                bundle.add(runtime, arcname=".")
            with tarfile.open(archive, "r:gz") as bundle:
                self.assertIn("./runtime-manifest.json", [member.name for member in bundle.getmembers()])
            qualification = root / "qualification.json"
            qualification.write_text(json.dumps({
                "format_version": 1,
                "producer_repository": "precsim/python-numpy-fenics",
                "producer_revision": revision,
                "workflow_run_id": "fixture",
                "platform": "linux-x64",
                "profile": "numpy",
                "artifact": archive.name,
                "artifact_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "checks": {
                    "pinned_manylinux": "pass",
                    "glibc_floor": "pass",
                    "inherited_core_identity": "pass",
                    "numpy_source_build": "pass",
                    "private_openblas": "pass",
                    "blas_lapack_functional": "pass",
                    "native_closure": "pass",
                    "private_consumer_checkout": "not-applicable",
                },
            }), encoding="utf-8")

            evidence, executable = validate_base_runtime(runtime, archive, qualification)
            self.assertEqual(executable, python)
            self.assertEqual(evidence["producer_revision"], revision)

            python.write_bytes(b"changed")
            with self.assertRaisesRegex(BuilderInputError, "differs from runtime-manifest"):
                validate_base_runtime(runtime, archive, qualification)
            python.write_bytes(b"qualified-cpython-fixture")
            stdlib.write_bytes(b"tampered-stdlib-fixture")
            with self.assertRaisesRegex(BuilderInputError, "file differs from runtime-manifest"):
                validate_base_runtime(runtime, archive, qualification)
            stdlib.write_bytes(b"qualified-stdlib-fixture")
            (runtime / "lib" / "python3.12" / "unrecorded.py").write_bytes(b"unexpected")
            with self.assertRaisesRegex(BuilderInputError, "file inventory differs"):
                validate_base_runtime(runtime, archive, qualification)

    def test_base_runtime_archive_rejects_traversal_member(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = root / "runtime"
            python = runtime / "bin" / "python3.12"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"qualified-cpython-fixture")
            revision = "2" * 40
            manifest = {
                "format_version": 1,
                "platform": "linux-x64",
                "profile": "numpy",
                "python_abi": "cp312",
                "producer": {"repository": "precsim/python-numpy-fenics", "revision": revision},
                "files": [{
                    "path": "bin/python3.12",
                    "kind": "file",
                    "bytes": python.stat().st_size,
                    "sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
                    "owners": ["python-numpy-fenics"],
                }],
            }
            manifest_path = runtime / "runtime-manifest.json"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
            archive = root / "python-runtime-v0.1.0-linux-x64-cp312-numpy.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                bundle.add(runtime, arcname=".")
                escaped = tarfile.TarInfo("../escape")
                escaped.size = 1
                bundle.addfile(escaped, io.BytesIO(b"x"))
            qualification = root / "qualification.json"
            qualification.write_text(json.dumps({
                "format_version": 1,
                "producer_repository": "precsim/python-numpy-fenics",
                "producer_revision": revision,
                "workflow_run_id": "fixture",
                "platform": "linux-x64",
                "profile": "numpy",
                "artifact": archive.name,
                "artifact_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "checks": {
                    "pinned_manylinux": "pass",
                    "glibc_floor": "pass",
                    "inherited_core_identity": "pass",
                    "numpy_source_build": "pass",
                    "private_openblas": "pass",
                    "blas_lapack_functional": "pass",
                    "native_closure": "pass",
                    "private_consumer_checkout": "not-applicable",
                },
            }), encoding="utf-8")

            with self.assertRaisesRegex(BuilderInputError, "unsafe member path"):
                validate_base_runtime(runtime, archive, qualification)


if __name__ == "__main__":
    unittest.main()
