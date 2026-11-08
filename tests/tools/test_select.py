"""#391: what one part of the `python` workflow runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.ci import select, verify

DOCS = ["docs/process/engineering.md"]
STORE = ["packages/store/src/orvex/store/lake.py"]
GLOBAL = ["pyproject.toml"]


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
@pytest.mark.parametrize("python", ["3.15", "3.14"])
def test_main_and_schedules_run_the_whole_suite_on_every_python(event: str, python: str) -> None:
    for part in range(select.PARTS):
        choice = select.choose(event, python, part, STORE)
        assert (choice.shard, choice.paths, choice.skip) == (f"{part}/10", (), False)


def test_a_pull_request_runs_the_whole_suite_on_the_newest_python() -> None:
    for part in range(select.PARTS):
        assert select.choose("pull_request", "3.15", part, DOCS).paths == ()
        assert select.choose("pull_request", "3.15", part, DOCS).shard == f"{part}/10"


def test_a_pull_request_runs_the_critical_and_affected_tests_on_the_other_python() -> None:
    chosen = [select.choose("pull_request", "3.14", part, STORE) for part in range(select.PARTS)]
    running = [c for c in chosen if not c.skip]
    assert [c.shard for c in running] == ["0/2", "1/2"]
    assert all(c.skip for c in chosen[select.FEW :])
    paths = set(running[0].paths)
    assert "packages/store/tests" in paths  # the change
    assert "packages/server/tests" in paths  # what depends on it
    assert "tests/contracts" in paths  # a critical set, whatever changes


def test_an_uncertain_impact_runs_the_whole_suite_on_every_python() -> None:
    for part in range(select.PARTS):
        choice = select.choose("pull_request", "3.14", part, GLOBAL)
        assert (choice.shard, choice.paths, choice.skip) == (f"{part}/10", (), False)


def test_a_path_that_is_not_plain_selects_the_whole_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify, "pr_tests", lambda *_: (["packages/x;rm -rf ~/tests"], ["forged"]))
    choice = select.choose("pull_request", "3.14", 0, STORE)
    assert (choice.paths, choice.skip, choice.shard) == ((), False, "0/10")


def test_the_choice_is_written_for_the_next_steps(tmp_path: Path) -> None:
    output = tmp_path / "env"
    environment = {"GITHUB_EVENT_NAME": "push", "PYTHON": "3.14", "PART": "3"}
    assert select.main({**environment, "GITHUB_ENV": str(output)}) == 0
    assert output.read_text("utf-8").splitlines() == [
        "PYTEST_SHARD=3/10",
        "PYTEST_PATHS=",
        "PYTEST_SKIP=false",
    ]
