"""The CI alert listener (#428): brings every tracking issue to the state the newest runs of its
context imply. It reconciles state rather than trusting the triggering event, so a lost,
repeated, retried or out-of-order event cannot leave a wrong state: the next run corrects it.

`ci-alerts.yml` runs it on `workflow_run` (the contexts of workflows not triggered by
`workflow_run`), on a schedule (every context, and every missed cadence) and on
`workflow_dispatch` (the same as the schedule; off `main` it is a dry run that only prints).
Any error fails the run: a missed alert is never silent.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from alert_config import ConfigurationError, Watch, load
from alert_issues import IssuesUnavailable, Tracker, escape, run_key, state_marker, token
from alert_runs import (
    Context,
    Run,
    RunsUnavailable,
    by_context,
    decisive,
    is_failing,
    jobs_of,
    list_runs,
    newest_first,
    on_main,
    required_jobs_passed,
)
from alert_staleness import push_missed, schedule_missed, trigger_missed
from github_api import GitHubApiError, GitHubClient, GitHubReader

SCOPES = {"schedule": "all", "workflow_dispatch": "all", "workflow_run": "direct"}
TRIGGER_EVENTS = ("push", "schedule", "workflow_dispatch")
_TRIAGE = (
    "Triage it in the next session: a failure is fixed for its class, with a regression test "
    "(`docs/process/engineering.md` §4)."
)


class DryRun:
    """`client` for reads; every write is printed instead of sent."""

    def __init__(self, client: GitHubReader) -> None:
        self.client = client
        self.writes: list[str] = []

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if method == "GET":
            return self.client.request(method, path, body)
        summary = (body or {}).get("title") or (body or {}).get("state") or ""
        self.writes.append(f"would {method} {path} {summary}".rstrip())
        return {"number": 0, "id": 0}

    def paginate(self, path: str) -> Iterator[Any]:
        return self.client.paginate(path)


def label(context: Context) -> str:
    """A context as log text: its ref is encoded, so it can carry no workflow command."""
    return f"{context.workflow} {context.event} {token(context.ref)}"


def run_url(repository: str, run: Run) -> str:
    return f"https://github.com/{repository}/actions/runs/{run.id}/attempts/{run.attempt}"


def describe(repository: str, context: Context, run: Run) -> str:
    return (
        f"The newest {context.event} run of `{context.workflow}` on {escape(context.ref)} "
        f"concluded **{escape(run.conclusion or run.status)}**.\n\n"
        f"- Run: {run_url(repository, run)}\n"
        f"- Commit: {run.sha[:12]}: {escape(run.title)}"
    )


class Reconciler:
    def __init__(
        self,
        client: GitHubReader,
        repository: str,
        tracker: Tracker,
        now: datetime,
        dry: bool = False,
    ) -> None:
        self.dry = dry
        self.client = client
        self.repository = repository
        self.tracker = tracker
        self.now = now
        self.actions: list[str] = []

    def failing(self, watch: Watch, context: Context, runs: Sequence[Run]) -> None:
        run = decisive(runs)
        if run is None:
            return
        issue = self.tracker.find(context, "failing")
        recorded = issue.recorded() if issue else None
        if recorded is not None and run.order <= recorded:
            return  # already accounted for, or older than what the issue records
        state = state_marker(run.id, run.number, run.attempt)
        if is_failing(run):
            text = describe(self.repository, context, run)
            if issue is None:
                title = f"ci: {context.workflow} failing ({context.event} on {escape(context.ref)})"
                body = f"{text}\n\n{_TRIAGE}\n\nThis issue closes by itself on a newer run of "
                body += (
                    f"the same workflow, event and ref that passes every required job.\n{state}\n"
                )
                opened = self.tracker.open(context, "failing", title, body)
                self.actions.append(f"opened #{opened.number}: {label(context)}")
            else:
                self.tracker.comment(issue.number, text, run_key(run.id, run.attempt))
                self.tracker.record(issue, state)
                self.actions.append(f"updated #{issue.number}: {label(context)} run {run.id}")
            return
        if issue is None:
            return
        if not required_jobs_passed(jobs_of(self.client, run), watch.required_jobs):
            self.actions.append(f"kept #{issue.number}: run {run.id} lacks a required job")
            return
        if context.event == "push" and not on_main(self.client, run.sha):
            self.actions.append(f"kept #{issue.number}: run {run.id} is not on main")
            return
        text = f"Run {run_url(self.repository, run)} passed every required job: closing."
        self.tracker.close(issue, text, run_key(run.id, run.attempt, "closed"))
        self.actions.append(f"closed #{issue.number}: {label(context)} run {run.id}")

    def stale(self, context: Context, reason: str | None, runs: Sequence[Run]) -> None:
        issue = self.tracker.find(context, "stale")
        if reason is not None and issue is None:
            title = f"ci: {context.workflow} has not run ({context.event} on {context.ref})"
            body = f"`{context.workflow}` missed its cadence: {reason}.\n\n"
            body += "This issue closes by itself when the workflow runs again.\n"
            opened = self.tracker.open(context, "stale", title, body)
            self.actions.append(f"opened #{opened.number}: {label(context)} has not run")
        elif reason is None and issue is not None:
            newest = newest_first(runs)
            key = (
                run_key(newest[0].id, newest[0].attempt, "fresh")
                if newest
                else run_key(0, 0, "fresh")
            )
            self.tracker.close(issue, "The workflow ran again: closing.", key)
            self.actions.append(f"closed #{issue.number}: {label(context)} ran again")

    def head_of_main(self) -> tuple[str, datetime]:
        try:
            commit = self.client.request("GET", "/commits/main")
            return str(commit["sha"]), datetime.fromisoformat(
                str(commit["commit"]["committer"]["date"])
            )
        except (GitHubApiError, KeyError, TypeError, ValueError) as error:
            raise RunsUnavailable(f"cannot read main's newest commit: {error}") from error

    def trigger_runs(self, watch: Watch) -> list[Run]:
        found: list[Run] = []
        for trigger in watch.triggers:
            for event in TRIGGER_EVENTS:
                proxy = Watch(trigger, (event,), ("*",), "none")
                found += [
                    r
                    for r in list_runs(self.client, proxy, event)
                    if r.repository == self.repository
                ]
        return found

    def cadence(self, watch: Watch, event: str, runs: Sequence[Run]) -> str | None:
        if watch.cadence == "push" and event == "push":
            head, committed = self.head_of_main()
            return push_missed(runs, head, committed, self.now)
        if watch.cadence == "schedule" and event == "schedule":
            return schedule_missed(runs, watch, self.now)
        if watch.cadence == "workflow_run" and event == "workflow_run":
            return trigger_missed(runs, self.trigger_runs(watch), self.now)
        return None

    def workflows(self) -> set[str]:
        """The file names of the workflows on the default branch."""
        try:
            listed = self.client.request("GET", "/actions/workflows?per_page=100")
            return {str(w["path"]).rsplit("/", 1)[-1] for w in listed["workflows"]}
        except (GitHubApiError, KeyError, TypeError) as error:
            raise RunsUnavailable(f"cannot list the workflows: {error}") from error

    def reconcile(self, watches: Sequence[Watch], scope: str) -> list[str]:
        present = self.workflows()
        for watch in watches:
            if watch.workflow not in present:
                if not self.dry:
                    raise RunsUnavailable(f"{watch.workflow} is watched but not on main")
                self.actions.append(f"skipped {watch.workflow}: not on main yet (dry run)")
                continue
            for event in watch.events:
                if event == "workflow_run" and scope != "all":
                    continue  # its runs are reconciled on the schedule only
                grouped = by_context(
                    list_runs(self.client, watch, event), watch, event, self.repository
                )
                for context in sorted(grouped, key=lambda c: c.ref):
                    self.failing(watch, context, grouped[context])
                if scope == "all" and event != "tag" and watch.cadence != "none":
                    context = Context(watch.workflow, event, "main")
                    runs = grouped.get(context, [])
                    if watch.cadence == event:
                        self.stale(context, self.cadence(watch, event, runs), runs)
        return self.actions


def main(
    environment: Mapping[str, str] = os.environ,
    connect: Callable[[str, str], GitHubReader] = GitHubClient,
) -> int:
    names = ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "EVENT_NAME", "GITHUB_REF", "ASSIGNEE")
    missing = [name for name in names if not environment.get(name)]
    if missing:
        print(f"ci-alerts: missing {', '.join(missing)}", file=sys.stderr)
        return 1
    scope = SCOPES.get(environment["EVENT_NAME"])
    if scope is None:
        print(f"ci-alerts: unexpected event {environment['EVENT_NAME']!r}", file=sys.stderr)
        return 1
    repository = environment["GITHUB_REPOSITORY"]
    client: GitHubReader = connect(environment["GITHUB_TOKEN"], repository)
    dry = environment["EVENT_NAME"] == "workflow_dispatch" and (
        environment["GITHUB_REF"] != "refs/heads/main"
    )
    if dry:
        client = DryRun(client)
    reconciler = Reconciler(
        client,
        repository,
        Tracker(client, environment["ASSIGNEE"]),
        datetime.now(UTC),
        dry=isinstance(client, DryRun),
    )
    try:
        actions = reconciler.reconcile(load(), scope)
    except (ConfigurationError, RunsUnavailable, IssuesUnavailable) as error:
        print(f"::error::ci-alerts: {error}")
        return 1
    for action in actions:
        print(f"ci-alerts: {action}")
    if isinstance(client, DryRun):
        print("ci-alerts: dry run, nothing written")
        for write in client.writes:
            print(f"ci-alerts: {write}")
    print(f"ci-alerts: {len(actions)} action(s), scope {scope}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
