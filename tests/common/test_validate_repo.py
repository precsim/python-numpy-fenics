"""Regression tests for public producer workflow action pinning."""

from __future__ import annotations

import unittest
import copy
from pathlib import Path
import tempfile

from scripts.common.validate_repo import (
    EXPECTED_WINDOWS_CPYTHON_LOCK,
    REFERENCE_BRIDGE_EXPORTS,
    REFERENCE_BRIDGE_TARGETS,
    REFERENCE_OCTAVE_ABI,
    bridge_build_contract_errors,
    private_source_directories,
    unpinned_actions,
    windows_cpython_lock_errors,
)


class WorkflowActionPinTests(unittest.TestCase):
    def test_reference_bridge_requires_octave_abi_metadata_and_two_exports(self) -> None:
        payload = {
            "format_version": 1,
            "mex_name": "bridge",
            "octave_abi": copy.deepcopy(REFERENCE_OCTAVE_ABI),
            "targets": {
                target: {
                    "output": "bridge.mex",
                    "exports": list(REFERENCE_BRIDGE_EXPORTS),
                }
                for target in REFERENCE_BRIDGE_TARGETS
            },
        }
        self.assertEqual(bridge_build_contract_errors(payload), [])

        old_contract = copy.deepcopy(payload)
        old_contract["targets"]["linux-x64"]["exports"] = ["mexFunction"]
        self.assertEqual(
            bridge_build_contract_errors(old_contract),
            [
                "reference bridge target has an invalid MEX/Octave ABI contract: linux-x64"
            ],
        )

        wrong_abi = copy.deepcopy(payload)
        wrong_abi["octave_abi"]["compatibility"] = "GNU Octave 10.1+"
        self.assertEqual(
            bridge_build_contract_errors(wrong_abi),
            ["reference bridge build manifest has an invalid target contract"],
        )

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

    def test_windows_cpython_binary_lock_is_exact(self) -> None:
        self.assertEqual(windows_cpython_lock_errors(copy.deepcopy(EXPECTED_WINDOWS_CPYTHON_LOCK)), [])
        changed = copy.deepcopy(EXPECTED_WINDOWS_CPYTHON_LOCK)
        changed["upstream"]["archive_asset"]["sha256"] = "0" * 64
        self.assertTrue(windows_cpython_lock_errors(changed))


if __name__ == "__main__":
    unittest.main()
