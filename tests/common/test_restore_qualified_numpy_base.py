"""Fail-closed tests for immutable, published NumPy base restoration."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from scripts.common.prepare_prerelease import validate_cross_profile_base_identities

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/common/restore_qualified_numpy_base.py"
spec = importlib.util.spec_from_file_location("restore_qualified_numpy_base", SCRIPT)
assert spec and spec.loader
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


class RestoreNumPyBaseTests(unittest.TestCase):
    def test_release_lock_has_complete_immutable_platform_pins(self):
        lock = json.loads((SCRIPT.parents[2] / "locks/core/fenics-numpy-base-release.json").read_text())
        self.assertEqual(lock["repository"], "precsim/python-numpy-fenics")
        self.assertEqual(lock["tag"], "runtime-v0.1.0-rc1")
        self.assertEqual(set(lock["platforms"]), restore.PLATFORMS)
        for pins in lock["platforms"].values():
            self.assertEqual(set(pins), {"archive_sha256", "manifest_sha256", "qualification_sha256", "numpy_tree_sha256", "numpy_entry_count"})
            self.assertTrue(all(restore.SHA.fullmatch(value) for key, value in pins.items() if key.endswith("sha256")))
            self.assertGreater(pins["numpy_entry_count"], 0)

    def test_restore_rejects_unpinned_release_tag_without_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(restore, "download_asset") as download:
                with self.assertRaisesRegex(SystemExit, "release tag differs"):
                    restore.verified_restore(
                        "runtime-v0.1.0-rc2", "win-x64", root / "runtime",
                        root / "archive.zip", root / "qualification.json",
                        SCRIPT.parents[2] / "locks/core/fenics-numpy-base-release.json",
                    )
                download.assert_not_called()

    def test_rejects_traversal_or_duplicate_archive_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as contents:
                contents.writestr("../escape", b"danger")
            with self.assertRaisesRegex(SystemExit, "unsafe archive path"):
                restore.restore_zip(archive, root / "runtime")
            with zipfile.ZipFile(archive, "w") as contents:
                contents.writestr("same.txt", b"first")
                contents.writestr("same.txt", b"second")
            with self.assertRaisesRegex(SystemExit, "duplicate ZIP entry"):
                restore.restore_zip(archive, root / "runtime2")

    def test_linux_profile_identity_comes_from_pinned_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            runtime.mkdir()
            restore.validate_extracted_profile_marker(runtime, "linux-x64")
            with self.assertRaisesRegex(SystemExit, "not the NumPy profile"):
                restore.validate_extracted_profile_marker(runtime, "win-x64")
            (runtime / "RUNTIME_PROFILE").write_text("numpy\n", encoding="utf-8")
            restore.validate_extracted_profile_marker(runtime, "linux-x64")
            restore.validate_extracted_profile_marker(runtime, "win-x64")

    def test_rejects_modified_release_asset_before_extracting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = {
                "format_version": 1,
                "repository": restore.REPO,
                "tag": "runtime-v0.1.0-rc1",
                "producer_revision": "a" * 40,
                "platforms": {"win-x64": {
                    "archive_sha256": "0" * 64,
                    "manifest_sha256": "1" * 64,
                    "qualification_sha256": "2" * 64,
                    "numpy_tree_sha256": "3" * 64,
                    "numpy_entry_count": 1,
                }},
            }
            lock_path = root / "lock.json"
            lock_path.write_text(json.dumps(lock))
            artifact = restore.name_for("win-x64")
            (root / artifact).write_bytes(b"tampered payload")
            (root / (artifact + ".manifest.json")).write_text("{}")
            (root / "qualification-win-x64-numpy.json").write_text("{}")
            with mock.patch.object(restore, "download_asset", side_effect=lambda tag, name, target: root / name):
                with self.assertRaisesRegex(SystemExit, "pinned SHA-256 mismatch"):
                    restore.verified_restore(
                        "runtime-v0.1.0-rc1", "win-x64", root / "runtime",
                        root / "archive.zip", root / "qualification.json", lock_path,
                    )
            self.assertFalse((root / "runtime").exists())

    def test_prerelease_links_fenics_to_pinned_external_numpy_base(self):
        lock = json.loads((SCRIPT.parents[2] / "locks/core/fenics-numpy-base-release.json").read_text())
        records = {}
        for platform, pins in lock["platforms"].items():
            records[(platform, "numpy")] = {
                "artifact": "current-revision-numpy.zip",
                "artifact_sha256": "f" * 64,
                "manifest": {"platform": platform, "files": []},
                "manifest_sha256": "f" * 64,
            }
            records[(platform, "fenics")] = {
                "artifact": "fenics-overlay.zip",
                "artifact_sha256": "e" * 64,
                "manifest": {
                    "numpy": {
                        "base_producer_revision": lock["producer_revision"],
                        "base_manifest_sha256": pins["manifest_sha256"],
                        "base_tree_sha256": pins["numpy_tree_sha256"],
                        "base_entry_count": pins["numpy_entry_count"],
                    },
                },
            }
        bindings = validate_cross_profile_base_identities(records)
        self.assertEqual(len(bindings), 4)
        for binding in bindings:
            self.assertEqual(binding["numpy_base_release_tag"], lock["tag"])
            self.assertEqual(binding["numpy_base_producer_revision"], lock["producer_revision"])
            self.assertEqual(binding["numpy_artifact_sha256"], lock["platforms"][binding["platform"]]["archive_sha256"])

        records[("win-x64", "fenics")]["manifest"]["numpy"]["base_tree_sha256"] = "0" * 64
        with self.assertRaisesRegex(SystemExit, "tree identity"):
            validate_cross_profile_base_identities(records)


if __name__ == "__main__":
    unittest.main()
