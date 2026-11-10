"""#391: what one part of the `python` workflow runs."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.ci import select, verify
from tools.repository import REPOSITORY_ROOT

DOCS = ["docs/process/engineering.md"]
STORE = ["packages/store/src/orvex/store/lake.py"]
GLOBAL = ["pyproject.toml"]


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch"])
@pytest.mark.parametrize("python", ["3.15", "3.14"])
def test_main_and_schedules_run_the_whole_suite_on_every_python(event: str, python: str) -> None:
    for part in range(select.PARTS):
        choice = select.choose(event, part, STORE)
        assert (choice.shard, choice.paths) == (f"{part}/10", ())


@pytest.mark.parametrize("python", ["3.15", "3.14"])
def test_a_pull_request_runs_the_selection_on_every_python(python: str) -> None:
    chosen = [select.choose("pull_request", part, STORE) for part in range(select.PARTS)]
    assert [c.shard for c in chosen] == [f"{part}/10" for part in range(select.PARTS)]
    assert all(c.paths for c in chosen)


def test_a_pull_request_runs_the_critical_and_affected_tests_on_the_other_python() -> None:
    chosen = [select.choose("pull_request", part, STORE) for part in range(select.PARTS)]
    # The selection is spread over every part, as the whole suite is: never a slower job.
    assert [c.shard for c in chosen] == [f"{part}/10" for part in range(select.PARTS)]
    assert len({c.paths for c in chosen}) == 1
    paths = set(chosen[0].paths)
    assert "packages/store/tests" in paths  # the change
    assert "packages/server/tests" in paths  # what depends on it
    assert "tests/contracts" in paths  # a critical set, whatever changes


def test_an_uncertain_impact_runs_the_whole_suite_on_every_python() -> None:
    for part in range(select.PARTS):
        choice = select.choose("pull_request", part, GLOBAL)
        assert (choice.shard, choice.paths) == (f"{part}/10", ())


def test_a_path_that_is_not_plain_selects_the_whole_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify, "pr_tests", lambda *_: (["packages/x;rm -rf ~/tests"], ["forged"]))
    choice = select.choose("pull_request", 0, STORE)
    assert (choice.paths, choice.shard) == ((), "0/10")


def test_the_choice_is_written_for_the_next_steps(tmp_path: Path) -> None:
    output = tmp_path / "env"
    environment = {"GITHUB_EVENT_NAME": "push", "PYTHON": "3.14", "PART": "3"}
    assert select.main({**environment, "GITHUB_ENV": str(output)}) == 0
    assert output.read_text("utf-8").splitlines() == [
        "PYTEST_SHARD=3/10",
        "PYTEST_PATHS=",
        "PYTEST_COVERAGE=1",
    ]


def test_coverage_is_measured_only_on_the_whole_suite(monkeypatch: pytest.MonkeyPatch) -> None:
    # #429: a selection measures less, so it would fail the floors; it is never measured.
    monkeypatch.setattr(verify, "pr_tests", lambda *_: (["packages/store/tests"], ["store"]))
    selected = select.lines(select.choose("pull_request", 0, STORE))
    assert "PYTEST_COVERAGE=0" in selected
    assert "PYTEST_COVERAGE=1" in select.lines(select.choose("push", 0, None))


def test_every_python_of_a_pull_request_fetches_the_history_it_compares() -> None:
    from tools.repository import REPOSITORY_ROOT

    workflow = (REPOSITORY_ROOT / ".github/workflows/python.yml").read_text("utf-8")
    assert "fetch-depth: ${{ github.event_name == 'pull_request' && '0' || '1' }}" in workflow


def test_a_part_without_history_runs_its_share_of_the_whole_suite(tmp_path: Path) -> None:
    output = tmp_path / "env"
    environment = {"PYTHON": "3.14", "PART": "4", "GITHUB_ENV": str(output)}
    assert select._whole(environment) == 0
    assert output.read_text("utf-8").splitlines()[0] == "PYTEST_SHARD=4/10"


def test_the_python_workflow_runs_the_whole_suite_on_every_push_to_main() -> None:
    # #427: the periodic full run that catches anything a selection misses cannot be removed or
    # narrowed without this failing: a push to `main` triggers it on both Pythons, every part.
    workflow = yaml.safe_load((REPOSITORY_ROOT / ".github/workflows/python.yml").read_text("utf-8"))
    triggers = workflow.get(True, workflow.get("on"))
    assert "main" in triggers["push"]["branches"]
    assert "paths" not in triggers["push"]
    assert "paths-ignore" not in triggers["push"]
    job = workflow["jobs"]["python-tests-part"]
    assert {str(p) for p in job["strategy"]["matrix"]["python"]} >= {"3.14", "3.15"}
    assert len(job["strategy"]["matrix"]["part"]) == select.PARTS
    steps = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "python -m tools.ci.select" in steps
