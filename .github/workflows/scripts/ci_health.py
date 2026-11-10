"""The `ci-alerts-healthy` check (#428): a pull request fails while the CI alert listener is
broken on `main`, so a silent listener is seen at the next pull request. It fails when the
newest completed, non-cancelled run of `ci-alerts.yml` on `main` did not succeed or started more
than `WINDOW` ago. Before the listener reaches `main` it passes with a notice, and so it does on
any event but a pull request (a push to main would report the listener back to the listener).
Fail closed: an unknown event is checked."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from github_api import GitHubApiError, GitHubClient, GitHubReader

LISTENER = "ci-alerts.yml"
WINDOW = timedelta(hours=7)


def verdict(client: GitHubReader, repository: str, now: datetime) -> tuple[bool, str]:
    """(healthy, why)."""
    try:
        listed = client.request("GET", "/contents/.github/workflows?ref=main")
        if LISTENER not in {str(entry.get("name")) for entry in listed}:
            return True, f"{LISTENER} is not on main yet"
        runs = client.request("GET", f"/actions/workflows/{LISTENER}/runs?branch=main&per_page=50")[
            "workflow_runs"
        ]
        completed = sorted(
            (
                (datetime.fromisoformat(str(r["created_at"])), str(r.get("conclusion") or ""))
                for r in runs
                if (r.get("head_repository") or {}).get("full_name") == repository
                and r.get("head_branch") == "main"
                and r.get("status") == "completed"
                and r.get("conclusion") != "cancelled"
            ),
            reverse=True,
        )
    except (GitHubApiError, KeyError, TypeError, ValueError, AttributeError) as error:
        return False, f"cannot read the listener's runs: {error}"
    if not completed:
        return False, f"{LISTENER} has no completed run on main"
    started, conclusion = completed[0]
    if conclusion != "success":
        return False, f"the newest {LISTENER} run on main concluded {conclusion}"
    if now - started > WINDOW:
        return False, f"the newest {LISTENER} run on main started {started.isoformat()}"
    return True, f"the newest {LISTENER} run on main succeeded at {started.isoformat()}"


def main(environment: Mapping[str, str] = os.environ) -> int:
    event = environment.get("GITHUB_EVENT_NAME", "")
    if event and event != "pull_request":
        print(f"ci-alerts-healthy: checked on pull requests only, not on {event}")
        return 0
    try:
        repository = environment["GITHUB_REPOSITORY"]
        client = GitHubClient(environment["GITHUB_TOKEN"], repository)
    except KeyError as error:
        print(f"::error::ci-alerts-healthy: missing {error}")
        return 1
    healthy, why = verdict(client, repository, datetime.now(UTC))
    print(f"ci-alerts-healthy: {why}" if healthy else f"::error::ci-alerts-healthy: {why}")
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
