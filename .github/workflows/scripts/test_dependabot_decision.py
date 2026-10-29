import re
import unittest
from pathlib import Path

from dependabot_decision import decide

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "dependabot-auto-merge.yml"
LOCKS = ["pyproject.toml", "pylock.toml", "pylock.py314.toml"]
NPM = ["web/package.json", "web/package-lock.json"]


class DecisionTableTest(unittest.TestCase):
    """The decision table of #44: auto-merge only tier C updates (git-workflow.md §3.5)."""

    def test_the_table(self) -> None:
        bot = "dependabot[bot]"
        cases = [
            (bot, "pip", "version-update:semver-patch", LOCKS, True, "pip patch"),
            (bot, "npm_and_yarn", "version-update:semver-minor", NPM, True, "npm_and_yarn minor"),
            (bot, "npm_and_yarn", "version-update:semver-major", NPM[:1], False, "major"),
            (
                bot,
                "github_actions",
                "version-update:semver-patch",
                [".github/workflows/python.yml"],
                False,
                "not pip or npm (github_actions)",
            ),
            (bot, "docker", "version-update:semver-minor", ["Dockerfile"], False, "not pip or npm"),
            (
                bot,
                "pip",
                "version-update:semver-minor",
                ["pyproject.toml", ".github/x.yml"],
                False,
                "governed path (.github/x.yml)",
            ),
            (
                "someone-else",
                "pip",
                "version-update:semver-patch",
                LOCKS[:1],
                False,
                "not Dependabot",
            ),
            (bot, "npm_and_yarn", "", NPM[:1], False, "unknown update type"),
        ]
        for author, ecosystem, update_type, files, merge, reason in cases:
            with self.subTest(author=author, ecosystem=ecosystem, update_type=update_type):
                decision = decide(author, ecosystem, update_type, files)
                self.assertEqual(decision.merge, merge)
                self.assertTrue(decision.reason.startswith(reason), decision.reason)

    def test_an_unknown_kind_or_no_files_is_left(self) -> None:
        bot = "dependabot[bot]"
        self.assertFalse(decide(bot, "pip", "version-update:semver-prerelease", LOCKS).merge)
        self.assertFalse(decide(bot, "pip", "version-update:semver-patch", []).merge)
        self.assertFalse(
            decide("Dependabot[bot]", "pip", "version-update:semver-patch", LOCKS).merge
        )


class WorkflowTest(unittest.TestCase):
    """The workflow itself: pinned actions, least privilege, the decision before the merge."""

    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_every_action_is_pinned_to_a_full_sha(self) -> None:
        uses = re.findall(r"uses:\s*(\S+)", self.text)
        self.assertTrue(uses)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")

    def test_only_the_job_may_write_and_only_for_dependabot(self) -> None:
        top = self.text.split("\njobs:", 1)[0]
        self.assertIn("permissions:\n  contents: read", top)
        self.assertNotIn("write", top)
        self.assertIn("github.event.pull_request.user.login == 'dependabot[bot]'", self.text)
        self.assertIn("github.repository == 'eribertocz/orvex'", self.text)

    def test_the_script_runs_from_the_base_commit(self) -> None:
        # The job holds a write token: it must not run code from the pull request's branch.
        self.assertIn("ref: ${{ github.event.pull_request.base.sha }}", self.text)
        self.assertIn("persist-credentials: false", self.text)

    def test_the_merge_waits_for_the_decision_and_squashes(self) -> None:
        self.assertIn("if: steps.decision.outputs.merge == 'true'", self.text)
        self.assertIn('gh pr merge --auto --squash "$PR_URL"', self.text)


if __name__ == "__main__":
    unittest.main()
