from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from scripts.linux.prepare_fenics_patchelf import (
    PatchelfPrepareError,
    extract_binary,
    load_lock,
    verify_binary,
)


ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "locks" / "linux" / "linux-x86_64-fenics-patchelf.json"


class LinuxFenicsPatchelfTests(unittest.TestCase):
    def test_checked_in_lock_is_public_and_sha_bound(self) -> None:
        data = load_lock(LOCK)
        self.assertEqual(data["tool"]["version"], "0.19.3")
        self.assertEqual(
            data["tool"]["sha256"],
            "38861b171079e440b25ac86483284f690d0ee7921dba98b0c8fe04a9b0a11667",
        )
        self.assertEqual(
            data["tool"]["binary_sha256"],
            "3acab15a88e5150b394a051fa573a0bc4faf354decfa8710c901bfa1a051ed56",
        )
        self.assertTrue(data["tool"]["url"].startswith("https://github.com/NixOS/patchelf/"))

    def test_private_or_path_escaping_lock_is_rejected(self) -> None:
        data = json.loads(LOCK.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "lock.json"
            private = copy.deepcopy(data)
            private["tool"]["url"] = "https://internal.example.invalid/patchelf.tar.gz"
            path.write_text(json.dumps(private), encoding="utf-8")
            with self.assertRaisesRegex(PatchelfPrepareError, "allowed public HTTPS origin"):
                load_lock(path)

            escaping = copy.deepcopy(data)
            escaping["tool"]["filename"] = "../patchelf.tar.gz"
            path.write_text(json.dumps(escaping), encoding="utf-8")
            with self.assertRaisesRegex(PatchelfPrepareError, "archive filename"):
                load_lock(path)

    def test_extract_binary_selects_exact_regular_member(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "patchelf.tar.gz"
            payload = b"binary-bytes"
            with tarfile.open(archive, "w:gz") as bundle:
                member = tarfile.TarInfo("./bin/patchelf")
                member.size = len(payload)
                member.mode = 0o755
                bundle.addfile(member, io.BytesIO(payload))
            output = root / "out"
            output.mkdir()
            binary = extract_binary(archive, output)
            self.assertEqual(binary.relative_to(output).as_posix(), "bin/patchelf")
            self.assertEqual(binary.read_bytes(), payload)

    def test_verify_binary_requires_exact_version_output(self) -> None:
        completed = mock.Mock(stdout="patchelf 0.19.3\n")
        with mock.patch("scripts.linux.prepare_fenics_patchelf.subprocess.run", return_value=completed):
            self.assertEqual(verify_binary(Path("/tmp/patchelf"), "0.19.3"), "patchelf 0.19.3")
        completed.stdout = "patchelf 0.18.0\n"
        with mock.patch("scripts.linux.prepare_fenics_patchelf.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(PatchelfPrepareError, "unexpected patchelf version"):
                verify_binary(Path("/tmp/patchelf"), "0.19.3")


if __name__ == "__main__":
    unittest.main()
