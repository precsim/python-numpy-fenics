"""Archive extraction preserves the qualified runtime directory structure."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.common.archive_runtime import write_zip


class ArchiveRuntimeTests(unittest.TestCase):
    def test_zip_preserves_empty_site_packages_and_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            runtime = root / "runtime"
            (runtime / "Lib" / "site-packages").mkdir(parents=True)
            (runtime / "python.exe").write_bytes(b"runtime")
            first = root / "first.zip"
            second = root / "second.zip"
            write_zip(runtime, first)
            write_zip(runtime, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertIn("Lib/site-packages/", archive.namelist())
                unpacked = root / "unpacked"
                archive.extractall(unpacked)
            self.assertTrue((unpacked / "Lib" / "site-packages").is_dir())
            self.assertEqual((unpacked / "python.exe").read_bytes(), b"runtime")


if __name__ == "__main__":
    unittest.main()
