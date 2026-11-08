import unittest
from collections.abc import Iterator
from typing import Any

from github_api import GitHubApiError
from nightly_alert import LABELS, TITLE, alert, main

RUN = "https://github.com/o/r/actions/runs/42"


class FakeIssues:
    def __init__(self, issues: list[dict[str, Any]], fail: bool = False) -> None:
        self.issues = issues
        self.fail = fail
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.listed: list[str] = []

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if self.fail:
            raise GitHubApiError(f"{method} {path} failed with HTTP 502")
        self.calls.append((method, path, body))
        return {"number": 99}

    def paginate(self, path: str) -> Iterator[Any]:
        if self.fail:
            raise GitHubApiError(f"GET {path} failed with HTTP 502")
        self.listed.append(path)
        yield from self.issues


def tracking(number: int, title: str = TITLE, **extra: Any) -> dict[str, Any]:
    return {"number": number, "title": title, **extra}


class AlertTests(unittest.TestCase):
    def test_a_first_failure_opens_one_labeled_issue_assigned_to_the_agent(self) -> None:
        client = FakeIssues([])
        self.assertEqual(alert(client, RUN, "owner"), "opened #99")
        [(method, path, body)] = client.calls
        self.assertEqual((method, path), ("POST", "/issues"))
        assert body is not None
        self.assertEqual(body["title"], TITLE)
        self.assertEqual(body["labels"], list(LABELS))
        self.assertEqual(body["assignees"], ["owner"])
        self.assertIn(RUN, body["body"])
        self.assertIn("regression test", body["body"])
        # The open issues are searched by the tracking label, URL-encoded.
        self.assertTrue(all("state=open" in p and "type%3Atest" in p for p in client.listed))

    def test_a_later_failure_comments_on_the_open_issue_and_opens_none(self) -> None:
        client = FakeIssues([tracking(7, "Something else"), tracking(12)])
        self.assertEqual(alert(client, RUN, "owner"), "updated #12")
        methods = [(method, path) for method, path, _ in client.calls]
        self.assertEqual(
            methods, [("POST", "/issues/12/comments"), ("POST", "/issues/12/assignees")]
        )
        self.assertIn(RUN, str(client.calls[0][2]))
        self.assertEqual(client.calls[1][2], {"assignees": ["owner"]})

    def test_a_pull_request_with_the_title_is_not_the_tracking_issue(self) -> None:
        client = FakeIssues([tracking(5, pull_request={"url": "x"})])
        self.assertEqual(alert(client, RUN, "owner"), "opened #99")

    def test_an_api_failure_fails_the_job(self) -> None:
        with self.assertRaises(GitHubApiError):
            alert(FakeIssues([], fail=True), RUN, "owner")

    def test_missing_settings_fail_the_job(self) -> None:
        self.assertEqual(main({}), 1)
        self.assertEqual(main({"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "o/r"}), 1)


if __name__ == "__main__":
    unittest.main()
