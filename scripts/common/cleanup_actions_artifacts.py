#!/usr/bin/env python3
"""Delete only explicitly disposable, completed non-main CI diagnostics.

All producer qualification artifacts, bridge candidate/package artifacts, and
release-set evidence are intentionally ineligible for scheduled deletion.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API_VERSION = "2022-11-28"
# Every pair must match a known, short-lived, diagnostic-only upload step.
DISPOSABLE = {
    "qualify-linux-core-numpy": re.compile(r"qualify-linux-core-numpy-diagnostics-(\d+)\Z"),
    "qualify-linux-fenics": re.compile(r"qualify-linux-fenics-diagnostics-(\d+)\Z"),
}


def request_json(url: str, token: str) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "precsim-python-numpy-fenics-actions-cleanup",
            "X-GitHub-Api-Version": API_VERSION,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API GET failed ({exc.code}): {detail}") from exc


def delete_artifact(repository: str, artifact_id: int, token: str) -> None:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/artifacts/{artifact_id}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "precsim-python-numpy-fenics-actions-cleanup",
            "X-GitHub-Api-Version": API_VERSION,
        },
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 204:
                raise RuntimeError(
                    f"unexpected delete status {response.status} for artifact {artifact_id}"
                )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API DELETE failed for artifact {artifact_id} ({exc.code}): {detail}"
        ) from exc


def list_artifacts(repository: str, token: str) -> list[dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    page = 1
    while True:
        payload = request_json(
            f"https://api.github.com/repos/{repository}/actions/artifacts"
            f"?per_page=100&page={page}",
            token,
        )
        batch = payload.get("artifacts")
        if not isinstance(batch, list):
            raise RuntimeError(f"unexpected artifacts response: {payload!r}")
        artifacts.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            return artifacts
        page += 1


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def select_candidates(
    artifacts: list[dict[str, object]],
    get_run,
    *,
    now: datetime,
    minimum_age_hours: float = 6.0,
    failed_minimum_age_hours: float = 24.0,
) -> list[dict[str, object]]:
    """Fail closed on unknown workflow, run, branch, age or result metadata."""
    selected: list[dict[str, object]] = []
    run_cache: dict[int, dict[str, object]] = {}
    for artifact in artifacts:
        if artifact.get("expired") is not False:
            continue
        artifact_id = artifact.get("id")
        name = artifact.get("name")
        workflow_run = artifact.get("workflow_run")
        if type(artifact_id) is not int or not isinstance(name, str):
            continue
        if not isinstance(workflow_run, dict):
            continue
        run_id = workflow_run.get("id")
        branch = workflow_run.get("head_branch")
        if type(run_id) is not int or not isinstance(branch, str) or not branch or branch == "main":
            continue
        # Name and embedded run ID are both checked before querying run metadata.
        if not any(pattern.fullmatch(name) and name.endswith(f"-{run_id}") for pattern in DISPOSABLE.values()):
            continue
        created = parse_timestamp(artifact.get("created_at"))
        if created is None:
            continue
        if run_id not in run_cache:
            run_cache[run_id] = get_run(run_id)
        run = run_cache[run_id]
        workflow = run.get("name")
        pattern = DISPOSABLE.get(workflow) if isinstance(workflow, str) else None
        if pattern is None or pattern.fullmatch(name) is None:
            continue
        if run.get("id") != run_id or run.get("head_branch") != branch:
            continue
        if run.get("status") != "completed" or run.get("event") not in {"push", "pull_request", "workflow_dispatch"}:
            continue
        conclusion = run.get("conclusion")
        if conclusion == "success":
            age_hours = minimum_age_hours
        elif conclusion == "failure":
            age_hours = failed_minimum_age_hours
        else:
            continue
        # Completion must have aged too, not merely the artifact creation.
        completed = parse_timestamp(run.get("updated_at"))
        cutoff = now - timedelta(hours=age_hours)
        if completed is None or created > cutoff or completed > cutoff:
            continue
        selected.append(artifact)
    return selected


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--minimum-age-hours", type=float, default=6.0)
    parser.add_argument("--failed-minimum-age-hours", type=float, default=24.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 0 < args.minimum_age_hours <= args.failed_minimum_age_hours:
        parser.error("minimum ages must be positive and failure age must not be shorter")
    return args


def main() -> int:
    args = parse_args()
    repository = args.repository
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not repository:
        raise SystemExit("repository is required via --repository or GITHUB_REPOSITORY")
    if not token:
        raise SystemExit("GITHUB_TOKEN or GH_TOKEN is required")

    artifacts = list_artifacts(repository, token)
    candidates = select_candidates(
        artifacts,
        lambda run_id: request_json(
            f"https://api.github.com/repos/{repository}/actions/runs/{run_id}", token
        ),
        now=datetime.now(timezone.utc),
        minimum_age_hours=args.minimum_age_hours,
        failed_minimum_age_hours=args.failed_minimum_age_hours,
    )
    reclaimed = 0
    for artifact in candidates:
        artifact_id = artifact["id"]
        size = artifact.get("size_in_bytes", 0)
        size = size if type(size) is int and size >= 0 else 0
        print(
            f"{'would delete' if args.dry_run else 'deleting'} "
            f"{artifact['name']} id={artifact_id} size={size / (1024 * 1024):.2f} MiB"
        )
        if not args.dry_run:
            delete_artifact(repository, artifact_id, token)
        reclaimed += size

    print(
        f"selected {len(candidates)} disposable diagnostic artifacts; "
        f"{'would reclaim' if args.dry_run else 'reclaimed'} "
        f"{reclaimed / (1024 * 1024):.2f} MiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
