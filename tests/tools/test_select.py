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
        assert (choice.shard, choice.paths) == (f"{part}/10", ())


def test_a_pull_request_runs_the_whole_suite_on_the_newest_python() -> None:
    for part in range(select.PARTS):
        assert select.choose("pull_request", "3.15", part, DOCS).paths == ()
        assert select.choose("pull_request", "3.15", part, DOCS).shard == f"{part}/10"


def test_a_pull_request_runs_the_critical_and_affected_tests_on_the_other_python() -> None:
    chosen = [select.choose("pull_request", "3.14", part, STORE) for part in range(select.PARTS)]
    # The selection is spread over every part, as the whole suite is: never a slower job.
    assert [c.shard for c in chosen] == [f"{part}/10" for part in range(select.PARTS)]
    assert len({c.paths for c in chosen}) == 1
    paths = set(chosen[0].paths)
    assert "packages/store/tests" in paths  # the change
    assert "packages/server/tests" in paths  # what depends on it
    assert "tests/contracts" in paths  # a critical set, whatever changes


def test_an_uncertain_impact_runs_the_whole_suite_on_every_python() -> None:
    for part in range(select.PARTS):
        choice = select.choose("pull_request", "3.14", part, GLOBAL)
        assert (choice.shard, choice.paths) == (f"{part}/10", ())


def test_a_path_that_is_not_plain_selects_the_whole_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify, "pr_tests", lambda *_: (["packages/x;rm -rf ~/tests"], ["forged"]))
    choice = select.choose("pull_request", "3.14", 0, STORE)
    assert (choice.paths, choice.shard) == ((), "0/10")


def test_the_choice_is_written_for_the_next_steps(tmp_path: Path) -> None:
    output = tmp_path / "env"
    environment = {"GITHUB_EVENT_NAME": "push", "PYTHON": "3.14", "PART": "3"}
    assert select.main({**environment, "GITHUB_ENV": str(output)}) == 0
    assert output.read_text("utf-8").splitlines() == [
        "PYTEST_SHARD=3/10",
        "PYTEST_PATHS=",
    ]


def test_the_newest_python_is_the_matrix_s_first_and_the_one_fetched_shallow() -> None:
    # A new Python in the matrix must move WHOLE too, or no Python would run the whole suite.
    import yaml

    from tools.repository import REPOSITORY_ROOT

    workflow = (REPOSITORY_ROOT / ".github/workflows/python.yml").read_text("utf-8")
    job = yaml.safe_load(workflow)["jobs"]["python-tests-part"]
    assert job["strategy"]["matrix"]["python"][0] == select.WHOLE
    assert f"matrix.python != '{select.WHOLE}'" in workflow


def test_a_part_without_history_runs_its_share_of_the_whole_suite(tmp_path: Path) -> None:
    output = tmp_path / "env"
    environment = {"PYTHON": "3.14", "PART": "4", "GITHUB_ENV": str(output)}
    assert select._whole(environment) == 0
    assert output.read_text("utf-8").splitlines()[0] == "PYTEST_SHARD=4/10"
