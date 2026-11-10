"""A fake of the GitHub REST API for the CI alert listener's tests (#428): workflows, runs, jobs,
commits and issues held in memory, with a hook called before every request so a test can
interleave two reconciliations at any point."""

from __future__ import annotations

import copy
import re
import urllib.parse
from collections.abc import Callable, Iterator
from typing import Any

from github_api import GitHubApiError

REPOSITORY = "owner/repo"
SHA = "a" * 40
OTHER_SHA = "b" * 40


def run(
    run_id: int,
    number: int,
    workflow: str,
    conclusion: str = "failure",
    *,
    event: str = "push",
    branch: str = "main",
    attempt: int = 1,
    sha: str = SHA,
    repository: str = REPOSITORY,
    status: str = "completed",
    created: str = "2026-11-10T00:00:00+00:00",
    title: str = "a commit",
    jobs: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "run_number": number,
        "run_attempt": attempt,
        "workflow": workflow,
        "event": event,
        "head_branch": branch,
        "head_repository": {"full_name": repository},
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "created_at": created,
        "display_title": title,
        "jobs": jobs if jobs is not None else {"job": conclusion},
    }


class FakeGitHub:
    def __init__(self, workflows: list[str], runs: list[dict[str, Any]]) -> None:
        self.workflows = workflows
        self.runs = runs
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, dict[str, Any]] = {}
        self.on_main = {SHA}
        self.head = (SHA, "2026-11-10T00:00:00+00:00")
        self.hook: Callable[[], None] | None = None
        self.fail_on: str | None = None
        self._next = 1
        self.calls = 0

    def snapshot(self) -> dict[str, Any]:
        """The issues and comments, by marker and key: independent of creation order."""
        issues: dict[str, list[tuple[str, str, list[str]]]] = {}
        for number, issue in self.issues.items():
            identity = issue["body"].splitlines()[0]
            keys = sorted(
                c["body"].splitlines()[0] for c in self.comments.values() if c["issue"] == number
            )
            issues.setdefault(identity, []).append((issue["state"], issue["body"], keys))
        return {k: sorted(v) for k, v in issues.items()}

    def _number(self) -> int:
        self._next += 1
        return self._next

    def _before(self, path: str) -> None:
        self.calls += 1
        if self.fail_on and self.fail_on in path:
            raise GitHubApiError(f"GET {path} failed with HTTP 502")
        if self.hook is not None:
            self.hook()

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self._before(path)
        url = urllib.parse.urlsplit(path)
        query = dict(urllib.parse.parse_qsl(url.query))
        body = body or {}
        if method == "GET" and url.path == "/actions/workflows":
            return {"workflows": [{"path": f".github/workflows/{w}"} for w in self.workflows]}
        if match := re.fullmatch(r"/actions/workflows/([^/]+)/runs", url.path):
            found = [
                {k: v for k, v in r.items() if k not in ("workflow", "jobs")}
                for r in sorted(self.runs, key=lambda r: -r["run_number"])
                if r["workflow"] == match.group(1)
                and r["event"] == query["event"]
                and ("branch" not in query or r["head_branch"] == query["branch"])
            ]
            return {"workflow_runs": copy.deepcopy(found)}
        if match := re.fullmatch(r"/actions/runs/(\d+)/attempts/(\d+)/jobs", url.path):
            for r in self.runs:
                if r["id"] == int(match.group(1)) and r["run_attempt"] == int(match.group(2)):
                    return {"jobs": [{"name": n, "conclusion": c} for n, c in r["jobs"].items()]}
            raise GitHubApiError(f"GET {path} failed with HTTP 404")
        if match := re.fullmatch(r"/compare/main\.\.\.([0-9a-f]{40})", url.path):
            return {"status": "behind" if match.group(1) in self.on_main else "diverged"}
        if url.path == "/commits/main":
            return {"sha": self.head[0], "commit": {"committer": {"date": self.head[1]}}}
        if method == "POST" and url.path == "/issues":
            number = self._number()
            self.issues[number] = {"number": number, "state": "open", **body}
            return {"number": number}
        if match := re.fullmatch(r"/issues/(\d+)/comments", url.path):
            comment = self._number()
            self.comments[comment] = {"id": comment, "issue": int(match.group(1)), **body}
            return {"id": comment}
        if method == "DELETE" and (match := re.fullmatch(r"/issues/comments/(\d+)", url.path)):
            del self.comments[int(match.group(1))]
            return None
        if method == "PATCH" and (match := re.fullmatch(r"/issues/(\d+)", url.path)):
            self.issues[int(match.group(1))].update(body)
            return {}
        raise AssertionError(f"unexpected request {method} {path}")

    def paginate(self, path: str) -> Iterator[Any]:
        self._before(path)
        url = urllib.parse.urlsplit(path)
        if url.path == "/issues":
            items = [dict(i) for i in self.issues.values() if i["state"] == "open"]
        elif match := re.fullmatch(r"/issues/(\d+)/comments", url.path):
            items = [dict(c) for c in self.comments.values() if c["issue"] == int(match.group(1))]
        else:
            raise AssertionError(f"unexpected listing {path}")
        yield from sorted(items, key=lambda i: int(i.get("number") or i.get("id") or 0))
