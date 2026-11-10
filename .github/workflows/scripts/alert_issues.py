"""The CI alert listener's tracking issues (#428): one open issue per context and kind, found by
a fixed hidden marker, with comments keyed by run so that each is written at most once. Every
write is followed by a repair of duplicates, so concurrent or retried reconciliations converge
on one issue and one comment per key. Untrusted text is escaped before it enters an issue."""

from __future__ import annotations

import contextlib
import re
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from alert_runs import Context
from github_api import GitHubApiError, GitHubReader

LABELS = ("type:ci", "priority:p0", "module:repo")
_STATE = re.compile(r"<!-- ci-alert-state id=(\d+) number=(\d+) attempt=(\d+) -->")
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|~<>&@:])")


class IssuesUnavailable(Exception):
    """The tracking issues could not be read or written."""


def escape(text: str, limit: int = 200) -> str:
    """Untrusted text as inert Markdown on one line: no markup, link, mention or HTML."""
    flat = " ".join(text.split())[:limit]
    return _MARKDOWN.sub(r"\\\1", flat)


def token(text: str) -> str:
    """`text` with only letters, digits, `.`, `_` and `/` kept; anything else percent-encoded,
    so a marker can never be closed or forged by a ref."""
    return "".join(
        c if c.isascii() and (c.isalnum() or c in "._/") else f"%{ord(c):02X}" for c in text
    )


def marker(context: Context, kind: str) -> str:
    return (
        f"<!-- ci-alert workflow={token(context.workflow)} event={context.event} "
        f"ref={token(context.ref)} kind={kind} -->"
    )


def run_key(run_id: int, attempt: int, what: str = "run") -> str:
    return f"<!-- ci-alert-{what} id={run_id} attempt={attempt} -->"


def state_marker(run_id: int, number: int, attempt: int) -> str:
    return f"<!-- ci-alert-state id={run_id} number={number} attempt={attempt} -->"


@dataclass(frozen=True, slots=True)
class Issue:
    number: int
    body: str

    def recorded(self) -> tuple[int, int] | None:
        """The (run number, attempt) of the newest run this issue has accounted for."""
        found = _STATE.search(self.body)
        return (int(found.group(2)), int(found.group(3))) if found else None


@contextlib.contextmanager
def _failing(action: str) -> Iterator[None]:
    """Any API or response error inside becomes `IssuesUnavailable` (fail closed)."""
    try:
        yield
    except (GitHubApiError, KeyError, TypeError, ValueError) as error:
        raise IssuesUnavailable(f"cannot {action}: {error}") from error


class Tracker:
    """The tracking issues of one repository, through `client`."""

    def __init__(self, client: GitHubReader, assignee: str) -> None:
        self.client = client
        self.assignee = assignee

    def _open_issues(self) -> Iterator[dict[str, Any]]:
        label = urllib.parse.quote(LABELS[0], safe="")
        for issue in self.client.paginate(f"/issues?state=open&labels={label}&per_page=100"):
            if "pull_request" not in issue:
                yield issue

    def find(self, context: Context, kind: str) -> Issue | None:
        """The open issue of `context` and `kind`; duplicates are closed in favor of the oldest."""
        with _failing("read the tracking issues"):
            tag = marker(context, kind)
            found = sorted(
                (int(i["number"]), str(i.get("body") or ""))
                for i in self._open_issues()
                if tag in str(i.get("body") or "")
            )
            if not found:
                return None
            kept, *duplicates = found
            for number, _ in duplicates:
                self.comment(
                    number, f"Duplicate of #{kept[0]}.", f"<!-- ci-alert-duplicate of={kept[0]} -->"
                )
                self.client.request(
                    "PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "not_planned"}
                )
            return Issue(*kept)

    def open(self, context: Context, kind: str, title: str, body: str) -> Issue:
        """Open the issue of `context` and `kind`, unless one is already open."""
        with _failing("open a tracking issue"):
            existing = self.find(context, kind)
            if existing is not None:
                return existing
            full = f"{marker(context, kind)}\n{body}"
            self.client.request(
                "POST",
                "/issues",
                {
                    "title": title,
                    "body": full,
                    "labels": list(LABELS),
                    "assignees": [self.assignee],
                },
            )
            # Another reconciliation may have opened one too: the oldest is kept.
            return self.find(context, kind) or Issue(0, full)

    def comment(self, number: int, text: str, key: str) -> None:
        """Comment once per `key`; a duplicate written concurrently is deleted."""
        with _failing("comment on a tracking issue"):
            if self._keyed(number, key):
                return
            self.client.request("POST", f"/issues/{number}/comments", {"body": f"{key}\n{text}"})
            for duplicate in self._keyed(number, key)[1:]:
                self.client.request("DELETE", f"/issues/comments/{duplicate}")

    def _keyed(self, number: int, key: str) -> list[int]:
        comments = self.client.paginate(f"/issues/{number}/comments?per_page=100")
        return sorted(int(c["id"]) for c in comments if str(c.get("body") or "").startswith(key))

    def record(self, issue: Issue, state: str) -> None:
        """Replace the issue's state marker with `state`."""
        with _failing("record a run on a tracking issue"):
            body = _STATE.sub("", issue.body).rstrip("\n") + f"\n{state}\n"
            if body != issue.body:
                self.client.request("PATCH", f"/issues/{issue.number}", {"body": body})

    def close(self, issue: Issue, text: str, key: str) -> None:
        with _failing("close a tracking issue"):
            self.comment(issue.number, text, key)
            self.client.request(
                "PATCH", f"/issues/{issue.number}", {"state": "closed", "state_reason": "completed"}
            )
