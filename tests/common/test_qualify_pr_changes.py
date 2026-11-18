"""Check the runtime qualification PR gate's fail-closed policy."""

import unittest
from pathlib import Path
import re
from io import BytesIO
from unittest.mock import patch

from scripts.common.qualify_pr_changes import (
    PLATFORMS,
    affected_platforms,
    needs_qualification,
    pull_request_files,
)


class QualificationGateTests(unittest.TestCase):
    def test_documentation_only_is_skipped(self):
        self.assertFalse(
            needs_qualification(
                ["README.md", "docs/prerelease-guide.md", "specs/EPIC.md"]
            )
        )

    def test_license_only_or_with_documentation_is_skipped(self):
        self.assertFalse(needs_qualification(["LICENSE.txt"]))
        self.assertFalse(needs_qualification(["LICENSE.txt", "README.md"]))

    def test_runtime_or_workflow_changes_require_qualification(self):
        for path in (
            "scripts/linux/build-core-runtime.sh",
            ".github/workflows/qualify-linux-core-numpy.yml",
            "locks/core/tcltk-8.6.18.json",
            "scripts/common/qualify_pr_changes.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(needs_qualification(["README.md", path]))

    def test_platform_owned_changes_only_qualify_their_platform(self):
        examples = {
            "windows": (
                "scripts/windows/stage-runtime.ps1",
                "locks/windows/windows-x64-fenics-package-lock.json",
                "runtime/windows/bootstrap.py",
                "tests/common/test_windows_core_layer.py",
                ".github/workflows/qualify-windows-core-numpy.yml",
            ),
            "linux": (
                "scripts/linux/build-numpy-runtime.sh",
                "locks/linux/linux-glibc217-numpy-sources.json",
                "runtime/linux/bootstrap.py",
                "tests/common/test_linux_abi_policy.py",
                ".github/workflows/qualify-linux-core-numpy.yml",
            ),
            "macos": (
                "scripts/macos/stage-core-runtime.sh",
                "locks/macos/macos-arm64-package-lock.json",
                "runtime/macos_fenics_embed_runtime.py",
                "tests/common/test_macos_runtime_evidence.py",
                ".github/workflows/qualify-macos-core-numpy.yml",
            ),
        }
        for platform, paths in examples.items():
            for path in paths:
                with self.subTest(platform=platform, path=path):
                    self.assertEqual(affected_platforms([path]), {platform})
                    for target in PLATFORMS:
                        self.assertEqual(
                            needs_qualification([path], target), target == platform
                        )

    def test_consumer_only_changes_do_not_rebuild_producer_runtime(self):
        paths = [
            "consumer-kit/tools/stage-runtime.py",
            "reference-bridge/src/mock_bridge.c",
            "tests/reference-bridge/test_runtime_kit.py",
            "locks/consumer/reference-runtime-v1.json",
            "scripts/consumer/stage_qualified_fenics.py",
            ".github/workflows/qualify-reference-bridge.yml",
            ".github/workflows/publish-reference-bridge.yml",
            "scripts/common/cleanup_actions_artifacts.py",
            "tests/common/test_cleanup_actions_artifacts.py",
        ]
        self.assertEqual(affected_platforms(paths), set())
        for platform in PLATFORMS:
            self.assertFalse(needs_qualification(paths, platform))

    def test_mixed_platform_changes_combine_without_cross_platform_builds(self):
        self.assertEqual(
            affected_platforms([
                "README.md",
                "consumer-kit/README.md",
                "scripts/linux/build-core-runtime.sh",
                "scripts/windows/test-runtime.ps1",
            ]),
            {"linux", "windows"},
        )

    def test_shared_and_unknown_changes_fail_closed_to_all_platforms(self):
        paths = [
            "contracts/runtime-manifest-v1.schema.json",
            "locks/core/tcltk-8.6.18.json",
            "scripts/common/qualify_pr_changes.py",
            "scripts/common/write_runtime_manifest.py",
            "tests/common/test_qualify_pr_changes.py",
            "config/runtime-matrix.json",
            ".github/workflows/prerelease.yml",
            "unexpected/new-runtime-policy.py",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(affected_platforms([path]), PLATFORMS)
        self.assertEqual(
            affected_platforms(["scripts/macos/stage-core-runtime.sh", paths[-1]]),
            PLATFORMS,
        )

    def test_unknown_platform_is_rejected(self):
        with self.assertRaises(ValueError):
            needs_qualification(["README.md"], "unix")

    def test_renamed_runtime_file_still_triggers_source_platform(self):
        response = BytesIO(
            b'[{"filename":"consumer-kit/obsolete.py",'
            b'"previous_filename":"scripts/linux/build-core-runtime.sh",'
            b'"status":"renamed"}]'
        )
        with patch("scripts.common.qualify_pr_changes.urllib.request.urlopen",
                   return_value=response):
            paths = pull_request_files("precsim/python-numpy-fenics", "1", "token")
        self.assertEqual(affected_platforms(paths), {"linux"})

    def test_renamed_file_without_previous_path_fails_closed(self):
        response = BytesIO(b'[{"filename":"docs/new.md","status":"renamed"}]')
        with patch("scripts.common.qualify_pr_changes.urllib.request.urlopen",
                   return_value=response):
            with self.assertRaises(ValueError):
                pull_request_files("precsim/python-numpy-fenics", "1", "token")

    def test_empty_changes_fail_closed(self):
        self.assertTrue(needs_qualification([]))

    def test_macos_required_checks_exist_without_runtime_qualification(self):
        """A skipped matrix job collapses to one check, blocking branch protection."""
        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github/workflows/qualify-macos-core-numpy.yml"
        ).read_text(encoding="utf-8")
        qualify = workflow.split("\n  qualify:\n", 1)[1]
        header, steps = qualify.split("\n    steps:\n", 1)
        self.assertIn("name: qualify-macos-core-numpy-${{ matrix.target_arch }}", header)
        self.assertNotRegex(header, r"(?m)^    if:")
        self.assertIn("target_arch: arm64", header)
        self.assertIn("runner: macos-15", header)
        self.assertIn("target_arch: x86_64", header)
        self.assertIn("runner: macos-15-intel", header)
        self.assertIn("matrix.runner || 'ubuntu-24.04'", header)
        self.assertIn("needs.changes.outputs.qualify == 'true'", header)

        blocks = re.split(r"(?m)^      - (?=name:|uses:)", steps)
        self.assertGreaterEqual(len(blocks), 12)
        noop_steps = 0
        for block in blocks[1:]:
            if "No macOS runtime changes;" in block:
                noop_steps += 1
                self.assertIn("if: needs.changes.outputs.qualify != 'true'", block)
            else:
                self.assertIn(
                    "needs.changes.outputs.qualify == 'true'",
                    block,
                    "Mac runtime step must not run on documentation-only PRs",
                )
        self.assertEqual(noop_steps, 1)


if __name__ == "__main__":
    unittest.main()
