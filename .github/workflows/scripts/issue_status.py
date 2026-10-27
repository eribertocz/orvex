"""Keep `status:*` labels in step with branches, pull requests and closed issues."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from github_api import GitHubApiError, GitHubClient

IN_PROGRESS = "status:in-progress"
IN_REVIEW = "status:in-review"
SPEC_READY = "status:spec-ready"
STATUS_PREFIX = "status:"

_BRANCH = re.compile(r"^[a-z]+/(?P<issue>[1-9]\d*)-[a-z0-9-]+$")
_CLOSING_REFERENCE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(?P<issue>[1-9]\d*)\b", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class LabelChange:
    issue: int
    add: tuple[str, ...]
    remove: tuple[str, ...]


def issue_from_branch(branch: str) -> int | None:
    match = _BRANCH.match(branch)
    return int(match["issue"]) if match else None


def closing_issues(pull_request_body: str | None) -> list[int]:
    found = (int(m["issue"]) for m in _CLOSING_REFERENCE.finditer(pull_request_body or ""))
    return sorted(set(found))


def changes_for_event(
    event_name: str, event: dict[str, Any], labels_of: Callable[[int], list[str]]
) -> list[LabelChange]:
    """Translate one GitHub event into label changes. `labels_of(n)` returns issue n's labels."""
    if event_name == "create" and event.get("ref_type") == "branch":
        issue = issue_from_branch(str(event.get("ref", "")))
        if issue is None:
            return []
        return [LabelChange(issue, add=(IN_PROGRESS,), remove=(SPEC_READY,))]
    if event_name == "pull_request":
        pull_request = event["pull_request"]
        if pull_request.get("draft"):
            return []
        return [
            LabelChange(issue, add=(IN_REVIEW,), remove=(IN_PROGRESS, SPEC_READY))
            for issue in closing_issues(pull_request.get("body"))
        ]
    if event_name == "issues" and event.get("action") == "closed":
        issue = int(event["issue"]["number"])
        status = tuple(label for label in labels_of(issue) if label.startswith(STATUS_PREFIX))
        return [LabelChange(issue, add=(), remove=status)] if status else []
    return []


def apply(client: GitHubClient, change: LabelChange) -> None:
    current = _labels_of(client, change.issue)
    if change.add:
        client.request("POST", f"/issues/{change.issue}/labels", {"labels": list(change.add)})
    for label in change.remove:
        if label in current:
            client.request(
                "DELETE", f"/issues/{change.issue}/labels/{urllib.parse.quote(label, safe='')}"
            )


def _labels_of(client: GitHubClient, issue: int) -> list[str]:
    return [label["name"] for label in client.paginate(f"/issues/{issue}/labels")]


def main() -> int:
    client = GitHubClient(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        event = json.load(handle)
    try:
        for change in changes_for_event(
            os.environ["GITHUB_EVENT_NAME"], event, lambda issue: _labels_of(client, issue)
        ):
            apply(client, change)
            print(f"#{change.issue}: +{list(change.add)} -{list(change.remove)}")
    except GitHubApiError as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
