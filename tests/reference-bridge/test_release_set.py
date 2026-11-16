"""Regression tests for safe reference release-set archive extraction."""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "consumer-kit" / "tools" / "verify-release-set.py"
SPEC = importlib.util.spec_from_file_location("verify_release_set", TOOL)
assert SPEC is not None and SPEC.loader is not None
release_set = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_set)


def zip_symlink(name: str, target: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    return info


def add_tar_file(archive: tarfile.TarFile, name: str, content: bytes = b"payload") -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(content))


class ReleaseSetExtractionTests(unittest.TestCase):
    def assert_empty(self, directory: Path) -> None:
        self.assertEqual(list(directory.iterdir()), [])

    def test_member_paths_reject_traversal_and_noncanonical_aliases(self) -> None:
        for raw in (
            "../escape",
            "root/../escape",
            "/absolute",
            "root/./file",
            "root//file",
            "root\\file",
            "C:/absolute",
            "root/file//",
        ):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(SystemExit, "unsafe release archive member"):
                    release_set.safe_member(raw)

    def test_zip_rejects_case_colliding_duplicate_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_path = root / "duplicate.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("package/A.txt", b"first")
                archive.writestr("package/a.txt", b"second")
            destination = root / "out"
            destination.mkdir()

            with self.assertRaisesRegex(SystemExit, "duplicate ZIP member"):
                release_set.extract_zip(archive_path, destination)
            self.assert_empty(destination)

    def test_zip_rejects_member_beneath_symlink_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_path = root / "symlink-parent.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(zip_symlink("package/link", "subdir"), b"subdir")
                archive.writestr("package/link/payload.txt", b"would traverse link")
            destination = root / "out"
            destination.mkdir()

            with self.assertRaisesRegex(SystemExit, "traverses non-directory archive entry"):
                release_set.extract_zip(archive_path, destination)
            self.assert_empty(destination)

    def test_zip_rejects_symlink_target_that_escapes_package_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_path = root / "escaping-link.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(zip_symlink("package/dir/link", "../../outside"), b"../../outside")
            destination = root / "out"
            destination.mkdir()

            with self.assertRaisesRegex(SystemExit, "unsafe ZIP symlink"):
                release_set.extract_zip(archive_path, destination)
            self.assert_empty(destination)

    def test_tar_rejects_unsafe_member_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_path = root / "unsafe.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                add_tar_file(archive, "package/good.txt", b"good")
                add_tar_file(archive, "package/../escape.txt", b"escape")
            destination = root / "out"
            destination.mkdir()

            with self.assertRaisesRegex(SystemExit, "unsafe release archive member"):
                release_set.extract_tar(archive_path, destination)
            self.assert_empty(destination)

    def test_tar_rejects_duplicate_and_symlink_parent_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            duplicate = root / "duplicate.tar.gz"
            with tarfile.open(duplicate, "w:gz") as archive:
                add_tar_file(archive, "package/A.txt", b"first")
                add_tar_file(archive, "package/a.txt", b"second")
            duplicate_out = root / "duplicate-out"
            duplicate_out.mkdir()
            with self.assertRaisesRegex(SystemExit, "duplicate tar member"):
                release_set.extract_tar(duplicate, duplicate_out)
            self.assert_empty(duplicate_out)

            symlink_parent = root / "symlink-parent.tar.gz"
            with tarfile.open(symlink_parent, "w:gz") as archive:
                link = tarfile.TarInfo("package/link")
                link.type = tarfile.SYMTYPE
                link.linkname = "subdir"
                link.mode = 0o777
                archive.addfile(link)
                add_tar_file(archive, "package/link/payload.txt", b"would traverse link")
            symlink_out = root / "symlink-out"
            symlink_out.mkdir()
            with self.assertRaisesRegex(SystemExit, "traverses non-directory archive entry"):
                release_set.extract_tar(symlink_parent, symlink_out)
            self.assert_empty(symlink_out)


if __name__ == "__main__":
    unittest.main()
