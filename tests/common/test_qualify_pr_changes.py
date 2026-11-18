"""Check the runtime qualification PR gate's fail-closed policy."""

import unittest
from pathlib import Path
import re

from scripts.common.qualify_pr_changes import needs_qualification


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
        self.assertIn("target_arch: x86_64", header)
        self.assertIn("'macos-15' || 'ubuntu-24.04'", header)
        self.assertIn("needs.changes.outputs.qualify == 'true'", header)

        blocks = re.split(r"(?m)^      - (?=name:|uses:)", steps)
        self.assertGreaterEqual(len(blocks), 12)
        noop_steps = 0
        for block in blocks[1:]:
            if "Documentation-only change;" in block:
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
