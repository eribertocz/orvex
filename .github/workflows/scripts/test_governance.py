import contextlib
import io
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

from github_api import GitHubApiError
from governance import (
    CRITICAL_HEADING,
    UNCOUNTED_HEADING,
    ChangedFile,
    GovernanceError,
    GovernedPaths,
    body_findings,
    changed_files,
    changed_paths,
    evaluate,
    has_filled_row,
    parse_governed_paths,
    parse_path_block,
    read_rules_document,
    run,
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

    def test_unreadable_document_fails_closed(self) -> None:
        with self.assertRaises(GovernanceError) as context:
            read_rules_document(REPOSITORY_ROOT / "does-not-exist.md")
        self.assertEqual(context.exception.code, "GOVERNED_PATHS_NOT_FOUND")

    def test_undecodable_document_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.md"
            path.write_bytes(b"\xff\xfe\x00invalid")
            with self.assertRaises(GovernanceError) as context:
                read_rules_document(path)
        self.assertEqual(context.exception.code, "GOVERNED_PATHS_NOT_FOUND")

    def test_repository_document_is_parsable(self) -> None:
        document = REPOSITORY_ROOT / "docs" / "process" / "git-workflow.md"
        governed = parse_governed_paths(document.read_text(encoding="utf-8"))
        self.assertIn("docs/", governed.included)
        self.assertIn("docs/user/", governed.excluded)
        self.assertIn(".github/workflows/", governed.included)


class RepositoryBlocksTests(unittest.TestCase):
    document = (REPOSITORY_ROOT / "docs" / "process" / "git-workflow.md").read_text(
        encoding="utf-8"
    )

    def test_every_critical_path_of_the_rules_is_tier_a(self) -> None:
        critical = parse_path_block(self.document, CRITICAL_HEADING)
        cases = {
            "packages/store/src/orvex/store/lake.py": True,
            "packages/store/tests/test_sources.py": True,
            "packages/transform/src/orvex/transform/models.py": True,
            "packages/semantic/src/orvex/semantic/__init__.py": True,
            "packages/access/src/orvex/access/__init__.py": True,
            "packages/server/src/orvex/server/app.py": True,
            "contracts/store/v1/README.md": True,
            "conformance/semantic-corpus/case.yaml": True,
            "docs/adr/0025-data-stays-in-duckdb.md": True,
            "docs/product/laws.md": True,
            ".github/workflows/python.yml": True,
            "packages/server/tests/test_commands.py": False,
            "packages/dashboards/src/orvex/dashboards/__init__.py": False,
            "web/src/routes/+page.svelte": False,
            "tools/checks/boundaries.py": False,
            "docs/process/review.md": False,
            "README.md": False,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(critical.is_governed(path), expected)

    def test_generated_and_lock_files_are_not_counted(self) -> None:
        uncounted = parse_path_block(self.document, UNCOUNTED_HEADING)
        cases = {
            "pylock.toml": True,
            "pylock.py314.toml": True,
            "web/package-lock.json": True,
            "packages/contracts/src/orvex/contracts/store/v1.py": True,
            "web/src/lib/contracts/http-api/v1.ts": True,
            "tests/contracts/generated/sample_v1.py": True,
            "packages/store/src/orvex/store/lake.py": False,
            "contracts/store/v1/schema.json": False,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(uncounted.is_governed(path), expected)

    def test_a_missing_block_fails_closed(self) -> None:
        with self.assertRaises(GovernanceError) as context:
            parse_path_block("# Nothing\n", CRITICAL_HEADING)
        self.assertEqual(context.exception.code, "GOVERNED_PATHS_NOT_FOUND")


MATRIX = """## What changed
Something.

## Case matrix
| Criterion / rejection case | Test | Result |
|---|---|---|
| A decimal arrives exactly | test_csv_decimals | passed |

## Size
Under the guide.
"""
TEMPLATE_MATRIX = """## Case matrix
<!-- Every acceptance criterion. -->
| Criterion / rejection case | Test | Result |
|---|---|---|
| | | |
"""
JUSTIFIED = """## Case matrix
| Case | Test | Result |
|---|---|---|
| x | y | z |

## Size
Mostly generated fixtures for the differential tests; the code change is 120 lines.
"""


class BodyTests(unittest.TestCase):
    critical = GovernedPaths(included=("packages/store/",), excluded=())
    uncounted = GovernedPaths(included=("pylock.toml",), excluded=())

    def findings(self, body: str, files: list[ChangedFile]) -> list[str]:
        return [code for code, _ in body_findings(body, files, self.critical, self.uncounted)]

    def test_a_tier_a_change_needs_a_filled_case_matrix(self) -> None:
        files = [ChangedFile("packages/store/src/x.py", None, 10)]
        self.assertEqual(self.findings("## What changed\nx\n", files), ["CASE_MATRIX_MISSING"])
        self.assertEqual(self.findings(TEMPLATE_MATRIX, files), ["CASE_MATRIX_MISSING"])
        self.assertEqual(self.findings(MATRIX, files), [])

    def test_a_renamed_file_leaving_a_critical_path_is_tier_a(self) -> None:
        files = [ChangedFile("misc/x.py", "packages/store/src/x.py", 2)]
        self.assertEqual(self.findings("", files), ["CASE_MATRIX_MISSING"])

    def test_a_tier_b_change_needs_no_case_matrix(self) -> None:
        self.assertEqual(self.findings("", [ChangedFile("tools/x.py", None, 10)]), [])

    def test_a_change_over_400_lines_needs_a_justified_size(self) -> None:
        big = [ChangedFile("tools/x.py", None, 401)]
        self.assertEqual(self.findings("", big), ["SIZE_NOT_JUSTIFIED"])
        self.assertEqual(self.findings(MATRIX, big), ["SIZE_NOT_JUSTIFIED"])  # "under the guide"
        self.assertEqual(self.findings(JUSTIFIED, big), [])
        self.assertEqual(self.findings("", [ChangedFile("tools/x.py", None, 400)]), [])

    def test_lock_and_generated_lines_are_not_counted(self) -> None:
        files = [ChangedFile("pylock.toml", None, 5000), ChangedFile("tools/x.py", None, 30)]
        self.assertEqual(self.findings("", files), [])

    def test_a_missing_body_fails_like_an_empty_one(self) -> None:
        files = [ChangedFile("packages/store/src/x.py", None, 500)]
        self.assertEqual(self.findings("", files), ["CASE_MATRIX_MISSING", "SIZE_NOT_JUSTIFIED"])


class FilledRowTests(unittest.TestCase):
    def test_only_a_data_row_with_content_counts(self) -> None:
        self.assertTrue(has_filled_row("| a | b |\n|---|---|\n| x | |\n"))
        self.assertFalse(has_filled_row("| a | b |\n|---|---|\n| | |\n"))
        self.assertFalse(has_filled_row("| a | b |\n|---|---|\n|---|---|\n|:-:|---|\n"))
        self.assertFalse(has_filled_row("| a |\n|---|\n| \\| |\n"))

    def test_a_table_inside_a_code_block_does_not_count(self) -> None:
        for fence in ("```", "~~~", "````"):
            with self.subTest(fence=fence):
                text = f"{fence}\n| a |\n|---|\n| x |\n{fence}\n"
                self.assertFalse(has_filled_row(text))

    def test_a_second_table_does_not_fill_an_empty_first_one(self) -> None:
        text = "| a |\n|---|\n| |\n\n| b |\n|---|\n"
        self.assertFalse(has_filled_row(text))


class DependabotTests(unittest.TestCase):
    critical = GovernedPaths(included=(".github/workflows/",), excluded=())
    uncounted = GovernedPaths(included=("pylock.toml",), excluded=())

    def test_a_dependabot_update_needs_no_case_matrix_nor_size(self) -> None:
        files = [ChangedFile(".github/workflows/python.yml", None, 900)]
        found = body_findings("", files, self.critical, self.uncounted, "dependabot[bot]")
        self.assertEqual(found, [])

    def test_anyone_else_still_needs_them(self) -> None:
        files = [ChangedFile(".github/workflows/python.yml", None, 900)]
        found = body_findings("", files, self.critical, self.uncounted, "eribertocz")
        self.assertEqual([code for code, _ in found], ["CASE_MATRIX_MISSING", "SIZE_NOT_JUSTIFIED"])


RULES = (
    DOCUMENT
    + """
### Critical paths (tier A)

```
packages/store/
```

### Not counted in a pull request's size

```
pylock.toml
```
"""
)


class PullClient:
    """A fake GitHub API for one pull request."""

    def __init__(self, body: Any, files: list[dict[str, Any]], labels: list[str]) -> None:
        self.body = body
        self.files = files
        self.labels = labels
        self.requests = 0

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        # The first request lists the files; with `GitHubApiError` as the body, the next one,
        # which reads the description, fails.
        self.requests += 1
        if self.body is GitHubApiError and self.requests > 1:
            raise GitHubApiError("GET failed with HTTP 502")
        return {"changed_files": len(self.files), "body": self.body, "user": {"login": "a"}}

    def paginate(self, path: str) -> Iterator[Any]:
        if path.endswith("/labels"):
            yield from ({"name": name} for name in self.labels)
        else:
            yield from self.files


class RunTests(unittest.TestCase):
    store: ClassVar[list[dict[str, Any]]] = [
        {"filename": "packages/store/src/x.py", "additions": 5, "deletions": 0}
    ]

    def run_with(self, client: PullClient) -> int:
        return run(client, 7, RULES)

    def test_the_check_fails_on_a_broken_description_rule(self) -> None:
        self.assertEqual(self.run_with(PullClient("## What\nx\n", self.store, [])), 1)

    def test_the_check_passes_a_compliant_pull_request(self) -> None:
        self.assertEqual(self.run_with(PullClient(MATRIX, self.store, [])), 0)

    def test_an_unreadable_description_fails_closed(self) -> None:
        # A small tier B change, which an empty description would let through: only the
        # failure to read it can block it.
        small = [{"filename": "tools/x.py", "additions": 5, "deletions": 0}]
        self.assertEqual(self.run_with(PullClient("", small, [])), 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.run_with(PullClient(GitHubApiError, small, [])), 1)
        self.assertIn("DESCRIPTION_UNAVAILABLE", output.getvalue())

    def test_a_null_description_is_an_empty_one(self) -> None:
        self.assertEqual(self.run_with(PullClient(None, self.store, [])), 1)

    def test_governed_paths_still_need_the_label(self) -> None:
        governed = [{"filename": "docs/x.md", "additions": 1, "deletions": 0}]
        self.assertEqual(self.run_with(PullClient("", governed, [])), 1)
        self.assertEqual(self.run_with(PullClient("", governed, ["owner-merge"])), 0)


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


class ChangedFilesTests(unittest.TestCase):
    def test_counts_added_and_removed_lines(self) -> None:
        files = [{"filename": "a.py", "additions": 3, "deletions": 2}]
        self.assertEqual(changed_files(FakeClient(1, files), 7), [ChangedFile("a.py", None, 5)])

    def test_a_missing_line_count_fails_closed(self) -> None:
        with self.assertRaises(GovernanceError) as context:
            changed_files(FakeClient(1, [{"filename": "a.py"}]), 7)
        self.assertEqual(context.exception.code, "CHANGED_FILES_UNAVAILABLE")


class ChangedPathsTests(unittest.TestCase):
    def test_includes_previous_filename_of_renames(self) -> None:
        files = [
            {
                "filename": "misc/0001.md",
                "previous_filename": "docs/adr/0001.md",
                "additions": 0,
                "deletions": 0,
            }
        ]
        self.assertEqual(
            changed_paths(FakeClient(1, files), 7),
            ["misc/0001.md", "docs/adr/0001.md"],
        )

    def test_api_failure_fails_closed(self) -> None:
        with self.assertRaises(GovernanceError) as context:
            changed_paths(FakeClient(1, [], fail=True), 7)
        self.assertEqual(context.exception.code, "CHANGED_FILES_UNAVAILABLE")

    def test_truncated_listing_fails_closed(self) -> None:
        files = [{"filename": f"f{index}", "additions": 1, "deletions": 0} for index in range(3000)]
        with self.assertRaises(GovernanceError) as context:
            changed_paths(FakeClient(3001, files), 7)
        self.assertEqual(context.exception.code, "CHANGED_FILES_UNAVAILABLE")

    def test_malformed_entry_fails_closed(self) -> None:
        with self.assertRaises(GovernanceError) as context:
            changed_paths(FakeClient(1, [{"name": "a"}]), 7)
        self.assertEqual(context.exception.code, "CHANGED_FILES_UNAVAILABLE")

    def test_count_mismatch_fails_closed(self) -> None:
        with self.assertRaises(GovernanceError) as context:
            changed_paths(FakeClient(2, [{"filename": "a", "additions": 1, "deletions": 0}]), 7)
        self.assertEqual(context.exception.code, "CHANGED_FILES_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
