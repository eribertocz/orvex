"""Runs of the watched workflows, as the CI alert listener reads them (#428): each run read from
the Actions API, the context it belongs to, and which run of a context decides its state. Run
data is untrusted text; only the fields read here leave this module."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from alert_config import API_EVENT, Watch
from github_api import GitHubApiError, GitHubReader

PAGE = 30  # the newest runs of each context read per reconciliation
FAILING = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
NO_CHANGE = frozenset({"skipped", "neutral"})


class RunsUnavailable(Exception):
    """The runs or jobs of a workflow could not be read."""


@dataclass(frozen=True, slots=True)
class Context:
    workflow: str
    event: str  # an event class of `alert_config.EVENTS`
    ref: str  # "main", or the tag


@dataclass(frozen=True, slots=True)
class Run:
    id: int
    number: int
    attempt: int
    event: str  # the API's event
    branch: str
    repository: str
    sha: str
    status: str
    conclusion: str
    created_at: datetime
    title: str  # untrusted

    @property
    def order(self) -> tuple[int, int]:
        return self.number, self.attempt


def _run(entry: dict[str, Any]) -> Run:
    head = entry.get("head_repository") or {}
    return Run(
        id=int(entry["id"]),
        number=int(entry["run_number"]),
        attempt=int(entry.get("run_attempt") or 1),
        event=str(entry["event"]),
        branch=str(entry.get("head_branch") or ""),
        repository=str(head.get("full_name") or ""),
        sha=str(entry["head_sha"]),
        status=str(entry["status"]),
        conclusion=str(entry.get("conclusion") or ""),
        created_at=datetime.fromisoformat(str(entry["created_at"])),
        title=str(entry.get("display_title") or ""),
    )


def list_runs(client: GitHubReader, watch: Watch, event: str) -> list[Run]:
    """The newest runs of `watch` for one event class (main's, or every tag's)."""
    query = f"event={API_EVENT[event]}&per_page={PAGE}"
    if event != "tag":
        query += "&branch=main"
    path = f"/actions/workflows/{watch.workflow}/runs?{query}"
    try:
        return [_run(entry) for entry in client.request("GET", path)["workflow_runs"]]
    except (GitHubApiError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise RunsUnavailable(f"cannot read the runs of {watch.workflow}: {error}") from error


def context_of(run: Run, watch: Watch, event: str, repository: str) -> Context | None:
    """The watched context of `run`, or None: a fork's run, a pull request's, another branch's
    or another event's never changes an issue."""
    if run.repository != repository or run.event != API_EVENT[event]:
        return None
    if event == "tag":
        if run.branch != "main" and any(fnmatch.fnmatchcase(run.branch, t) for t in watch.tags):
            return Context(watch.workflow, "tag", run.branch)
        return None
    return Context(watch.workflow, event, "main") if run.branch == "main" else None


def by_context(
    runs: Iterable[Run], watch: Watch, event: str, repository: str
) -> dict[Context, list[Run]]:
    grouped: dict[Context, list[Run]] = {}
    for run in runs:
        context = context_of(run, watch, event, repository)
        if context is not None:
            grouped.setdefault(context, []).append(run)
    return grouped


def newest_first(runs: Iterable[Run]) -> list[Run]:
    """Each run once (its highest attempt), newest first."""
    latest: dict[int, Run] = {}
    for run in runs:
        if run.id not in latest or run.attempt > latest[run.id].attempt:
            latest[run.id] = run
    return sorted(latest.values(), key=lambda r: r.order, reverse=True)


def decisive(runs: Sequence[Run]) -> Run | None:
    """The run that decides a context's state: the newest completed run, passing over a
    skipped or neutral one (the previous run decides) and a cancelled one that a newer run
    replaced (that newer run decides). None when no run decides yet."""
    ordered = newest_first(runs)
    for index, run in enumerate(ordered):
        if run.status != "completed" or run.conclusion in NO_CHANGE:
            continue
        if run.conclusion == "cancelled" and index > 0:
            continue
        return run
    return None


def is_failing(run: Run) -> bool:
    """Every conclusion but success, skipped and neutral is a failure (fail closed)."""
    return run.conclusion != "success" and run.conclusion not in NO_CHANGE


def jobs_of(client: GitHubReader, run: Run) -> list[tuple[str, str]]:
    """The (name, conclusion) of each job of `run`'s attempt."""
    path = f"/actions/runs/{run.id}/attempts/{run.attempt}/jobs?per_page=100"
    try:
        jobs = client.request("GET", path)["jobs"]
        return [(str(job["name"]), str(job.get("conclusion") or "")) for job in jobs]
    except (GitHubApiError, KeyError, TypeError) as error:
        raise RunsUnavailable(f"cannot read the jobs of run {run.id}: {error}") from error


def required_jobs_passed(jobs: Sequence[tuple[str, str]], patterns: Sequence[str]) -> bool:
    """Each pattern matches at least one job, and every matching job concluded success."""
    for pattern in patterns:
        matched = [c for name, c in jobs if fnmatch.fnmatchcase(name, pattern)]
        if not matched or any(conclusion != "success" for conclusion in matched):
            return False
    return True


def on_main(client: GitHubReader, sha: str) -> bool:
    """Whether commit `sha` is main's head or one of its ancestors."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RunsUnavailable(f"not a commit SHA: {sha[:80]!r}")
    try:
        status = client.request("GET", f"/compare/main...{sha}")["status"]
    except (GitHubApiError, KeyError, TypeError) as error:
        raise RunsUnavailable(f"cannot compare {sha} with main: {error}") from error
    return status in ("identical", "behind")
