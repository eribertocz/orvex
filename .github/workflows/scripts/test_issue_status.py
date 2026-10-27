import unittest

from issue_status import (
    IN_PROGRESS,
    IN_REVIEW,
    SPEC_READY,
    LabelChange,
    changes_for_event,
    closing_issues,
    issue_from_branch,
)


def no_labels(issue: int) -> list[str]:
    return []


class BranchTests(unittest.TestCase):
    def test_branch_names(self) -> None:
        cases = {
            "ci/3-repository-governance": 3,
            "feat/120-row-policies": 120,
            "main": None,
            "release/v0.1": None,
            "feat/0-x": None,
            "Feat/3-x": None,
            "feat/3-Upper": None,
        }
        for branch, expected in cases.items():
            with self.subTest(branch=branch):
                self.assertEqual(issue_from_branch(branch), expected)

    def test_create_branch_event(self) -> None:
        event = {"ref_type": "branch", "ref": "ci/3-repository-governance"}
        self.assertEqual(
            changes_for_event("create", event, no_labels),
            [LabelChange(3, add=(IN_PROGRESS,), remove=(SPEC_READY,))],
        )

    def test_create_tag_event_is_ignored(self) -> None:
        self.assertEqual(
            changes_for_event("create", {"ref_type": "tag", "ref": "v1"}, no_labels), []
        )


class PullRequestTests(unittest.TestCase):
    def test_closing_references(self) -> None:
        body = "Closes #3\nfixes #12, Resolves #3 and refs #99"
        self.assertEqual(closing_issues(body), [3, 12])
        self.assertEqual(closing_issues(None), [])

    def test_ready_pull_request_marks_issues_in_review(self) -> None:
        event = {"pull_request": {"draft": False, "body": "Closes #3"}}
        self.assertEqual(
            changes_for_event("pull_request", event, no_labels),
            [LabelChange(3, add=(IN_REVIEW,), remove=(IN_PROGRESS,))],
        )

    def test_draft_pull_request_changes_nothing(self) -> None:
        event = {"pull_request": {"draft": True, "body": "Closes #3"}}
        self.assertEqual(changes_for_event("pull_request", event, no_labels), [])


class ClosedIssueTests(unittest.TestCase):
    def test_closed_issue_loses_every_status_label(self) -> None:
        event = {"action": "closed", "issue": {"number": 3}}
        labels = ["type:ci", IN_REVIEW, "status:blocked"]
        self.assertEqual(
            changes_for_event("issues", event, lambda issue: labels),
            [LabelChange(3, add=(), remove=(IN_REVIEW, "status:blocked"))],
        )

    def test_closed_issue_without_status_changes_nothing(self) -> None:
        event = {"action": "closed", "issue": {"number": 3}}
        self.assertEqual(changes_for_event("issues", event, lambda issue: ["type:ci"]), [])


if __name__ == "__main__":
    unittest.main()
