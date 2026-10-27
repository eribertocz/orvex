"""Governance check: pull requests touching governed paths must carry `owner-merge`.

The governed-path list is read from `docs/process/git-workflow.md` (section "Governed paths").
The check fails closed: if the list or the changed files cannot be read, it fails.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from github_api import GitHubApiError, GitHubClient, GitHubReader

OWNER_MERGE_LABEL = "owner-merge"
SECTION_HEADING = "### Governed paths"
MAX_LISTED_FILES = 3000


class GovernanceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class GovernedPaths:
    included: tuple[str, ...]
    excluded: tuple[str, ...]

    def is_governed(self, path: str) -> bool:
        if any(_matches(pattern, path) for pattern in self.excluded):
            return False
        return any(_matches(pattern, path) for pattern in self.included)


def _matches(pattern: str, path: str) -> bool:
    return path.startswith(pattern) if pattern.endswith("/") else path == pattern


def parse_governed_paths(markdown: str) -> GovernedPaths:
    """Read the first fenced block after the "Governed paths" heading.

    One pattern per line: the first token is the pattern, a leading `!` marks an exclusion,
    and the rest of the line is a comment.
    """
    lines = markdown.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == SECTION_HEADING)
        fence_open = next(
            index for index in range(start + 1, len(lines)) if lines[index].startswith("```")
        )
        fence_close = next(
            index for index in range(fence_open + 1, len(lines)) if lines[index].startswith("```")
        )
    except StopIteration:
        raise GovernanceError(
            "GOVERNED_PATHS_NOT_FOUND",
            f"no fenced block under '{SECTION_HEADING}' in the governance document",
        ) from None

    included: list[str] = []
    excluded: list[str] = []
    for line in lines[fence_open + 1 : fence_close]:
        tokens = line.split()
        if not tokens:
            continue
        pattern = tokens[0]
        if pattern.startswith("!"):
            excluded.append(pattern[1:])
        else:
            included.append(pattern)
    if not included:
        raise GovernanceError("GOVERNED_PATHS_NOT_FOUND", "the governed-path block is empty")
    return GovernedPaths(tuple(included), tuple(excluded))


def governed_files(changed: Iterable[str], governed: GovernedPaths) -> list[str]:
    return sorted({path for path in changed if governed.is_governed(path)})


def evaluate(changed: Sequence[str], labels: Iterable[str], governed: GovernedPaths) -> list[str]:
    """Return the governed files that block the pull request (empty when it may proceed)."""
    touched = governed_files(changed, governed)
    if touched and OWNER_MERGE_LABEL not in set(labels):
        return touched
    return []


def changed_paths(client: GitHubReader, pull_number: int) -> list[str]:
    try:
        pull = client.request("GET", f"/pulls/{pull_number}")
        files = list(client.paginate(f"/pulls/{pull_number}/files"))
    except GitHubApiError as error:
        raise GovernanceError("CHANGED_FILES_UNAVAILABLE", str(error)) from error
    try:
        expected = int(pull["changed_files"])
    except (KeyError, TypeError, ValueError) as error:
        raise GovernanceError("CHANGED_FILES_UNAVAILABLE", "no changed-file count") from error
    if expected > MAX_LISTED_FILES or len(files) != expected:
        raise GovernanceError(
            "CHANGED_FILES_UNAVAILABLE",
            f"the API listed {len(files)} of {expected} changed files",
        )
    paths: list[str] = []
    for entry in files:
        paths.append(entry["filename"])
        if entry.get("previous_filename"):
            paths.append(entry["previous_filename"])
    return paths


def current_labels(client: GitHubReader, pull_number: int) -> list[str]:
    try:
        return [label["name"] for label in client.paginate(f"/issues/{pull_number}/labels")]
    except GitHubApiError as error:
        raise GovernanceError("LABELS_UNAVAILABLE", str(error)) from error


def read_rules_document(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GovernanceError("GOVERNED_PATHS_NOT_FOUND", f"cannot read {path}: {error}") from error


def main() -> int:
    rules_document = Path(os.environ["GOVERNANCE_DOCUMENT"])
    client = GitHubClient(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    pull_number = int(os.environ["PULL_NUMBER"])
    try:
        governed = parse_governed_paths(read_rules_document(rules_document))
        blocking = evaluate(
            changed_paths(client, pull_number), current_labels(client, pull_number), governed
        )
    except GovernanceError as error:
        print(f"::error::{error}")
        return 1
    if blocking:
        print(
            f"::error::This pull request touches governed paths and lacks the "
            f"'{OWNER_MERGE_LABEL}' label. Only the Owner merges it."
        )
        for path in blocking:
            print(f"  governed: {path}")
        return 1
    print("governance: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
