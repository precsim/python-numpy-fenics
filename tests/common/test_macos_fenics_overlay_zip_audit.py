"""Tests for direct macOS FEniCS overlay ZIP auditing."""

import hashlib
import json
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
    from audit_fenics_overlay_zip import BOOTSTRAP, TOOL_NAMES, audit_overlay_zip
finally:
    sys.path.pop(0)


def _write_member(
    archive: zipfile.ZipFile,
    name: str,
    data: bytes,
    mode: int,
) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.create_system = 3
    info.external_attr = mode << 16
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)


class MacOSFenicsOverlayZipAuditTests(unittest.TestCase):
    def _make_archive(self, root: Path, *, forbidden_numpy: bool = False) -> tuple[Path, Path]:
        archive_path = root / "overlay.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            _write_member(
                archive,
                BOOTSTRAP,
                b"def activate():\n    return None\n",
                stat.S_IFREG | 0o644,
            )
            clang, linker = TOOL_NAMES["x86_64"]
            clang_target = "clang-23"
            _write_member(
                archive,
                clang,
                clang_target.encode("utf-8"),
                stat.S_IFLNK | 0o755,
            )
            _write_member(
                archive,
                str(Path(clang).parent / clang_target).replace(os.sep, "/"),
                b"clang",
                stat.S_IFREG | 0o755,
            )
            _write_member(archive, linker, b"ld", stat.S_IFREG | 0o755)
            if forbidden_numpy:
                _write_member(
                    archive,
                    "fenics-runtime/conda/lib/python3.14/site-packages/numpy/core.py",
                    b"pass\n",
                    stat.S_IFREG | 0o644,
                )

        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        summary_path = root / "overlay.zip.json"
        summary_path.write_text(
            json.dumps({"artifact": archive_path.name, "sha256": digest}),
            encoding="utf-8",
        )
        return archive_path, summary_path

    def test_valid_archive_passes_without_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive, summary = self._make_archive(Path(temporary))
            expected = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(audit_overlay_zip(archive, summary, "x86_64"), expected)

    def test_forbidden_numpy_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive, summary = self._make_archive(Path(temporary), forbidden_numpy=True)
            with self.assertRaisesRegex(RuntimeError, "producer-owned NumPy"):
                audit_overlay_zip(archive, summary, "x86_64")

    def test_nonexecutable_tool_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, summary = self._make_archive(root)
            rewritten = root / "rewritten.zip"
            clang, _ = TOOL_NAMES["x86_64"]
            clang_target = str(Path(clang).parent / "clang-23").replace(os.sep, "/")
            with zipfile.ZipFile(archive) as source, zipfile.ZipFile(rewritten, "w") as output:
                for info in source.infolist():
                    mode = info.external_attr >> 16
                    if info.filename == clang_target:
                        mode = stat.S_IFREG | 0o644
                    _write_member(output, info.filename, source.read(info), mode)
            digest = hashlib.sha256(rewritten.read_bytes()).hexdigest()
            summary.write_text(
                json.dumps({"artifact": rewritten.name, "sha256": digest}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "not executable"):
                audit_overlay_zip(rewritten, summary, "x86_64")

    def test_duplicate_member_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, summary = self._make_archive(root)
            with zipfile.ZipFile(archive, "a") as output:
                _write_member(output, BOOTSTRAP, b"pass\n", stat.S_IFREG | 0o644)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            summary.write_text(
                json.dumps({"artifact": archive.name, "sha256": digest}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "duplicate ZIP member"):
                audit_overlay_zip(archive, summary, "x86_64")

    def test_noncanonical_member_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, summary = self._make_archive(root)
            with zipfile.ZipFile(archive, "a") as output:
                _write_member(
                    output,
                    "fenics-runtime/conda/../conda/escape.txt",
                    b"bad",
                    stat.S_IFREG | 0o644,
                )
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            summary.write_text(
                json.dumps({"artifact": archive.name, "sha256": digest}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "not canonical"):
                audit_overlay_zip(archive, summary, "x86_64")

    def test_summary_must_bind_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive, summary = self._make_archive(Path(temporary))
            summary.write_text(
                json.dumps({"artifact": archive.name, "sha256": "0" * 64}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "does not bind"):
                audit_overlay_zip(archive, summary, "x86_64")


if __name__ == "__main__":
    unittest.main()
