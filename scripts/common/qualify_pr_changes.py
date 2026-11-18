"""Select the producer platforms affected by a pull request.

Classify only known platform-owned or consumer-only paths narrowly. Unknown
paths, shared runtime inputs, and an unavailable PR file list fail closed to
qualification on all platforms. Workflow dispatches and main pushes always
qualify, preserving explicit release evidence.
"""

import json
import os
import sys
import urllib.request


PLATFORMS = frozenset({"windows", "linux", "macos"})

# This is intentionally an *allowlist* of paths that are safe not to qualify.
# Keep ambiguous paths out of this table: unknowns require the full matrix.
CONSUMER_ONLY_PREFIXES = (
    "consumer-kit/",
    "reference-bridge/",
    "tests/reference-bridge/",
    "locks/consumer/",
    "scripts/consumer/",
)
CONSUMER_ONLY_FILES = frozenset({
    ".github/workflows/qualify-reference-bridge.yml",
    ".github/workflows/publish-reference-bridge.yml",
    ".github/workflows/actions-storage-cleanup.yml",
    ".github/workflows/validate.yml",
    "scripts/common/cleanup_actions_artifacts.py",
    "tests/common/test_cleanup_actions_artifacts.py",
    "tests/common/test_consumer_release_stage.py",
})

PLATFORM_PREFIXES = {
    platform: (
        f"scripts/{platform}/",
        f"locks/{platform}/",
        f"runtime/{platform}/",
        f"tests/common/test_{platform}_",
    )
    for platform in PLATFORMS
}
PLATFORM_FILES = {
    "windows": {".github/workflows/qualify-windows-core-numpy.yml",
                ".github/workflows/qualify-windows-fenics.yml"},
    "linux": {".github/workflows/qualify-linux-core-numpy.yml",
              ".github/workflows/qualify-linux-fenics.yml"},
    "macos": {".github/workflows/qualify-macos-core-numpy.yml",
              ".github/workflows/qualify-macos-fenics.yml",
              "runtime/macos_fenics_embed_runtime.py"},
}


def affected_platforms(paths: list[str]) -> frozenset[str]:
    """Return affected platforms, conservatively treating unknown paths as shared."""
    if not paths:
        return PLATFORMS
    affected: set[str] = set()
    for path in paths:
        if path.lower().endswith(".md") or path == "LICENSE.txt":
            continue
        if path in CONSUMER_ONLY_FILES or path.startswith(CONSUMER_ONLY_PREFIXES):
            continue
        matched = {
            platform
            for platform in PLATFORMS
            if path in PLATFORM_FILES[platform]
            or path.startswith(PLATFORM_PREFIXES[platform])
        }
        if not matched:
            return PLATFORMS
        affected.update(matched)
    return frozenset(affected)


def needs_qualification(paths: list[str], platform: str | None = None) -> bool:
    """Select one platform, or any platform if none is supplied (legacy callers)."""
    if platform is not None and platform not in PLATFORMS:
        raise ValueError(f"unknown qualification platform: {platform!r}")
    impacted = affected_platforms(paths)
    return bool(impacted) if platform is None else platform in impacted


def pull_request_files(repository: str, number: str, token: str) -> list[str]:
    paths: list[str] = []
    for page in range(1, 1001):
        url = (
            f"https://api.github.com/repos/{repository}/pulls/{number}/files"
            f"?per_page=100&page={page}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "precsim-qualification-gate",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            rows = json.load(response)
        if not isinstance(rows, list):
            raise ValueError("GitHub PR files response was not a list")
        for row in rows:
            name = row.get("filename") if isinstance(row, dict) else None
            if not isinstance(name, str) or not name:
                raise ValueError("GitHub PR files response has no filename")
            paths.append(name)
            # A rename can move a runtime input into a consumer-only path.
            # Classify both sides so deletion of the old input is never skipped.
            if row.get("status") == "renamed":
                previous = row.get("previous_filename")
                if not isinstance(previous, str) or not previous:
                    raise ValueError("renamed PR file has no previous_filename")
                paths.append(previous)
        if len(rows) < 100:
            return paths
    raise ValueError("PR files pagination exceeded 1000 pages")


def main() -> int:
    platform = os.environ["QUALIFICATION_PLATFORM"]
    if platform not in PLATFORMS:
        raise ValueError(f"invalid QUALIFICATION_PLATFORM: {platform!r}")
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event == "pull_request":
        event_path = os.environ["GITHUB_EVENT_PATH"]
        with open(event_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        paths = pull_request_files(
            os.environ["GITHUB_REPOSITORY"],
            str(payload["number"]),
            os.environ["GH_TOKEN"],
        )
        impacted = affected_platforms(paths)
        qualify = platform in impacted
        print(
            f"PR files: {len(paths)}; affected platforms: {','.join(sorted(impacted)) or 'none'};"
            f" {platform} qualification: {qualify}"
        )
    else:
        # Explicit dispatches and push-triggered qualification always run.
        qualify = True
        print(f"Event {event!r}: runtime qualification required")

    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"qualify={'true' if qualify else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
