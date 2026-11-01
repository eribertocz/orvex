"""The canonical list of mandatory checks (tools/ci/checks.toml, #240) equals what CI runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.ci import checks
from tools.repository import REPOSITORY_ROOT


def test_the_list_equals_the_workflows_of_the_repository() -> None:
    assert checks.drift(REPOSITORY_ROOT) == []


def test_every_check_runs_locally_or_says_why_not() -> None:
    for check in checks.load(REPOSITORY_ROOT):
        assert check.local or check.reason, check.name


def write_repository(root: Path, workflow: str, listed: str) -> Path:
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text(workflow)
    (root / "tools" / "ci").mkdir(parents=True)
    (root / "tools" / "ci" / "checks.toml").write_text(listed)
    return root


WORKFLOW = """name: ci
on: [pull_request]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: pip install x
      - run: ruff check .
"""
LISTED = """[[job]]
workflow = "ci.yml"
job = "lint"
kind = "check"
setup = ["pip install x"]
[[job.check]]
name = "ruff"
run = "ruff check ."
local = true
"""


def test_a_matching_sample_has_no_drift(tmp_path: Path) -> None:
    assert checks.drift(write_repository(tmp_path, WORKFLOW, LISTED)) == []


@pytest.mark.parametrize(
    ("workflow", "listed", "expected"),
    [
        (WORKFLOW + "      - run: mypy .\n", LISTED, "not listed"),
        (WORKFLOW.replace("      - run: ruff check .\n", ""), LISTED, "not in the workflow"),
        (
            WORKFLOW + "  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n",
            LISTED,
            "job ci.yml:test is not listed",
        ),
        (WORKFLOW, LISTED.replace("local = true", "local = false"), "needs a reason"),
        (
            WORKFLOW.replace(
                "      - run: ruff check .",
                "      - run: ruff check .\n        working-directory: web",
            ),
            LISTED,
            "directory",
        ),
        (
            WORKFLOW.replace("on: [pull_request]", "on:\n  pull_request:\n    paths: [x/**]"),
            LISTED,
            "scoped",
        ),
        (WORKFLOW, LISTED.replace('kind = "check"', 'kind = "scoped"'), "every pull request"),
        (
            WORKFLOW.replace(
                "    steps:", "    strategy:\n      matrix:\n        python: ['3.15']\n    steps:"
            ),
            LISTED,
            "pythons",
        ),
        (
            WORKFLOW,
            LISTED + '[[job]]\nworkflow = "ci.yml"\njob = "gone"\nkind = "automation"\n',
            "listed but not",
        ),
        (
            WORKFLOW,
            LISTED.replace('setup = ["pip install x"]', 'setup = ["pip install x", "make"]'),
            "setup step",
        ),
    ],
)
def test_any_difference_is_reported(
    tmp_path: Path, workflow: str, listed: str, expected: str
) -> None:
    found = checks.drift(write_repository(tmp_path, workflow, listed))
    assert any(expected in line for line in found), found
