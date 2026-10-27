import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from github_api import GitHubApiError
from governance import (
    GovernanceError,
    GovernedPaths,
    changed_paths,
    evaluate,
    parse_governed_paths,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]

DOCUMENT = """# Git workflow

### Governed paths

Text before the block.

```
docs/              everything under docs/
!docs/user/        end-user documentation is not governed
AGENTS.md
.github/workflows/
```

## Next section
"""


class ParseTests(unittest.TestCase):
    def test_reads_inclusions_and_exclusions(self) -> None:
        governed = parse_governed_paths(DOCUMENT)
        self.assertEqual(governed.included, ("docs/", "AGENTS.md", ".github/workflows/"))
        self.assertEqual(governed.excluded, ("docs/user/",))

    def test_missing_section_fails_closed(self) -> None:
        with self.assertRaises(GovernanceError) as context:
            parse_governed_paths("# Nothing here\n")
        self.assertEqual(context.exception.code, "GOVERNED_PATHS_NOT_FOUND")

    def test_unclosed_block_fails_closed(self) -> None:
        with self.assertRaises(GovernanceError) as context:
            parse_governed_paths("### Governed paths\n```\ndocs/\n")
        self.assertEqual(context.exception.code, "GOVERNED_PATHS_NOT_FOUND")

    def test_empty_block_fails_closed(self) -> None:
        with self.assertRaises(GovernanceError) as context:
            parse_governed_paths("### Governed paths\n```\n\n!docs/user/\n```\n")
        self.assertEqual(context.exception.code, "GOVERNED_PATHS_NOT_FOUND")

    def test_repository_document_is_parsable(self) -> None:
        document = REPOSITORY_ROOT / "docs" / "process" / "git-workflow.md"
        governed = parse_governed_paths(document.read_text(encoding="utf-8"))
        self.assertIn("docs/", governed.included)
        self.assertIn("docs/user/", governed.excluded)
        self.assertIn(".github/workflows/", governed.included)


class MatchingTests(unittest.TestCase):
    governed = parse_governed_paths(DOCUMENT)

    def test_matching_rules(self) -> None:
        cases = {
            "docs/README.md": True,
            "docs/userx.md": True,
            "docs/user/guide.md": False,
            "AGENTS.md": True,
            "AGENTS.md.bak": False,
            ".github/workflows/x.yml": True,
            ".github/labels.yml": False,
            "tools/repo/main-ruleset.json": False,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(self.governed.is_governed(path), expected)


class EvaluateTests(unittest.TestCase):
    governed = GovernedPaths(included=("docs/", ".github/workflows/"), excluded=("docs/user/",))

    def test_governed_change_without_label_is_blocked(self) -> None:
        self.assertEqual(
            evaluate(["docs/README.md", "tools/x.py"], [], self.governed), ["docs/README.md"]
        )

    def test_governed_change_with_label_passes(self) -> None:
        self.assertEqual(evaluate(["docs/README.md"], ["owner-merge"], self.governed), [])

    def test_non_governed_change_passes_without_label(self) -> None:
        self.assertEqual(evaluate(["tools/x.py", ".github/labels.yml"], [], self.governed), [])

    def test_rename_out_of_governed_path_counts(self) -> None:
        # changed_paths() reports both the new and the previous filename of a rename.
        changed = ["misc/0001.md", "docs/adr/0001-record-architecture-decisions.md"]
        self.assertEqual(
            evaluate(changed, [], self.governed),
            ["docs/adr/0001-record-architecture-decisions.md"],
        )


class FakeClient:
    def __init__(self, changed_files: int, files: list[dict[str, Any]], fail: bool = False) -> None:
        self._changed_files = changed_files
        self._files = files
        self._fail = fail

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if self._fail:
            raise GitHubApiError("GET failed with HTTP 502")
        return {"changed_files": self._changed_files}

    def paginate(self, path: str) -> Iterator[Any]:
        yield from self._files


class ChangedPathsTests(unittest.TestCase):
    def test_includes_previous_filename_of_renames(self) -> None:
        files = [{"filename": "misc/0001.md", "previous_filename": "docs/adr/0001.md"}]
        self.assertEqual(
            changed_paths(FakeClient(1, files), 7),
            ["misc/0001.md", "docs/adr/0001.md"],
        )

    def test_api_failure_fails_closed(self) -> None:
        with self.assertRaises(GovernanceError) as context:
            changed_paths(FakeClient(1, [], fail=True), 7)
        self.assertEqual(context.exception.code, "CHANGED_FILES_UNAVAILABLE")

    def test_truncated_listing_fails_closed(self) -> None:
        files = [{"filename": f"f{index}"} for index in range(3000)]
        with self.assertRaises(GovernanceError) as context:
            changed_paths(FakeClient(3001, files), 7)
        self.assertEqual(context.exception.code, "CHANGED_FILES_UNAVAILABLE")

    def test_count_mismatch_fails_closed(self) -> None:
        with self.assertRaises(GovernanceError) as context:
            changed_paths(FakeClient(2, [{"filename": "a"}]), 7)
        self.assertEqual(context.exception.code, "CHANGED_FILES_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
