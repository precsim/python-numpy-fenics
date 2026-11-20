"""Verify the parallel macOS FEniCS ZIP writer preserves legacy bytes."""

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile


MACOS_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "macos"
sys.path.insert(0, str(MACOS_SCRIPTS))
try:
    from fenics_deterministic_zip import (
        LEGACY_ZIP64_LIMIT,
        _check_legacy_zip32_archive_limit,
        _check_legacy_zip32_member_limits,
        _legacy_zip64_required_for_file_size,
        deterministic_zip,
    )
finally:
    sys.path.pop(0)


def legacy_deterministic_zip(stage_root: Path, destination: Path) -> None:
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        for path in sorted(stage_root.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(stage_root).as_posix()
            mode = stat.S_IMODE(os.lstat(path).st_mode)
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            if path.is_symlink():
                info.external_attr = (stat.S_IFLNK | mode) << 16
                data = os.readlink(path).encode("utf-8")
            else:
                info.external_attr = (stat.S_IFREG | mode) << 16
                data = path.read_bytes()
            archive.writestr(
                info,
                data,
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            )


class MacOSFenicsDeterministicZipTests(unittest.TestCase):
    def test_parallel_writer_is_byte_identical_to_legacy_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            (stage / "nested").mkdir(parents=True)
            (stage / "alpha.txt").write_bytes((b"alpha beta gamma\n" * 4096) + b"tail\n")
            (stage / "nested" / "binary.dat").write_bytes(bytes(range(256)) * 2048)
            (stage / "nested" / "unicode-λ.txt").write_text("λ" * 8192, encoding="utf-8")
            (stage / "alpha.txt").chmod(0o755)

            if os.name != "nt":
                os.symlink("../alpha.txt", stage / "nested" / "alpha-link")

            legacy = root / "legacy.zip"
            parallel_one = root / "parallel-one.zip"
            parallel_four = root / "parallel-four.zip"
            legacy_deterministic_zip(stage, legacy)
            deterministic_zip(stage, parallel_one, compression_level=6, workers=1)
            deterministic_zip(stage, parallel_four, compression_level=6, workers=4)

            expected = legacy.read_bytes()
            self.assertEqual(parallel_one.read_bytes(), expected)
            self.assertEqual(parallel_four.read_bytes(), expected)
            with zipfile.ZipFile(parallel_four) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.read("alpha.txt"), (b"alpha beta gamma\n" * 4096) + b"tail\n")

    def test_empty_archive_is_byte_identical_to_legacy_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir()
            legacy = root / "legacy.zip"
            parallel = root / "parallel.zip"
            legacy_deterministic_zip(stage, legacy)
            deterministic_zip(stage, parallel, compression_level=6, workers=4)
            self.assertEqual(parallel.read_bytes(), legacy.read_bytes())

    def test_legacy_zip64_boundaries_fail_closed_without_large_allocations(self) -> None:
        self.assertEqual(LEGACY_ZIP64_LIMIT, zipfile.ZIP64_LIMIT)
        largest_non_zip64 = int(LEGACY_ZIP64_LIMIT / 1.05)
        while _legacy_zip64_required_for_file_size(largest_non_zip64):
            largest_non_zip64 -= 1
        while not _legacy_zip64_required_for_file_size(largest_non_zip64 + 1):
            largest_non_zip64 += 1

        _check_legacy_zip32_member_limits(
            "ok.bin",
            file_size=largest_non_zip64,
            compressed_size=LEGACY_ZIP64_LIMIT,
        )
        with self.assertRaisesRegex(RuntimeError, "ZIP64 size handling"):
            _check_legacy_zip32_member_limits(
                "too-large.bin",
                file_size=largest_non_zip64 + 1,
                compressed_size=1,
            )
        with self.assertRaisesRegex(RuntimeError, "ZIP64 compressed-size handling"):
            _check_legacy_zip32_member_limits(
                "too-compressed.bin",
                file_size=1,
                compressed_size=LEGACY_ZIP64_LIMIT + 1,
            )
        _check_legacy_zip32_archive_limit("test offset", LEGACY_ZIP64_LIMIT)
        with self.assertRaisesRegex(RuntimeError, "ZIP64 test offset"):
            _check_legacy_zip32_archive_limit("test offset", LEGACY_ZIP64_LIMIT + 1)

    def test_rejects_nonpositive_worker_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "worker count"):
                deterministic_zip(root, root / "out.zip", compression_level=6, workers=0)


if __name__ == "__main__":
    unittest.main()
