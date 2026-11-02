"""Regression tests for public producer workflow action pinning."""

from __future__ import annotations

import unittest
from pathlib import Path
import tempfile

from scripts.common.validate_repo import private_source_directories, unpinned_actions


class WorkflowActionPinTests(unittest.TestCase):
    def test_nested_private_directories_are_checked_even_under_named_build_folders(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git" / "Private").mkdir(parents=True)
            (root / "scripts" / "build" / "PRIVATE").mkdir(parents=True)
            self.assertEqual(
                private_source_directories(root),
                [Path("scripts/build/PRIVATE")],
            )

    def test_pinned_actions_and_local_actions_are_permitted(self) -> None:
        workflow = "\n".join(
            [
                "    - uses: vendor/action@" + "a" * 40 + " # pinned",
                "      uses: vendor/repo/sub-action@" + "B" * 40,
                "    - uses: ./local/action",
            ]
        )
        self.assertEqual(unpinned_actions(workflow), [])

    def test_mutable_and_unversioned_references_are_rejected(self) -> None:
        workflow = "- uses: vendor/action@v2\n  uses: vendor/repo@main\n"
        self.assertEqual(unpinned_actions(workflow), ["vendor/action@v2", "vendor/repo@main"])

    def test_short_sha_is_rejected(self) -> None:
        self.assertEqual(unpinned_actions("uses: vendor/repo@deadbeef"), ["vendor/repo@deadbeef"])


if __name__ == "__main__":
    unittest.main()
