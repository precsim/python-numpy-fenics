"""Check the runtime qualification PR gate's fail-closed policy."""

import unittest

from scripts.common.qualify_pr_changes import needs_qualification


class QualificationGateTests(unittest.TestCase):
    def test_documentation_only_is_skipped(self):
        self.assertFalse(
            needs_qualification(
                ["README.md", "docs/prerelease-guide.md", "specs/EPIC.md"]
            )
        )

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


if __name__ == "__main__":
    unittest.main()
