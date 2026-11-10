"""#428: `ci-alerts-healthy` fails while the CI alert listener is broken or silent on main."""

from __future__ import annotations

import unittest
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from ci_health import verdict
from github_api import GitHubApiError

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
REPOSITORY = "owner/repo"


def listener_run(conclusion: str, created: str, **fields: Any) -> dict[str, Any]:
    return {
        "head_repository": {"full_name": REPOSITORY},
        "head_branch": "main",
        "status": "completed",
        "conclusion": conclusion,
        "created_at": created,
    } | fields


class Fake:
    def __init__(
        self, runs: list[dict[str, Any]], present: bool = True, fail: bool = False
    ) -> None:
        self.runs, self.present, self.fail = runs, present, fail

    def request(self, method: str, path: str, body: Any = None) -> Any:
        if self.fail:
            raise GitHubApiError("GET failed with HTTP 502")
        if path.startswith("/contents/"):
            return [{"name": "ci-alerts.yml"}] if self.present else [{"name": "python.yml"}]
        return {"workflow_runs": self.runs}

    def paginate(self, path: str) -> Iterator[Any]:
        raise AssertionError(f"unexpected listing {path}")


class HealthTests(unittest.TestCase):
    def healthy(self, fake: Fake) -> bool:
        return verdict(fake, REPOSITORY, NOW)[0]

    def test_a_fresh_success_passes(self) -> None:
        self.assertTrue(self.healthy(Fake([listener_run("success", "2026-11-10T08:00:00+00:00")])))

    def test_a_failed_run_fails(self) -> None:
        runs = [
            listener_run("failure", "2026-11-10T10:00:00+00:00"),
            listener_run("success", "2026-11-10T08:00:00+00:00"),
        ]
        self.assertFalse(self.healthy(Fake(runs)))

    def test_a_stale_run_fails(self) -> None:
        self.assertFalse(self.healthy(Fake([listener_run("success", "2026-11-10T04:00:00+00:00")])))

    def test_only_a_cancelled_run_in_the_window_fails(self) -> None:
        runs = [
            listener_run("cancelled", "2026-11-10T11:00:00+00:00"),
            listener_run("success", "2026-11-10T01:00:00+00:00"),
        ]
        self.assertFalse(self.healthy(Fake(runs)))
        self.assertFalse(self.healthy(Fake([runs[0]])))

    def test_a_cancelled_newest_run_after_a_recent_success_passes(self) -> None:
        runs = [
            listener_run("cancelled", "2026-11-10T11:00:00+00:00"),
            listener_run("success", "2026-11-10T08:00:00+00:00"),
        ]
        self.assertTrue(self.healthy(Fake(runs)))

    def test_runs_of_other_branches_or_forks_and_unfinished_runs_do_not_count(self) -> None:
        runs = [
            listener_run("success", "2026-11-10T11:00:00+00:00", head_branch="feature"),
            listener_run(
                "success", "2026-11-10T11:00:00+00:00", head_repository={"full_name": "f/r"}
            ),
            listener_run("success", "2026-11-10T11:00:00+00:00", status="in_progress"),
        ]
        self.assertFalse(self.healthy(Fake(runs)))

    def test_an_api_error_fails_closed(self) -> None:
        self.assertFalse(self.healthy(Fake([], fail=True)))

    def test_before_the_listener_reaches_main_it_passes(self) -> None:
        self.assertTrue(self.healthy(Fake([], present=False)))


if __name__ == "__main__":
    unittest.main()
