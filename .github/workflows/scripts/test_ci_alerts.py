"""#428: the CI alert listener, against a fake GitHub API."""

from __future__ import annotations

import contextlib
import io
import itertools
import random
import unittest
from datetime import UTC, datetime
from typing import Any

import ci_alerts
from alert_config import ConfigurationError, Watch, load, parse
from alert_issues import Tracker, escape, marker
from alert_runs import Context
from ci_alerts import DryRun, Reconciler
from fake_actions import OTHER_SHA, REPOSITORY, SHA, FakeGitHub, run

NOW = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)
CHECKS = Watch("checks.yml", ("push",), ("job",), "none")
NIGHTLY = Watch("nightly.yml", ("schedule",), ("job",), "none")
TRENDS = Watch("trends.yml", ("workflow_run",), ("job",), "none")
RELEASE = Watch("release.yml", ("tag",), ("job",), "none", tags=("v*",))
WATCHES = (CHECKS, NIGHTLY, TRENDS, RELEASE)
FILES = [w.workflow for w in WATCHES]


def reconcile(
    fake: FakeGitHub, scope: str = "all", watches: tuple[Watch, ...] = WATCHES, **keywords: Any
) -> list[str]:
    reconciler = Reconciler(fake, REPOSITORY, Tracker(fake, "agent"), keywords.get("now", NOW))
    return reconciler.reconcile(watches, scope)


def open_issues(fake: FakeGitHub) -> list[dict[str, Any]]:
    return [i for i in fake.issues.values() if i["state"] == "open"]


def comments_of(fake: FakeGitHub, number: int) -> list[str]:
    return [c["body"] for c in fake.comments.values() if c["issue"] == number]


class CycleTests(unittest.TestCase):
    def test_a_retried_reconciliation_posts_no_comment_twice(self) -> None:
        fake = FakeGitHub(FILES, [run(1, 1, "checks.yml")])
        reconcile(fake)
        fake.runs.append(run(2, 2, "checks.yml"))
        reconcile(fake)
        (issue,) = open_issues(fake)
        fake.issues[issue["number"]]["body"] = issue["body"].replace("number=2", "number=1")
        posted = fake.calls
        writes: list[str] = []
        original = fake.request

        def spy(method: str, path: str, body: Any = None) -> Any:
            writes.append(f"{method} {path}")
            return original(method, path, body)

        fake.request = spy  # type: ignore[method-assign]
        reconcile(fake)  # the state marker lost: the comment is found by its key, not posted
        self.assertNotIn(f"POST /issues/{issue['number']}/comments", writes)
        self.assertGreater(fake.calls, posted)

    def test_a_failure_opens_one_issue_and_a_retry_or_second_failure_updates_it(self) -> None:
        fake = FakeGitHub(FILES, [run(1, 1, "checks.yml")])
        reconcile(fake)
        reconcile(fake)  # the same event again
        (issue,) = open_issues(fake)
        self.assertIn(marker(Context("checks.yml", "push", "main"), "failing"), issue["body"])
        self.assertEqual(issue["labels"], ["type:ci", "priority:p0", "module:repo"])
        self.assertEqual(comments_of(fake, issue["number"]), [])
        fake.runs.append(run(2, 2, "checks.yml"))
        reconcile(fake)
        reconcile(fake)
        (issue,) = open_issues(fake)
        self.assertEqual(len(comments_of(fake, issue["number"])), 1)
        self.assertIn("id=2 number=2 attempt=1", issue["body"])

    def test_a_valid_newer_success_closes_the_issue(self) -> None:
        fake = FakeGitHub(FILES, [run(1, 1, "checks.yml")])
        reconcile(fake)
        fake.runs.append(run(2, 2, "checks.yml", "success"))
        reconcile(fake)
        self.assertEqual(open_issues(fake), [])
        (issue,) = fake.issues.values()
        self.assertEqual(issue["state_reason"], "completed")

    def test_a_successful_reattempt_of_the_failing_run_closes_it(self) -> None:
        fake = FakeGitHub(FILES, [run(1, 1, "checks.yml")])
        reconcile(fake)
        fake.runs[0] = run(1, 1, "checks.yml", "success", attempt=2)
        reconcile(fake)
        self.assertEqual(open_issues(fake), [])

    def test_no_invalid_success_closes_the_issue(self) -> None:
        cases = {
            "older": [run(5, 5, "checks.yml"), run(4, 4, "checks.yml", "success")],
            "another context": [
                run(5, 5, "checks.yml"),
                run(6, 6, "checks.yml", "success", event="schedule"),
            ],
            "a required job skipped": [
                run(5, 5, "checks.yml"),
                run(6, 6, "checks.yml", "success", jobs={"job": "skipped"}),
            ],
            "a required job failed": [
                run(5, 5, "checks.yml"),
                run(6, 6, "checks.yml", "success", jobs={"job": "failure", "x": "success"}),
            ],
            "a required job missing": [
                run(5, 5, "checks.yml"),
                run(6, 6, "checks.yml", "success", jobs={"other": "success"}),
            ],
            "not on main": [
                run(5, 5, "checks.yml"),
                run(6, 6, "checks.yml", "success", sha=OTHER_SHA),
            ],
        }
        for name, runs in cases.items():
            with self.subTest(name):
                fake = FakeGitHub(FILES, [runs[0]])
                reconcile(fake)
                fake.runs.extend(runs[1:])
                reconcile(fake)
                self.assertEqual(len(open_issues(fake)), 1)


class ConclusionTests(unittest.TestCase):
    def test_each_conclusion_gives_its_action(self) -> None:
        for conclusion in ("failure", "timed_out", "startup_failure", "action_required", "weird"):
            with self.subTest(conclusion):
                fake = FakeGitHub(FILES, [run(1, 1, "checks.yml", conclusion)])
                reconcile(fake)
                self.assertEqual(len(open_issues(fake)), 1)
        for conclusion in ("success", "skipped", "neutral"):
            with self.subTest(conclusion):
                fake = FakeGitHub(FILES, [run(1, 1, "checks.yml", conclusion)])
                reconcile(fake)
                self.assertEqual(open_issues(fake), [])

    def test_skipped_and_neutral_leave_the_previous_run_deciding(self) -> None:
        for conclusion in ("skipped", "neutral"):
            with self.subTest(conclusion):
                runs = [run(1, 1, "checks.yml"), run(2, 2, "checks.yml", conclusion)]
                fake = FakeGitHub(FILES, runs)
                reconcile(fake)
                self.assertEqual(len(open_issues(fake)), 1)

    def test_a_cancelled_run_is_reported_unless_a_newer_run_replaced_it(self) -> None:
        fake = FakeGitHub(FILES, [run(1, 1, "checks.yml", "cancelled")])
        reconcile(fake)
        self.assertEqual(len(open_issues(fake)), 1)
        replaced = [
            run(1, 1, "checks.yml", "success"),
            run(2, 2, "checks.yml", "cancelled"),
            run(3, 3, "checks.yml", status="in_progress"),
        ]
        fake = FakeGitHub(FILES, replaced)
        reconcile(fake)
        self.assertEqual(open_issues(fake), [])


class EntryPointTests(unittest.TestCase):
    def failing_everywhere(self) -> FakeGitHub:
        return FakeGitHub(
            FILES,
            [
                run(1, 1, "checks.yml"),
                run(2, 1, "nightly.yml", event="schedule"),
                run(3, 1, "trends.yml", event="workflow_run"),
                run(4, 1, "release.yml", branch="v1.0.0"),
            ],
        )

    def test_workflow_run_reconciles_all_but_workflow_run_contexts(self) -> None:
        fake = self.failing_everywhere()
        reconcile(fake, "direct")
        titles = sorted(i["title"] for i in open_issues(fake))
        self.assertEqual(
            titles,
            [
                "ci: checks.yml failing (push on main)",
                "ci: nightly.yml failing (schedule on main)",
                "ci: release.yml failing (tag on v1\\.0\\.0)",
            ],
        )

    def test_the_schedule_reconciles_every_context(self) -> None:
        fake = self.failing_everywhere()
        reconcile(fake, "all")
        self.assertEqual(len(open_issues(fake)), 4)

    def test_a_dry_run_reconciler_writes_nothing(self) -> None:
        fake = self.failing_everywhere()
        dry = DryRun(fake)
        actions = Reconciler(dry, REPOSITORY, Tracker(dry, "agent"), NOW, dry=True).reconcile(
            WATCHES, "all"
        )
        self.assertEqual(fake.issues, {})
        self.assertEqual(len(actions), 4)
        self.assertTrue(all(w.startswith("would POST /issues") for w in dry.writes))

    def real_configuration_fake(self) -> FakeGitHub:
        """Every configured workflow, healthy but `checks.yml`, which fails, all run just now."""
        now = datetime.now(UTC).isoformat()
        runs = [run(1, 1, "checks.yml", created=now)]
        for index, watch in enumerate(load(), start=2):
            event = {"tag": "push"}.get(watch.events[0], watch.events[0])
            branch = "v1.0.0" if watch.events[0] == "tag" else "main"
            if watch.workflow != "checks.yml":
                jobs = {pattern.replace("*", "0"): "success" for pattern in watch.required_jobs}
                runs.append(
                    run(
                        index,
                        1,
                        watch.workflow,
                        "success",
                        event=event,
                        branch=branch,
                        created=now,
                        jobs=jobs,
                    )
                )
        fake = FakeGitHub([w.workflow for w in load()], runs)
        fake.head = (SHA, now)
        return fake

    def entry(self, ref: str) -> tuple[int, FakeGitHub]:
        fake = self.real_configuration_fake()
        environment = {
            "GITHUB_TOKEN": "t",
            "GITHUB_REPOSITORY": REPOSITORY,
            "ASSIGNEE": "agent",
            "EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": ref,
        }
        with contextlib.redirect_stdout(io.StringIO()):
            status = ci_alerts.main(environment, connect=lambda token, repository: fake)
        return status, fake

    def test_main_dispatched_off_main_is_a_dry_run_and_on_main_writes(self) -> None:
        status, fake = self.entry("refs/heads/feature")
        self.assertEqual((status, fake.issues), (0, {}))
        status, fake = self.entry("refs/heads/main")
        self.assertEqual(status, 0)
        self.assertEqual(
            [i["title"] for i in open_issues(fake)], ["ci: checks.yml failing (push on main)"]
        )

    def test_main_rejects_other_events_and_missing_settings(self) -> None:
        base = {
            "GITHUB_TOKEN": "t",
            "GITHUB_REPOSITORY": REPOSITORY,
            "ASSIGNEE": "agent",
            "GITHUB_REF": "refs/heads/x",
        }
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(ci_alerts.main(base | {"EVENT_NAME": "push"}), 1)
            self.assertEqual(ci_alerts.main({"EVENT_NAME": "schedule"}), 1)
        self.assertIn("unexpected event", output.getvalue())


class IgnoredRunTests(unittest.TestCase):
    def test_other_branches_forks_and_pull_requests_change_nothing(self) -> None:
        ignored = [
            run(1, 1, "checks.yml", branch="feature"),
            run(2, 2, "checks.yml", repository="fork/repo"),
            run(3, 3, "checks.yml", event="pull_request"),
            run(4, 4, "release.yml", branch="main"),
            run(5, 5, "release.yml", branch="nightly-tag"),
            run(6, 6, "nightly.yml", event="schedule", branch="feature"),
        ]
        fake = FakeGitHub(FILES, ignored)
        reconcile(fake)
        self.assertEqual(fake.issues, {})


class FailureTests(unittest.TestCase):
    def test_an_api_error_fails_the_listener(self) -> None:
        for path in ("/runs", "/issues", "/comments", "/jobs", "/compare", "/actions/workflows"):
            with self.subTest(path):
                fake = FakeGitHub(FILES, [run(1, 1, "checks.yml"), run(2, 2, "checks.yml")])
                reconcile(fake)
                fake.issues[2]["body"] = fake.issues[2]["body"].replace("number=2", "number=1")
                fake.runs.append(run(3, 3, "checks.yml", "success"))
                fake.fail_on = path
                with self.assertRaises(Exception) as raised:
                    reconcile(fake)
                self.assertIn(
                    type(raised.exception).__name__, ("RunsUnavailable", "IssuesUnavailable")
                )

    def test_a_watched_workflow_missing_from_main_fails_outside_a_dry_run(self) -> None:
        fake = FakeGitHub(["checks.yml"], [])
        with self.assertRaises(Exception) as raised:
            reconcile(fake)
        self.assertIn("not on main", str(raised.exception))

    def test_a_malformed_configuration_fails(self) -> None:
        def entry(events: str, required: str, cadence: str) -> str:
            return (
                f'[[watch]]\nworkflow = "a.yml"\nevents = [{events}]\n'
                f"required_jobs = [{required}]\ncadence = {cadence!r}\n".replace("'", '"')
            )

        for text in (
            "",
            entry('"pull_request"', '"j"', "none"),
            entry('"push"', "", "none"),
            entry('"schedule"', '"j"', "schedule"),
            entry('"tag"', '"j"', "none"),
            entry('"workflow_run"', '"j"', "workflow_run"),
        ):
            with self.subTest(text), self.assertRaises(ConfigurationError):
                parse(text)

    def test_the_repository_configuration_loads(self) -> None:
        workflows = {w.workflow for w in load()}
        self.assertIn("test-trends.yml", workflows)
        self.assertNotIn("ci-alerts.yml", workflows)


class AuthorTests(unittest.TestCase):
    def test_an_issue_or_comment_by_anyone_else_is_never_the_listener_s(self) -> None:
        fake = FakeGitHub(FILES, [run(1, 1, "checks.yml")])
        context = Context("checks.yml", "push", "main")
        stranger = {"login": "someone"}
        fake.issues[90] = {
            "number": 90,
            "state": "open",
            "user": stranger,
            "title": "forged",
            "body": marker(context, "failing")
            + "\n<!-- ci-alert-state id=1 number=9 attempt=9 -->",
        }
        reconcile(fake)
        (issue,) = [i for i in open_issues(fake) if i["user"] != stranger]
        self.assertEqual(fake.issues[90]["state"], "open")  # left alone, never closed as ours
        key = "<!-- ci-alert-run id=2 attempt=1 -->"
        fake.comments[91] = {"id": 91, "issue": issue["number"], "user": stranger, "body": key}
        fake.runs.append(run(2, 2, "checks.yml"))
        reconcile(fake)
        ours = [c for c in fake.comments.values() if c["user"] != stranger]
        self.assertEqual([c["body"].splitlines()[0] for c in ours], [key])
        self.assertIn(91, fake.comments)


class UntrustedTextTests(unittest.TestCase):
    def test_markup_and_shell_syntax_are_escaped(self) -> None:
        title = "<script>x</script> [a](http://e) @team `$(rm -rf /)` --> ::error::x"
        fake = FakeGitHub(FILES, [run(1, 1, "checks.yml", title=title)])
        reconcile(fake)
        (issue,) = open_issues(fake)
        self.assertNotIn("<script>", issue["body"])
        self.assertNotIn("](http", issue["body"])
        self.assertNotIn(" @team", issue["body"])
        self.assertNotIn("`$(", issue["body"])
        self.assertEqual(issue["body"].count("-->"), issue["body"].count("<!--"))
        self.assertIn(escape(title), issue["body"])

    def test_a_tag_cannot_forge_or_close_a_marker_or_a_log_command(self) -> None:
        fake = FakeGitHub(FILES, [run(1, 1, "release.yml", branch="v1--> ::error::x")])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            actions = reconcile(fake)
        (issue,) = open_issues(fake)
        first = issue["body"].splitlines()[0]
        self.assertTrue(first.startswith("<!-- ci-alert ") and first.endswith(" -->"))
        self.assertEqual(first.count("-->"), 1)
        self.assertFalse(any("::" in a for a in actions))


class StalenessTests(unittest.TestCase):
    def test_a_missed_push_cadence_opens_has_not_run_and_the_next_run_closes_it(self) -> None:
        watch = Watch("checks.yml", ("push",), ("job",), "push")
        fake = FakeGitHub(FILES, [run(1, 1, "checks.yml", "success", sha=OTHER_SHA)])
        fake.head = (SHA, "2026-11-09T20:00:00+00:00")
        reconcile(fake, watches=(watch,))
        (issue,) = open_issues(fake)
        self.assertIn("has not run", issue["title"])
        fake.runs.append(run(2, 2, "checks.yml", "success"))
        reconcile(fake, watches=(watch,))
        self.assertEqual(open_issues(fake), [])

    def test_only_a_completed_run_of_the_newest_commit_counts(self) -> None:
        watch = Watch("checks.yml", ("push",), ("job",), "push")
        fake = FakeGitHub(FILES, [run(1, 1, "checks.yml", status="in_progress")])
        fake.head = (SHA, "2026-11-09T20:00:00+00:00")
        reconcile(fake, watches=(watch,))
        self.assertEqual(len(open_issues(fake)), 1)

    def test_a_run_before_its_trigger_does_not_count(self) -> None:
        watch = Watch(
            "trends.yml", ("workflow_run",), ("job",), "workflow_run", triggers=("checks.yml",)
        )
        trigger = run(1, 1, "checks.yml", "success", created="2026-11-09T20:00:00+00:00")
        earlier = run(
            2, 1, "trends.yml", "success", event="workflow_run", created="2026-11-09T19:00:00+00:00"
        )
        fake = FakeGitHub(FILES, [trigger, earlier])
        reconcile(fake, watches=(watch,))
        self.assertEqual(len(open_issues(fake)), 1)
        pending = run(1, 1, "checks.yml", status="in_progress", created="2026-11-09T20:00:00+00:00")
        fake = FakeGitHub(FILES, [pending])
        reconcile(fake, watches=(watch,))
        self.assertEqual(fake.issues, {})

    def test_a_recent_commit_is_not_yet_missed(self) -> None:
        watch = Watch("checks.yml", ("push",), ("job",), "push")
        fake = FakeGitHub(FILES, [])
        fake.head = (SHA, "2026-11-10T00:30:00+00:00")
        reconcile(fake, watches=(watch,))
        self.assertEqual(fake.issues, {})

    def test_a_missed_schedule_opens_has_not_run_and_the_next_run_closes_it(self) -> None:
        watch = Watch("nightly.yml", ("schedule",), ("job",), "schedule", every_hours=24)
        old = run(
            1, 1, "nightly.yml", "success", event="schedule", created="2026-11-08T18:00:00+00:00"
        )
        fake = FakeGitHub(FILES, [old])
        reconcile(fake, watches=(watch,))
        self.assertEqual(len(open_issues(fake)), 1)
        fake.runs.append(run(2, 2, "nightly.yml", "success", event="schedule"))
        reconcile(fake, watches=(watch,))
        self.assertEqual(open_issues(fake), [])

    def test_a_missed_trigger_opens_has_not_run_and_the_next_run_closes_it(self) -> None:
        watch = Watch(
            "trends.yml", ("workflow_run",), ("job",), "workflow_run", triggers=("checks.yml",)
        )
        trigger = run(1, 1, "checks.yml", "success", created="2026-11-09T20:00:00+00:00")
        fake = FakeGitHub(FILES, [trigger])
        reconcile(fake, watches=(watch,))
        self.assertEqual(len(open_issues(fake)), 1)
        reconcile(fake, "direct", watches=(watch,))  # never on workflow_run: still open
        self.assertEqual(len(open_issues(fake)), 1)
        follow = run(
            2, 1, "trends.yml", "success", event="workflow_run", created="2026-11-09T20:10:00+00:00"
        )
        fake.runs.append(follow)
        reconcile(fake, watches=(watch,))
        self.assertEqual(open_issues(fake), [])


def generated_runs(seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    runs = []
    for index in range(rng.randint(1, 6)):
        workflow, event, branch = rng.choice(
            [
                ("checks.yml", "push", "main"),
                ("nightly.yml", "schedule", "main"),
                ("trends.yml", "workflow_run", "main"),
                ("release.yml", "push", "v1.0.0"),
                ("checks.yml", "push", "feature"),
            ]
        )
        conclusion = rng.choice(["success", "failure", "cancelled", "skipped", "timed_out"])
        runs.append(run(index + 1, index + 1, workflow, conclusion, event=event, branch=branch))
    return runs


class OrderIndependenceTests(unittest.TestCase):
    def test_every_order_and_duplication_of_events_gives_one_final_state(self) -> None:
        for seed in range(40):
            runs = generated_runs(seed)
            for events in ([], ["schedule"]):
                scopes = ["workflow_run", "workflow_run", *events]
                single = FakeGitHub(FILES, runs)
                reconcile(single, "all" if events else "direct")
                for order in set(itertools.permutations(scopes)):
                    fake = FakeGitHub(FILES, runs)
                    for event in order:
                        reconcile(fake, ci_alerts.SCOPES[event])
                    with self.subTest(seed=seed, order=order):
                        self.assertEqual(fake.snapshot(), single.snapshot())


class ConcurrencyTests(unittest.TestCase):
    def interleave(self, runs: list[dict[str, Any]], prepare: Any = None) -> None:
        point = 1  # the second reconciliation runs before the first's request number `point`
        while True:
            fake = FakeGitHub(FILES, runs)
            if prepare:
                prepare(fake)
            reached = {"at": 0, "done": False}

            def hook(
                fake: FakeGitHub = fake, reached: dict[str, Any] = reached, point: int = point
            ) -> None:
                reached["at"] += 1
                if reached["at"] == point and not reached["done"]:
                    reached["done"] = True
                    fake.hook = None
                    reconcile(fake)  # the second reconciliation, run whole at this point
                    fake.hook = hook

            fake.hook = hook
            reconcile(fake)
            fake.hook = None
            with self.subTest(point=point):
                markers = [i["body"].splitlines()[0] for i in open_issues(fake)]
                self.assertEqual(len(markers), len(set(markers)))
                keys = [(c["issue"], c["body"].splitlines()[0]) for c in fake.comments.values()]
                self.assertEqual(len(keys), len(set(keys)))
            if not reached["done"]:
                break
            point += 1
        self.assertGreater(point, 5)  # every interleaving point was reached

    def test_two_interleaved_reconciliations_open_one_issue(self) -> None:
        self.interleave([run(1, 1, "checks.yml"), run(2, 1, "nightly.yml", event="schedule")])

    def test_two_interleaved_reconciliations_write_one_comment_and_one_close(self) -> None:
        def prepare(fake: FakeGitHub) -> None:
            reconcile(fake)
            fake.runs.append(run(3, 3, "checks.yml"))

        self.interleave([run(1, 1, "checks.yml")], prepare)

        def closing(fake: FakeGitHub) -> None:
            reconcile(fake)
            fake.runs.append(run(3, 3, "checks.yml", "success"))

        self.interleave([run(1, 1, "checks.yml")], closing)


if __name__ == "__main__":
    unittest.main()
