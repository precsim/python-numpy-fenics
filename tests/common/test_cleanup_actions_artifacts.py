"""Fail-closed tests for scheduled GitHub Actions artifact selection."""

import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/common/cleanup_actions_artifacts.py"
spec = importlib.util.spec_from_file_location("cleanup_actions_artifacts", SCRIPT)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)
NOW = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)


def iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def artifact(name="qualify-linux-core-numpy-diagnostics-123", branch="feature", run_id=123, **changes):
    item = dict(id=10, name=name, expired=False, created_at=iso(8), size_in_bytes=1024,
                workflow_run=dict(id=run_id, head_branch=branch))
    item.update(changes)
    return item


def run(name="qualify-linux-core-numpy", branch="feature", run_id=123, **changes):
    item = dict(id=run_id, name=name, head_branch=branch, status="completed",
                conclusion="success", event="pull_request", updated_at=iso(7))
    item.update(changes)
    return item


class SelectionTests(unittest.TestCase):
    def select(self, arts, metadata=None, **kwargs):
        calls = []
        metadata = metadata if metadata is not None else run()
        def get_run(run_id):
            calls.append(run_id)
            return metadata
        selected = cleanup.select_candidates(arts, get_run, now=NOW, **kwargs)
        return selected, calls

    def test_only_exact_allowlisted_diagnostic_and_matching_run(self):
        arts = [artifact(), artifact(name="qualify-linux-core-numpy-diagnostics-123-extra"),
                artifact(name="qualify-linux-core-numpy-diagnostics-124"),
                artifact(name="python-runtime-linux-x64-cp314-core"),
                artifact(name="reference-release-set-index"),
                artifact(name="reference-package-linux-x64-fenics"),
                artifact(name="reference-bridge-candidate-linux-x64")]
        selected, calls = self.select(arts)
        self.assertEqual([item["id"] for item in selected], [10])
        self.assertEqual(calls, [123])

    def test_linux_fenics_failure_diagnostics_eligible_after_day(self):
        item = artifact(name="qualify-linux-fenics-diagnostics-123", created_at=iso(28))
        qualified = run(name="qualify-linux-fenics", conclusion="failure", updated_at=iso(25))
        self.assertEqual(self.select([item], qualified)[0], [item])
        self.assertEqual(self.select([item], run(name="qualify-linux-core-numpy"))[0], [])

    def test_preserve_main_missing_or_mismatched_branch(self):
        for branch in ("main", "", None):
            with self.subTest(branch=branch):
                self.assertEqual(self.select([artifact(branch=branch)])[0], [])
        self.assertEqual(self.select([artifact()], run(branch="main"))[0], [])
        self.assertEqual(self.select([artifact()], run(branch=None))[0], [])

    def test_preserve_active_unknown_and_cancelled_runs(self):
        for changes in (dict(status="in_progress"), dict(status=None),
                        dict(conclusion=None), dict(conclusion="cancelled"),
                        dict(event="schedule"), dict(id=456), dict(updated_at=None)):
            with self.subTest(changes=changes):
                self.assertEqual(self.select([artifact()], run(**changes))[0], [])

    def test_creation_and_completion_age_both_required(self):
        self.assertEqual(self.select([artifact(created_at=iso(5))])[0], [])
        self.assertEqual(self.select([artifact()], run(updated_at=iso(5)))[0], [])
        self.assertEqual(self.select([artifact(created_at="invalid")])[0], [])
        self.assertEqual(self.select([artifact()], run(updated_at="invalid"))[0], [])
        self.assertEqual(self.select([artifact()], run(updated_at=iso(7)), minimum_age_hours=8)[0], [])

    def test_failed_runs_require_day_of_diagnostics(self):
        self.assertEqual(self.select([artifact()], run(conclusion="failure"))[0], [])
        self.assertEqual(self.select([artifact(created_at=iso(28))],
                                     run(conclusion="failure", updated_at=iso(25)))[0],
                         [artifact(created_at=iso(28))])

    def test_skip_expired_malformed_and_unknown_metadata(self):
        invalid = [artifact(expired=True), artifact(expired=None), artifact(workflow_run=None),
                   artifact(id="10"), artifact(name=None), artifact(created_at=None),
                   artifact(workflow_run=dict(id="123", head_branch="feature"))]
        self.assertEqual(self.select(invalid), ([], []))

    def test_run_metadata_fetched_once(self):
        selected, calls = self.select([artifact(id=10), artifact(id=11)])
        self.assertEqual(len(selected), 2)
        self.assertEqual(calls, [123])

    def test_paginated_listing(self):
        calls = []
        def get(url, token):
            calls.append(url)
            if "&page=1" in url:
                return {"artifacts": [artifact(id=i) for i in range(100)]}
            return {"artifacts": [artifact(id=101)]}
        with mock.patch.object(cleanup, "request_json", side_effect=get):
            result = cleanup.list_artifacts("owner/repo", "token")
        self.assertEqual(len(result), 101)
        self.assertEqual(len(calls), 2)

    def test_dry_run_does_not_delete(self):
        with mock.patch.object(cleanup, "parse_args") as args, \
             mock.patch.object(cleanup, "list_artifacts", return_value=[artifact()]), \
             mock.patch.object(cleanup, "request_json", return_value=run()), \
             mock.patch.object(cleanup, "delete_artifact") as delete, \
             mock.patch.dict(os.environ, {"GITHUB_TOKEN": "fake"}), \
             mock.patch.object(cleanup, "datetime", wraps=datetime) as dt, redirect_stdout(io.StringIO()) as output:
            args.return_value = mock.Mock(repository="owner/repo", dry_run=True,
                                         minimum_age_hours=6, failed_minimum_age_hours=24)
            dt.now.return_value = NOW
            self.assertEqual(cleanup.main(), 0)
            delete.assert_not_called()
            self.assertIn("would delete", output.getvalue())

    def test_delete_only_selected_and_api_failure_propagates(self):
        with mock.patch.object(cleanup, "parse_args") as args, \
             mock.patch.object(cleanup, "list_artifacts", return_value=[artifact(), artifact(name="reference-release-set-index")]), \
             mock.patch.object(cleanup, "request_json", return_value=run()), \
             mock.patch.object(cleanup, "delete_artifact", side_effect=RuntimeError("API failure")) as delete, \
             mock.patch.dict(os.environ, {"GITHUB_TOKEN": "fake"}), \
             mock.patch.object(cleanup, "datetime", wraps=datetime) as dt, redirect_stdout(io.StringIO()):
            args.return_value = mock.Mock(repository="owner/repo", dry_run=False,
                                         minimum_age_hours=6, failed_minimum_age_hours=24)
            dt.now.return_value = NOW
            with self.assertRaisesRegex(RuntimeError, "API failure"):
                cleanup.main()
            delete.assert_called_once_with("owner/repo", 10, "fake")


if __name__ == "__main__":
    unittest.main()
