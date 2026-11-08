"""Test durations (#375): read from JUnit or from a CI log, the same ids pytest uses."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.ci import durations
from tools.repository import REPOSITORY_ROOT

SAMPLE = """
import time

import pytest


@pytest.fixture
def slow_setup():
    time.sleep(0.2)


def test_quick():
    pass


def test_with_setup(slow_setup):
    time.sleep(0.1)


@pytest.mark.parametrize("n", [1, 2])
def test_many(n):
    pass


class TestGroup:
    def test_inside(self):
        pass


@pytest.mark.skip(reason="not here")
def test_skipped():
    pass
"""


def run_sample(tmp_path: Path) -> tuple[Path, list[str]]:
    folder = tmp_path / "suite"
    folder.mkdir()
    (folder / "test_sample.py").write_text(SAMPLE, "utf-8")
    junit = tmp_path / "junit-3.15.xml"
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider", "suite"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tools.ci.timing",
            f"--junitxml={junit}",
            "suite",
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(REPOSITORY_ROOT), "PATH": ""},
        capture_output=True,
        check=True,
    )
    return junit, [line for line in collected if "::" in line]


def test_junit_ids_are_pytest_node_ids_with_setup_and_fixtures(tmp_path: Path) -> None:
    junit, collected = run_sample(tmp_path)
    timings = {t.test: t for t in durations.from_junit(junit, "3.15")}
    assert set(timings) == {c for c in collected if "skipped" not in c}  # a skip is not timed
    slow = timings["suite/test_sample.py::test_with_setup"]
    assert slow.setup >= 0.2
    assert slow.total >= 0.3
    assert "slow_setup" in slow.fixtures
    assert "suite/test_sample.py::TestGroup::test_inside" in timings


def test_a_log_carries_what_the_report_holds(tmp_path: Path) -> None:
    junit, _ = run_sample(tmp_path)
    log = [f"2026-11-08T01:40:39.4083139Z {line}" for line in durations.emit(junit)]
    log += ["2026-11-08T01:40:39.4083139Z some other output"]
    from_log = sorted(durations.from_log(log), key=lambda t: t.test)
    from_report = sorted(durations.from_junit(junit), key=lambda t: t.test)
    assert [(t.test, t.fixtures) for t in from_log] == [(t.test, t.fixtures) for t in from_report]
    assert all(abs(a.total - b.total) < 1e-3 for a, b in zip(from_log, from_report, strict=True))


def test_the_record_is_each_test_s_median_and_the_report_names_the_slowest(
    tmp_path: Path,
) -> None:
    timings = [
        durations.Timing("a.py::t", 1.0, 0.5, ("lake",), "3.14"),
        durations.Timing("a.py::t", 3.0, 0.5, ("lake",), "3.15"),
        durations.Timing("a.py::t", 2.0, 0.5, ("lake",), "3.15"),
        durations.Timing("packages/store/tests/b.py::u", 9.0, 8.0, ("worker",), "3.15"),
    ]
    assert durations.record(timings) == {"a.py::t": 2.0, "packages/store/tests/b.py::u": 9.0}
    text = durations.markdown(timings, "T")
    assert "| `packages/store/tests/b.py::u` | 3.15 | 9.0 | 8.0 |" in text
    assert "| store |" in text
    empty = tmp_path / "empty.log"
    empty.write_text("no timings here\n", "utf-8")
    assert durations.main(["report", str(empty), "--record", str(tmp_path / "r.json")]) == 1


def test_emit_without_a_report_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert durations.main(["emit", str(tmp_path / "absent.xml")]) == 0
    assert capsys.readouterr().out == ""


def test_report_writes_the_record_for_the_shards(tmp_path: Path) -> None:
    junit, _ = run_sample(tmp_path)
    out = tmp_path / "record.json"
    assert durations.main(["report", str(junit), "--record", str(out)]) == 0
    assert "suite/test_sample.py::test_quick" in json.loads(out.read_text("utf-8"))


def test_a_folder_or_class_named_like_a_test_still_maps_to_its_file(tmp_path: Path) -> None:
    (tmp_path / "test_suite").mkdir()
    (tmp_path / "test_suite" / "test_x.py").write_text("")
    assert (
        durations.node_id("test_suite.test_x.test_Class", "test_y", tmp_path)
        == "test_suite/test_x.py::test_Class::test_y"
    )


def test_a_log_carries_the_skipped_tests_by_reason(tmp_path: Path) -> None:
    # #377: the trend report lists the tests that did not run, from the CI log alone.
    junit, _ = run_sample(tmp_path)
    log = [f"2026-11-08T01:40:39.4083139Z {line}" for line in durations.emit(junit)]
    assert durations.skipped_from_log(log) == {"not here": 1}
    # The skip lines are not timings.
    assert all("skipped" not in t.test for t in durations.from_log(log))


def test_a_skip_reason_stays_on_one_line() -> None:
    assert durations.one_line("needs\tPostgreSQL\n  (CI only)") == "needs PostgreSQL (CI only)"
    assert durations.one_line("") == "no reason given"
