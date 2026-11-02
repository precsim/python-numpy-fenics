"""Contract tests for exact Windows core -> NumPy runtime layering."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.windows.verify_core_layer import CoreLayerError, verify_core_layer


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class WindowsCoreLayerTests(unittest.TestCase):
    def make_runtime_pair(self, root: Path) -> tuple[Path, Path]:
        core = root / "core"
        numpy = root / "numpy"
        core.mkdir()
        numpy.mkdir()
        files = {
            "python.exe": b"qualified-python",
            "DLLs/_ssl.pyd": b"qualified-ssl",
            "RUNTIME_PROFILE": b"core",
        }
        entries = []
        for relative, payload in files.items():
            path = core / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            target = numpy / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            entries.append(
                {
                    "path": relative,
                    "bytes": len(payload),
                    "sha256": sha256(payload),
                    "owners": ["python-numpy-fenics"],
                }
            )
        (numpy / "RUNTIME_PROFILE").write_bytes(b"numpy")
        (numpy / "Lib/site-packages/numpy/__init__.py").parent.mkdir(parents=True)
        (numpy / "Lib/site-packages/numpy/__init__.py").write_bytes(b"numpy")
        manifest = {
            "format_version": 1,
            "artifact": "python-runtime-win-x64-cp312-core",
            "version": "0.1.0",
            "platform": "win-x64",
            "python_abi": "cp312",
            "profile": "core",
            "producer": {"repository": "precsim/python-numpy-fenics", "revision": "a" * 40},
            "files": entries,
        }
        (core / "runtime-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return core, numpy

    def test_exact_layer_is_bound_to_core_manifest_and_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            core, numpy = self.make_runtime_pair(Path(raw))
            first = verify_core_layer(core, numpy)
            relocated = Path(raw) / "relocated numpy"
            relocated.mkdir()
            for source in numpy.rglob("*"):
                relative = source.relative_to(numpy)
                target = relocated / relative
                if source.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
            second = verify_core_layer(core, relocated)
            self.assertEqual(first, second)
            self.assertEqual(first["inherited_file_count"], 2)
            self.assertEqual(first["excluded_paths"], ["RUNTIME_PROFILE"])

    def test_layer_rejects_changed_inherited_core_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            core, numpy = self.make_runtime_pair(Path(raw))
            (numpy / "DLLs/_ssl.pyd").write_bytes(b"changed")
            with self.assertRaisesRegex(CoreLayerError, "changed inherited core file"):
                verify_core_layer(core, numpy)

    def test_layer_rejects_unmanifested_core_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            core, numpy = self.make_runtime_pair(Path(raw))
            (core / "unexpected.dll").write_bytes(b"untracked")
            with self.assertRaisesRegex(CoreLayerError, "inventory differs"):
                verify_core_layer(core, numpy)


if __name__ == "__main__":
    unittest.main()
