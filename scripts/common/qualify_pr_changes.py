"""Decide whether an existing PR needs runtime qualification.

GitHub branch protection requires the qualification job names even when a PR
only changes documentation or the repository license. The workflows therefore
run this inexpensive gate and skip runtime jobs for those pull requests.
"""

import json
import os
import sys
import urllib.request


def needs_qualification(paths: list[str]) -> bool:
    """Fail closed except for Markdown documentation and the root license."""
    return not paths or any(
        not (path.lower().endswith(".md") or path == "LICENSE.txt")
        for path in paths
    )


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
        if len(rows) < 100:
            return paths
    raise ValueError("PR files pagination exceeded 1000 pages")


def main() -> int:
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
        qualify = needs_qualification(paths)
        print(f"PR files: {len(paths)}; runtime qualification: {qualify}")
    else:
        # Explicit dispatches and push-triggered qualification always run.
        qualify = True
        print(f"Event {event!r}: runtime qualification required")

    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"qualify={'true' if qualify else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
