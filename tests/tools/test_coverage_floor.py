"""#429: the critical sources' branch coverage holds its floors, and bad data fails closed."""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

import coverage
import pytest

from tools.ci import coverage_floor
from tools.ci.coverage_floor import CoverageError
from tools.ci.parts import PARTS
from tools.repository import REPOSITORY_ROOT

SOURCE = "pkg/branchy.py"
OTHER = "pkg/never.py"
CODE = "def pick(x):\n    if x:\n        return 1\n    return 0\n\npick(True)\n"
CONFIG = (
    '[tool.coverage.run]\nbranch = true\nparallel = true\nrelative_files = true\nsource = ["pkg"]\n'
)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / SOURCE).write_text(CODE, "utf-8")
    (tmp_path / OTHER).write_text("VALUE = 1\n", "utf-8")
    (tmp_path / "pyproject.toml").write_text(CONFIG, "utf-8")
    monkeypatch.chdir(tmp_path)  # coverage keeps paths relative to where it runs, as in CI
    return tmp_path


def part(root: Path, number: int, measured: bool = True, python: str = "3.15") -> Path:
    folder = root / "data" / f"coverage-{python}-{number}"
    folder.mkdir(parents=True)
    if not measured:
        (folder / coverage_floor.NOT_MEASURED).touch()
        return folder
    work = coverage.Coverage(
        data_file=str(folder / ".coverage"), config_file=str(root / "pyproject.toml")
    )
    work.start()
    runpy.run_path(str(root / SOURCE))
    work.stop()
    work.save()
    return folder


def test_combined_parts_give_each_imported_file_its_branch_coverage(root: Path) -> None:
    for number in range(2):
        part(root, number)
    files = coverage_floor.part_files(root / "data", "3.15", parts=2)
    assert files is not None
    measured = coverage_floor.measure(files, root)
    assert set(measured) == {SOURCE}  # never imported: absent, not 0%
    assert 50 < measured[SOURCE] < 100  # the `return 0` branch never ran


def test_a_part_without_data_or_without_its_folder_fails(root: Path) -> None:
    part(root, 0)
    with pytest.raises(CoverageError, match=r"part 1 .* left no coverage folder"):
        coverage_floor.part_files(root / "data", "3.15", parts=2)
    (root / "data" / "coverage-3.15-1").mkdir()
    with pytest.raises(CoverageError, match=r"part 1 .* left no coverage data"):
        coverage_floor.part_files(root / "data", "3.15", parts=2)


def test_measured_and_unmeasured_parts_never_mix(root: Path) -> None:
    part(root, 0)
    part(root, 1, measured=False)
    with pytest.raises(CoverageError, match="1 of 2 parts"):
        coverage_floor.part_files(root / "data", "3.15", parts=2)


def test_no_part_measured_is_a_selection_and_passes(root: Path) -> None:
    for number in range(2):
        part(root, number, measured=False)
    assert coverage_floor.part_files(root / "data", "3.15", parts=2) is None


def test_unreadable_or_empty_data_fails(root: Path) -> None:
    folder = root / "data" / "coverage-3.15-0"
    folder.mkdir(parents=True)
    (folder / ".coverage.broken").write_text("not a database", "utf-8")
    files = coverage_floor.part_files(root / "data", "3.15", parts=1)
    assert files is not None
    with pytest.raises(CoverageError, match="unreadable"):
        coverage_floor.measure(files, root)


def test_data_that_measured_nothing_is_empty_and_fails(root: Path) -> None:
    folder = root / "data" / "coverage-3.15-0"
    folder.mkdir(parents=True)
    work = coverage.Coverage(
        data_file=str(folder / ".coverage"), config_file=str(root / "pyproject.toml")
    )
    work.start()
    work.stop()
    with pytest.warns(coverage.exceptions.CoverageWarning, match="No data was collected"):
        work.save()
    files = coverage_floor.part_files(root / "data", "3.15", parts=1)
    assert files is not None
    with pytest.raises(CoverageError, match="empty"):
        coverage_floor.measure(files, root)


def test_each_floor_case_fails_and_a_rise_is_only_reported() -> None:
    sources = ["a.py", "b.py", "c.py", "d.py"]
    measured = {"a.py": 79.9, "b.py": 90.0, "d.py": 95.5}
    floors = {"a.py": 80, "b.py": 85, "c.py": 10, "e.py": 50}
    failures, candidates = coverage_floor.judge(measured, sources, floors)
    assert sorted(f.split(":")[0] for f in failures) == [
        "COVERAGE_BELOW_FLOOR a.py",
        "COVERAGE_FLOOR_MISSING d.py",
        "COVERAGE_FLOOR_UNKNOWN e.py",
        "COVERAGE_NOT_INSTRUMENTED c.py",
    ]
    assert candidates == ["b.py: 90.00% (floor 85%)"]
    held, raised = coverage_floor.judge({"a.py": 80.0}, ["a.py"], {"a.py": 80})
    assert (held, raised) == ([], [])


def test_the_measured_floors_round_down() -> None:
    text = coverage_floor.floors_text({"a.py": 79.99, "b.py": 100.0}, ["a.py", "b.py", "c.py"])
    assert text == '[floors]\n"a.py" = 79\n"b.py" = 100\n'


def test_the_repository_floors_name_exactly_the_critical_sources() -> None:
    sources = coverage_floor.critical_sources()
    assert sources
    assert sorted(coverage_floor.floors()) == sources


def test_the_command_fails_closed_and_passes_an_unmeasured_selection(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coverage_floor, "PARTS", 1)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    assert coverage_floor.main([str(root / "data"), "--python", "3.15"]) == 1
    assert "COVERAGE_DATA_INVALID" in capsys.readouterr().out
    part(root, 0, measured=False)
    assert coverage_floor.main([str(root / "data"), "--python", "3.15"]) == 0
    assert "coverage not measured" in capsys.readouterr().out


def test_the_workflow_measures_the_whole_suite_and_its_result_gates_the_tests() -> None:
    import yaml

    workflow = yaml.safe_load((REPOSITORY_ROOT / ".github/workflows/python.yml").read_text("utf-8"))
    jobs = workflow["jobs"]
    run = next(
        s["run"] for s in jobs["python-tests-part"]["steps"] if "--junitxml" in s.get("run", "")
    )
    assert "-p tools.ci.coverage_run" in run
    assert jobs["python-coverage"]["needs"] == "python-tests-part"
    assert "python-coverage" in jobs["python-tests"]["needs"]
    gate = jobs["python-tests"]["steps"][0]
    assert gate["env"]["COVERAGE"] == "${{ needs.python-coverage.result }}"
    assert 'test "$COVERAGE" = success' in gate["run"]


def test_an_unmeasured_push_fails(
    root: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coverage_floor, "PARTS", 1)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    part(root, 0, measured=False)
    assert coverage_floor.main([str(root / "data"), "--python", "3.15"]) == 1
    assert "no part measured coverage" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("floor", "code"),
    [({SOURCE: 100}, "COVERAGE_BELOW_FLOOR"), ({}, "COVERAGE_FLOOR_MISSING")],
    ids=["below", "missing"],
)
def test_the_command_exits_one_on_a_floor_failure(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    floor: dict[str, int],
    code: str,
) -> None:
    monkeypatch.setattr(coverage_floor, "PARTS", 1)
    monkeypatch.setattr(coverage_floor, "REPOSITORY_ROOT", root)
    monkeypatch.setattr(coverage_floor, "critical_sources", lambda: [SOURCE])
    monkeypatch.setattr(coverage_floor, "floors", lambda: floor)
    part(root, 0)
    assert coverage_floor.main([str(root / "data"), "--python", "3.15"]) == 1
    assert code in capsys.readouterr().out
    monkeypatch.setattr(coverage_floor, "floors", lambda: {SOURCE: 50})
    assert coverage_floor.main([str(root / "data"), "--python", "3.15"]) == 0


def test_the_command_runs_with_coverage_alone_installed(tmp_path: Path) -> None:
    # The coverage job installs only `coverage` (#429): no YAML parser may be needed.
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "yaml.py").write_text("raise ImportError('not installed in the coverage job')\n")
    environment = {**os.environ, "PYTHONPATH": f"{blocked}{os.pathsep}{REPOSITORY_ROOT}"}
    environment["GITHUB_EVENT_NAME"] = "pull_request"
    data = tmp_path / "data"
    for number in range(PARTS):
        (data / f"coverage-3.15-{number}").mkdir(parents=True)
        (data / f"coverage-3.15-{number}" / coverage_floor.NOT_MEASURED).touch()
    done = subprocess.run(
        [sys.executable, "-m", "tools.ci.coverage_floor", str(data), "--python", "3.15"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
