"""Known gaps whose issue is closed (#427): each `expected-fail` entry of a normative manifest,
read as the pull request's head has it, must link an issue that is still open. The manifest is
fetched as data through the API, never checked out or run. The check fails closed."""

from __future__ import annotations

import base64
import tomllib
from typing import Any

from github_api import GitHubApiError, GitHubReader

# Each normative manifest that may list known gaps: (folder, file name).
MANIFESTS = (("conformance/semantic-corpus", "expected-fail.toml"),)


class KnownGapsUnavailable(Exception):
    """The manifest or an issue it links could not be read."""


def head_sha(client: GitHubReader, pull_number: int) -> str:
    try:
        return str(client.request("GET", f"/pulls/{pull_number}")["head"]["sha"])
    except (GitHubApiError, KeyError, TypeError) as error:
        raise KnownGapsUnavailable(f"no head commit: {error}") from error


def manifest_text(client: GitHubReader, folder: str, name: str, ref: str) -> str | None:
    """The manifest at `ref`, or None when its folder holds no such file."""
    try:
        listing = client.request("GET", f"/contents/{folder}?ref={ref}")
        if not isinstance(listing, list):
            raise KnownGapsUnavailable(f"{folder} is not a folder at {ref}")
        if name not in {str(entry.get("name")) for entry in listing}:
            return None
        document: Any = client.request("GET", f"/contents/{folder}/{name}?ref={ref}")
        return base64.b64decode(document["content"]).decode("utf-8")
    except (GitHubApiError, KeyError, TypeError, AttributeError, ValueError) as error:
        raise KnownGapsUnavailable(f"cannot read {folder}/{name}: {error}") from error


def linked_issues(text: str, where: str) -> dict[str, int]:
    """Each listed case with its issue; an entry without an integer issue is unreadable."""
    try:
        listed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise KnownGapsUnavailable(f"{where}: {error}") from error
    issues = {}
    for case, entry in listed.items():
        issue = entry.get("issue") if isinstance(entry, dict) else None
        if not isinstance(issue, int) or isinstance(issue, bool):
            raise KnownGapsUnavailable(f"{where}: {case} has no issue number")
        issues[case] = issue
    return issues


def closed_gaps(client: GitHubReader, pull_number: int) -> list[str]:
    """One line per known gap whose linked issue is not open."""
    ref = head_sha(client, pull_number)
    found = []
    for folder, name in MANIFESTS:
        text = manifest_text(client, folder, name, ref)
        if text is None:
            continue
        for case, issue in linked_issues(text, f"{folder}/{name}").items():
            try:
                linked = client.request("GET", f"/issues/{issue}")
                state, pull = linked["state"], "pull_request" in linked
            except (GitHubApiError, KeyError, TypeError) as error:
                raise KnownGapsUnavailable(f"cannot read issue #{issue}: {error}") from error
            if pull:
                found.append(
                    f"{folder}/{name}: {case} links #{issue}, a pull request, not an issue"
                )
            elif state != "open":
                found.append(f"{folder}/{name}: {case} links #{issue}, which is {state}")
    return found
