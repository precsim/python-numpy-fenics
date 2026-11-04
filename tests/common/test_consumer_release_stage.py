"""Regression tests for the public, fail-closed consumer handoff mock."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from scripts.consumer.stage_qualified_fenics import (
    ConsumerStageError, embedded_manifest, extract_layer, stage, validate_manifest,
)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def write_zip(path: Path, content: dict[str, bytes]) -> bytes:
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in content.items():
            archive.writestr(name, payload)
    return path.read_bytes()


class ConsumerHandoffTests(unittest.TestCase):
    def fixture(self, root: Path, platform: str = "win-x64") -> tuple[Path, Path]:
        cache = root / "cache"
        cache.mkdir()
        base_name = "python-runtime-base.zip"
        overlay_name = "fenics-runtime.zip"
        base_manifest = {"platform": platform, "profile": "numpy",
                         "producer": {"revision": "b" * 40}}
        base_bytes = json.dumps(base_manifest).encode()
        base = cache / base_name
        write_zip(base, {"runtime-manifest.json": base_bytes,
                         "python.exe": b"qualified base", "Lib/a.py": b"base"})
        overlay_manifest = {
            "platform": platform, "profile": "fenics", "python_abi": "cp314",
            "producer": {"revision": "a" * 40},
            "numpy": {"base_producer_revision": "b" * 40,
                      "base_manifest_sha256": digest(base_bytes),
                      "base_tree_sha256": "c" * 64, "base_entry_count": 2},
        }
        if platform == "win-x64":
            overlay_manifest["numpy"].update({
                "base_release_tag": "runtime-v0.1.0-rc1",
                "base_archive_sha256": digest(base.read_bytes()),
            })
        else:
            overlay_manifest["numpy"].update({
                "ownership": "consumer-numpy-profile", "producer_payload_excluded": True,
            })
        overlay_bytes = json.dumps(overlay_manifest).encode()
        overlay = cache / overlay_name
        write_zip(overlay, {"fenics-embed-manifest.json": overlay_bytes,
                            "Lib/site-packages/fenics_embed_runtime.py": b"overlay"})
        qualification_name = f"qualification-{platform}-numpy.json"
        qualification = {
            "format_version": 1, "platform": platform, "profile": "numpy",
            "producer_repository": "precsim/python-numpy-fenics",
            "producer_revision": "b" * 40, "artifact": base_name,
            "artifact_sha256": digest(base.read_bytes()),
            "workflow_run_id": "12345", "checks": {"relocation": "pass"},
        }
        qualification_file = cache / qualification_name
        qualification_file.write_text(json.dumps(qualification), encoding="utf-8")
        fenics_pin = {"name": overlay_name, "sha256": digest(overlay.read_bytes()),
                      "manifest_sha256": digest(overlay_bytes)}
        base_pin = {"name": base_name, "sha256": digest(base.read_bytes()),
                    "manifest_sha256": digest(base_bytes), "tag": "runtime-v0.1.0-rc1",
                    "producer_revision": "b" * 40, "numpy_tree_sha256": "c" * 64,
                    "numpy_entry_count": 2, "qualification_name": qualification_name,
                    "qualification_sha256": digest(qualification_file.read_bytes())}
        lock = {"format_version": 1, "repository": "precsim/python-numpy-fenics",
                "tag": "runtime-v0.1.0-rc2", "producer_revision": "a" * 40,
                "platforms": {platform: {"fenics": fenics_pin, "numpy_base": base_pin}}}
        record = {
            "format_version": 1, "tag": lock["tag"],
            "producer_repository": lock["repository"],
            "producer_revision": lock["producer_revision"],
            "files": [{"name": overlay_name, "sha256": fenics_pin["sha256"]}],
            "qualifications": [{"platform": platform, "profile": "fenics",
                                "artifact": overlay_name,
                                "artifact_sha256": fenics_pin["sha256"],
                                "manifest_sha256": fenics_pin["manifest_sha256"]}],
            "base_runtime_bindings": [{
                "platform": platform, "numpy_base_release_tag": base_pin["tag"],
                "numpy_base_producer_revision": base_pin["producer_revision"],
                "numpy_artifact": base_name, "numpy_artifact_sha256": base_pin["sha256"],
                "numpy_manifest_sha256": base_pin["manifest_sha256"],
                "numpy_tree_sha256": base_pin["numpy_tree_sha256"],
                "numpy_entry_count": base_pin["numpy_entry_count"]}],
        }
        manifest_file = cache / "prerelease-manifest.json"
        manifest_file.write_text(json.dumps(record), encoding="utf-8")
        lock["prerelease_manifest_sha256"] = digest(manifest_file.read_bytes())
        lock_file = root / "lock.json"
        lock_file.write_text(json.dumps(lock), encoding="utf-8")
        return lock_file, cache

    def test_stages_verified_cross_release_overlay_without_colliding_with_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock, cache = self.fixture(root)
            output = root / "host with spaces" / "runtime with spaces"
            evidence = stage(lock, "win-x64", cache, output)
            self.assertEqual(evidence["base_tag"], "runtime-v0.1.0-rc1")
            self.assertEqual((output / "python.exe").read_bytes(), b"qualified base")
            self.assertEqual((output / "Lib/site-packages/fenics_embed_runtime.py").read_bytes(), b"overlay")
            with self.assertRaisesRegex(ConsumerStageError, "already exists"):
                stage(lock, "win-x64", cache, output)

    def test_posix_cross_release_identity_uses_producer_and_manifest_pins(self) -> None:
        for platform in ("linux-x64", "macos-arm64", "macos-x64"):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lock, cache = self.fixture(root, platform)
                evidence = stage(lock, platform, cache, root / "runtime")
                self.assertEqual(evidence["base_revision"], "b" * 40)
                self.assertEqual((root / "runtime" / "python.exe").read_bytes(), b"qualified base")

    def test_rejects_changed_release_manifest_before_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock, cache = self.fixture(root)
            (cache / "prerelease-manifest.json").write_text("{}")
            with self.assertRaisesRegex(ConsumerStageError, "changed pinned asset"):
                stage(lock, "win-x64", cache, root / "output")
            self.assertFalse((root / "output").exists())

    def test_rejects_changed_archive_before_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock, cache = self.fixture(root)
            (cache / "fenics-runtime.zip").write_bytes(b"corrupt")
            with self.assertRaisesRegex(ConsumerStageError, "changed pinned asset"):
                stage(lock, "win-x64", cache, root / "output")
            self.assertFalse((root / "output").exists())

    def test_rejects_changed_base_qualification_pin_before_extracting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_file, cache = self.fixture(root)
            lock = json.loads(lock_file.read_text())
            lock["platforms"]["win-x64"]["numpy_base"]["qualification_sha256"] = "0" * 64
            lock_file.write_text(json.dumps(lock), encoding="utf-8")
            with self.assertRaisesRegex(ConsumerStageError, "changed pinned asset"):
                stage(lock_file, "win-x64", cache, root / "output")
            self.assertFalse((root / "output").exists())

    def test_rejects_wrong_rc2_numpy_instead_of_pinned_rc1_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_file, cache = self.fixture(root)
            lock = json.loads(lock_file.read_text())
            record = json.loads((cache / "prerelease-manifest.json").read_text())
            record["base_runtime_bindings"][0]["numpy_base_release_tag"] = "runtime-v0.1.0-rc2"
            with self.assertRaisesRegex(ConsumerStageError, "pinned prior release"):
                validate_manifest(lock, record, "win-x64")

    def test_rejects_archive_file_collision_and_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.zip"
            overlay = root / "overlay.zip"
            write_zip(base, {"Lib/package.py": b"base"})
            write_zip(overlay, {"Lib/package.py": b"overwrite"})
            output = root / "runtime"
            output.mkdir()
            occupied: dict[str, str] = {}
            extract_layer(base, output, occupied)
            with self.assertRaisesRegex(ConsumerStageError, "overwritten"):
                extract_layer(overlay, output, occupied)
            self.assertEqual((output / "Lib/package.py").read_bytes(), b"base")
            write_zip(overlay, {"../escape.txt": b"escape"})
            with self.assertRaisesRegex(ConsumerStageError, "unsafe archive path"):
                extract_layer(overlay, output, occupied)
            self.assertFalse((root / "escape.txt").exists())
            write_zip(overlay, {"C:/outside.txt": b"escape"})
            with self.assertRaisesRegex(ConsumerStageError, "invalid archive member"):
                extract_layer(overlay, output, occupied)
            write_zip(overlay, {"Lib//ambiguous.py": b"escape"})
            with self.assertRaisesRegex(ConsumerStageError, "unsafe archive path"):
                extract_layer(overlay, output, occupied)

    def test_tar_gnu_dot_prefix_and_shared_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, overlay = root / "base.tar.gz", root / "overlay.tar.gz"
            for path, filename in ((base, "python.py"), (overlay, "dolfinx.py")):
                with tarfile.open(path, "w:gz") as archive:
                    for name in (".", "./lib", "./lib/site-packages"):
                        entry = tarfile.TarInfo(name)
                        entry.type = tarfile.DIRTYPE
                        archive.addfile(entry)
                    payload = filename.encode()
                    entry = tarfile.TarInfo("./lib/site-packages/" + filename)
                    entry.size = len(payload)
                    archive.addfile(entry, io.BytesIO(payload))
            runtime = root / "runtime"
            runtime.mkdir()
            occupied: dict[str, str] = {}
            extract_layer(base, runtime, occupied)
            extract_layer(overlay, runtime, occupied)
            self.assertTrue((runtime / "lib/site-packages/python.py").is_file())
            self.assertTrue((runtime / "lib/site-packages/dolfinx.py").is_file())

    def test_tar_embedded_manifest_accepts_gnu_dot_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "runtime.tar.gz"
            payload = json.dumps({"profile": "numpy"}).encode()
            with tarfile.open(archive, "w:gz") as opened:
                member = tarfile.TarInfo("./runtime-manifest.json")
                member.size = len(payload)
                opened.addfile(member, io.BytesIO(payload))
            self.assertEqual(
                embedded_manifest(archive, "runtime-manifest.json", digest(payload)),
                {"profile": "numpy"},
            )

    def test_tar_rejects_escaping_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "escape.tar.gz"
            with tarfile.open(archive, "w:gz") as opened:
                member = tarfile.TarInfo("link")
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                opened.addfile(member)
            runtime = root / "runtime"
            runtime.mkdir()
            with self.assertRaisesRegex(ConsumerStageError, "escapes runtime"):
                extract_layer(archive, runtime, {})


if __name__ == "__main__":
    unittest.main()
