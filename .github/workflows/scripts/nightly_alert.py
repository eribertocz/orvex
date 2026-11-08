"""Open or update the one tracking issue of failing nightly runs (#377, #373 §6).

`property-nightly.yml` runs it when its run fails. It looks for the open issue with `TITLE`
among the open issues labeled `type:test`: when one exists it comments the failed run on it and
keeps it assigned; otherwise it opens it, labeled and assigned to the Agent, which works through
the Owner's account (`ASSIGNEE`). It never opens a second issue. Any API failure fails the job,
so a missed alert shows as a failed step.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
from collections.abc import Mapping

from github_api import GitHubApiError, GitHubClient, GitHubReader

TITLE = "Nightly run failing"
LABELS = ("type:test", "priority:p0", "module:repo")
_TRIAGE = (
    "Triage it in the next session. A security, exactness, contract or corpus failure is fixed "
    "for its class, and its counterexample is committed as a regression test in the `pr` and "
    "`full` profiles, never only in nightly (`tools/ci/checks.toml` `[profiles]`, "
    "`docs/process/engineering.md` §4)."
)


def _open_issue(client: GitHubReader) -> int | None:
    label = urllib.parse.quote(LABELS[0], safe="")
    for issue in client.paginate(f"/issues?state=open&labels={label}"):
        if "pull_request" not in issue and issue.get("title") == TITLE:
            return int(issue["number"])
    return None


def alert(client: GitHubReader, run_url: str, assignee: str) -> str:
    """Comment on the open tracking issue, or open it; returns what was done."""
    failed = f"The nightly run {run_url} failed."
    number = _open_issue(client)
    if number is not None:
        client.request("POST", f"/issues/{number}/comments", {"body": f"{failed}\n\n{_TRIAGE}"})
        client.request("POST", f"/issues/{number}/assignees", {"assignees": [assignee]})
        return f"updated #{number}"
    body = (
        f"{failed}\n\n{_TRIAGE}\n\nLater failures are added here as comments; close this issue "
        "when the nightly runs pass again."
    )
    created = client.request(
        "POST",
        "/issues",
        {"title": TITLE, "body": body, "labels": list(LABELS), "assignees": [assignee]},
    )
    return f"opened #{created['number']}"


def main(environment: Mapping[str, str] = os.environ) -> int:
    names = ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "RUN_URL", "ASSIGNEE")
    missing = [name for name in names if not environment.get(name)]
    if missing:
        print(f"nightly-alert: missing {', '.join(missing)}", file=sys.stderr)
        return 1
    client = GitHubClient(environment["GITHUB_TOKEN"], environment["GITHUB_REPOSITORY"])
    try:
        print(f"nightly-alert: {alert(client, environment['RUN_URL'], environment['ASSIGNEE'])}")
    except GitHubApiError as error:
        print(f"nightly-alert: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
