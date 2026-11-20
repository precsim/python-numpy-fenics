"""Stable runtime promotion must preserve qualified prerelease payload bytes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PROMOTE = ROOT / "scripts" / "common" / "promote_runtime_release.py"
WORKFLOW = ROOT / ".github" / "workflows" / "promote-runtime-release.yml"


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class StableRuntimePromotionTests(unittest.TestCase):
    def make_source(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir(parents=True)
        asset = b"qualified runtime bytes\x00\x01"
        (source / "runtime.bin").write_bytes(asset)
        revision = "a" * 40
        qualifications = [
            {
                "platform": platform,
                "profile": profile,
                "producer_revision": revision,
            }
            for platform in ("win-x64", "linux-x64", "macos-arm64", "macos-x64")
            for profile in ("core", "numpy", "fenics")
        ]
        manifest = {
            "format_version": 1,
            "tag": "runtime-v0.1.0-rc3",
            "version": "0.1.0",
            "producer_repository": "precsim/python-numpy-fenics",
            "producer_revision": revision,
            "profiles": ["core", "numpy", "fenics"],
            "source_runs": [{"head_sha": revision, "workflow_run_id": "123"}],
            "qualifications": qualifications,
            "base_runtime_bindings": [],
            "files": [{"name": "runtime.bin", "sha256": digest(asset)}],
        }
        (source / "prerelease-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return source

    def run_promotion(self, source: Path, output: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(PROMOTE),
                "--source",
                str(source),
                "--output",
                str(output),
                "--source-tag",
                "runtime-v0.1.0-rc3",
                "--target-tag",
                "runtime-v0.1.0",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_promotion_preserves_payload_and_records_source_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            source_manifest_bytes = (source / "prerelease-manifest.json").read_bytes()
            output = root / "output"
            result = self.run_promotion(source, output)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((output / "runtime.bin").read_bytes(), (source / "runtime.bin").read_bytes())
            promoted = json.loads((output / "prerelease-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(promoted["tag"], "runtime-v0.1.0")
            self.assertEqual(promoted["producer_revision"], "a" * 40)
            self.assertEqual(promoted["promotion"]["source_tag"], "runtime-v0.1.0-rc3")
            self.assertEqual(
                promoted["promotion"]["source_manifest_sha256"],
                digest(source_manifest_bytes),
            )

    def test_promotion_rejects_changed_or_extra_source_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self.make_source(root)
            (source / "runtime.bin").write_bytes(b"tampered")
            result = self.run_promotion(source, root / "tampered-output")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SHA-256 mismatch", result.stdout + result.stderr)

            source = self.make_source(root / "second")
            (source / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
            result = self.run_promotion(source, root / "extra-output")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("asset set mismatch", result.stdout + result.stderr)

    def test_workflow_promotes_existing_release_without_rebuilding(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('gh release download "$SOURCE_TAG"', workflow)
        self.assertIn("promote_runtime_release.py", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn('git merge-base --is-ancestor "$producer_revision" HEAD', workflow)
        self.assertIn('-f sha="$PRODUCER_REVISION"', workflow)
        self.assertIn('--notes-file "$notes_file"', workflow)
        self.assertIn("published stable release asset set or digest differs", workflow)
        self.assertNotIn("--prerelease", workflow)


if __name__ == "__main__":
    unittest.main()
