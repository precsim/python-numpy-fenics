import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.linux.package_fenics_overlay import validate_base_runtime


class LinuxFenicsBaseRuntimeTests(unittest.TestCase):
    def _write_base(self, root: Path, revision: str) -> tuple[Path, Path]:
        runtime = root / "runtime"
        python = runtime / "bin" / "python3.14"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"qualified-cpython")
        (runtime / "lib" / "python3.14" / "site-packages" / "numpy").mkdir(parents=True)

        core_provenance = runtime / "build-provenance-linux.json"
        core_provenance.write_text(
            json.dumps({"python": {"version": "3.14.10"}}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        numpy_provenance = runtime / "build-provenance-linux-numpy.json"
        numpy_provenance.write_text(
            json.dumps(
                {
                    "baseline": {"architecture": "x86_64", "glibc": "2.17"},
                    "numpy": {"version": "2.5.2"},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        numpy_payload = runtime / "numpy-payload-manifest-linux.json"
        numpy_payload.write_text("{}\n", encoding="utf-8")

        manifest_files = []
        for path in (python, core_provenance, numpy_provenance, numpy_payload):
            manifest_files.append(
                {
                    "path": path.relative_to(runtime).as_posix(),
                    "kind": "file",
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "owners": ["python-numpy-fenics"],
                }
            )
        manifest = {
            "format_version": 1,
            "platform": "linux-x64",
            "profile": "numpy",
            "python_abi": "cp314",
            "producer": {
                "repository": "precsim/python-numpy-fenics",
                "revision": revision,
            },
            "tcl_tk": {
                "version": "8.6.18",
                "ownership": "producer",
                "provenance_path": "build-provenance-linux.json",
                "provenance_sha256": hashlib.sha256(core_provenance.read_bytes()).hexdigest(),
            },
            "files": manifest_files,
        }
        (runtime / "runtime-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
        )

        qualification = root / "qualification.json"
        qualification.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "producer_repository": "precsim/python-numpy-fenics",
                    "producer_revision": revision,
                    "platform": "linux-x64",
                    "profile": "numpy",
                    "checks": {
                        "pinned_manylinux": "pass",
                        "glibc_floor": "pass",
                        "inherited_core_identity": "pass",
                        "numpy_source_build": "pass",
                        "private_openblas": "pass",
                        "blas_lapack_functional": "pass",
                        "native_closure": "pass",
                    },
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return runtime, qualification

    def test_accepts_qualification_bound_to_expected_release_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            revision = "1" * 40
            runtime, qualification = self._write_base(Path(raw), revision)
            info = validate_base_runtime(runtime, qualification, revision)
            self.assertEqual(info["producer_revision"], revision)

    def test_rejects_other_valid_release_revision(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            runtime, qualification = self._write_base(Path(raw), "1" * 40)
            with self.assertRaisesRegex(SystemExit, "invalid public NumPy base runtime manifest"):
                validate_base_runtime(runtime, qualification, "2" * 40)


if __name__ == "__main__":
    unittest.main()
