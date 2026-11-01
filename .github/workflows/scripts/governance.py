"""Governance check of a pull request (`docs/process/git-workflow.md`):

1. A pull request touching governed paths must carry `owner-merge` ("Governed paths").
2. A tier A pull request (a file under "Critical paths (tier A)") needs a `## Case matrix` with
   at least one filled row.
3. A pull request of more than `SIZE_GUIDE` changed lines, not counting the files listed under
   "Not counted in a pull request's size", needs a `## Size` section that justifies it.

Every path list is read from the rules document. The check fails closed: if a list, the changed
files, their line counts or the description cannot be read, it fails.
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
CRITICAL_HEADING = "### Critical paths (tier A)"
UNCOUNTED_HEADING = "### Not counted in a pull request's size"
SIZE_GUIDE = 400
MAX_LISTED_FILES = 3000
# Text that says the size needs no justification: it does not justify a larger pull request.
NO_JUSTIFICATION = ("under the guide", "none", "n/a")


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


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    previous: str | None  # the path before a rename
    lines: int  # added plus removed lines

    def paths(self) -> list[str]:
        return [self.path] if self.previous is None else [self.path, self.previous]


def parse_governed_paths(markdown: str) -> GovernedPaths:
    return parse_path_block(markdown, SECTION_HEADING)


def parse_path_block(markdown: str, heading: str) -> GovernedPaths:
    """Read the first fenced block after `heading`.

    One pattern per line: the first token is the pattern, a leading `!` marks an exclusion,
    and the rest of the line is a comment.
    """
    lines = markdown.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading)
        fence_open = next(
            index for index in range(start + 1, len(lines)) if lines[index].startswith("```")
        )
        fence_close = next(
            index for index in range(fence_open + 1, len(lines)) if lines[index].startswith("```")
        )
    except StopIteration:
        raise GovernanceError(
            "GOVERNED_PATHS_NOT_FOUND",
            f"no fenced block under '{heading}' in the governance document",
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
        raise GovernanceError("GOVERNED_PATHS_NOT_FOUND", f"the block under '{heading}' is empty")
    return GovernedPaths(tuple(included), tuple(excluded))


def section(body: str, title: str) -> str | None:
    """The text of the `## <title>` section of a description, without HTML comments; None when
    the section is absent."""
    lines = body.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip().lower() == f"## {title}")
    except StopIteration:
        return None
    text: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        text.append(line)
    joined = "\n".join(text)
    while "<!--" in joined:
        head, _, rest = joined.partition("<!--")
        joined = head + rest.partition("-->")[2]
    return joined.strip()


def has_filled_row(text: str) -> bool:
    """Whether a Markdown table in `text` has a data row with at least one non-empty cell."""
    rows = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    # Data rows come after the header and its separator.
    return any(any(cell.strip() for cell in row.strip("|").split("|")) for row in rows[2:])


def body_findings(
    body: str,
    files: Sequence[ChangedFile],
    critical: GovernedPaths,
    uncounted: GovernedPaths,
) -> list[tuple[str, str]]:
    """(code, message) for each rule of the description the pull request breaks."""
    findings: list[tuple[str, str]] = []
    if any(critical.is_governed(path) for file in files for path in file.paths()):
        matrix = section(body, "case matrix")
        if matrix is None or not has_filled_row(matrix):
            findings.append(
                (
                    "CASE_MATRIX_MISSING",
                    "this pull request touches critical (tier A) paths: its description needs "
                    "a '## Case matrix' table with at least one filled row",
                )
            )
    counted = sum(file.lines for file in files if not uncounted.is_governed(file.path))
    if counted > SIZE_GUIDE:
        size = section(body, "size") or ""
        if not size or size.lower().rstrip(".") in NO_JUSTIFICATION:
            findings.append(
                (
                    "SIZE_NOT_JUSTIFIED",
                    f"this pull request changes {counted} counted lines (guide: {SIZE_GUIDE}): "
                    "its description needs a '## Size' section that says why it is not split",
                )
            )
    return findings


def governed_files(changed: Iterable[str], governed: GovernedPaths) -> list[str]:
    return sorted({path for path in changed if governed.is_governed(path)})


def evaluate(changed: Sequence[str], labels: Iterable[str], governed: GovernedPaths) -> list[str]:
    """Return the governed files that block the pull request (empty when it may proceed)."""
    touched = governed_files(changed, governed)
    if touched and OWNER_MERGE_LABEL not in set(labels):
        return touched
    return []


def changed_paths(client: GitHubReader, pull_number: int) -> list[str]:
    return [path for file in changed_files(client, pull_number) for path in file.paths()]


def changed_files(client: GitHubReader, pull_number: int) -> list[ChangedFile]:
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
    changed: list[ChangedFile] = []
    try:
        for entry in files:
            lines = int(entry["additions"]) + int(entry["deletions"])
            changed.append(ChangedFile(entry["filename"], entry.get("previous_filename"), lines))
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise GovernanceError("CHANGED_FILES_UNAVAILABLE", "malformed file entry") from error
    return changed


def pull_body(client: GitHubReader, pull_number: int) -> str:
    try:
        body = client.request("GET", f"/pulls/{pull_number}").get("body")
    except (GitHubApiError, AttributeError) as error:
        raise GovernanceError("DESCRIPTION_UNAVAILABLE", str(error)) from error
    return body if isinstance(body, str) else ""


def current_labels(client: GitHubReader, pull_number: int) -> list[str]:
    try:
        return [label["name"] for label in client.paginate(f"/issues/{pull_number}/labels")]
    except (GitHubApiError, KeyError, TypeError) as error:
        raise GovernanceError("LABELS_UNAVAILABLE", str(error)) from error


def read_rules_document(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GovernanceError("GOVERNED_PATHS_NOT_FOUND", f"cannot read {path}: {error}") from error


def main() -> int:
    try:
        rules_document = Path(os.environ["GOVERNANCE_DOCUMENT"])
        client = GitHubClient(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
        pull_number = int(os.environ["PULL_NUMBER"])
    except (KeyError, ValueError) as error:
        print(f"::error::GOVERNANCE_MISCONFIGURED: missing or invalid environment value {error}")
        return 1
    try:
        document = read_rules_document(rules_document)
        governed = parse_governed_paths(document)
        critical = parse_path_block(document, CRITICAL_HEADING)
        uncounted = parse_path_block(document, UNCOUNTED_HEADING)
        files = changed_files(client, pull_number)
        paths = [path for file in files for path in file.paths()]
        blocking = evaluate(paths, current_labels(client, pull_number), governed)
        broken = body_findings(pull_body(client, pull_number), files, critical, uncounted)
    except GovernanceError as error:
        print(f"::error::{error}")
        return 1
    for code, message in broken:
        print(f"::error::{code}: {message}")
    if blocking:
        print(
            f"::error::This pull request touches governed paths and lacks the "
            f"'{OWNER_MERGE_LABEL}' label. Only the Owner merges it."
        )
        for path in blocking:
            print(f"  governed: {path}")
        return 1
    if broken:
        return 1
    print("governance: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
