"""Test-time trends (#377, #373 §6): one entry per main build, sustained rises only."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.ci import durations, trends
from tools.repository import REPOSITORY_ROOT


def entry(run: int, total: float, slowest: float = 0.0, **extra: object) -> trends.Entry:
    values: dict[str, object] = {
        "run": run,
        "profile": "full",
        "sha": f"{run:07d}",
        "date": f"2026-11-{run % 28 + 1:02d}T00:00:00Z",
        "conclusion": "success",
        "url": f"https://example.invalid/runs/{run}",
        "tests": 100,
        "total_s": total,
        "slowest_part": "python-tests-3.15-part-0",
        "slowest_s": slowest or total / 10,
        "skipped": {},
    }
    values.update(extra)
    return trends.Entry(**values)  # type: ignore[arg-type]


def history(*totals: float) -> list[trends.Entry]:
    return [entry(index + 1, total) for index, total in enumerate(totals)]


STEADY = (100.0, 102.0, 98.0, 101.0, 99.0, 100.0)


def test_a_rise_sustained_over_the_window_is_flagged() -> None:
    flags = trends.rises(history(*STEADY, 130, 131, 129, 135, 132))
    assert [f.metric for f in flags] == ["total_s", "slowest_s"]
    assert "full" in flags[0].message
    assert "+" in flags[0].message


@pytest.mark.parametrize(
    "recent",
    [
        (100, 101, 99, 100, 180),  # one noisy run
        (130, 131, 100, 135, 132),  # a dip inside the window: not sustained
        (110, 112, 111, 113, 110),  # under the threshold
    ],
)
def test_noise_or_a_small_change_is_not_flagged(recent: tuple[float, ...]) -> None:
    assert trends.rises(history(*STEADY, *recent)) == []


def test_too_few_comparable_runs_flag_nothing() -> None:
    assert trends.rises(history(100, 100, 100, 200, 200, 200, 200, 200)) == []


def test_only_comparable_runs_count() -> None:
    # Failed runs and the other profile are not comparable: they neither raise nor hide a rise.
    found = history(*STEADY)
    found += [entry(50 + i, 130) for i in range(4)]
    found.insert(8, entry(40, 20, conclusion="failure"))
    found.insert(9, entry(41, 20, profile="nightly"))
    assert trends.rises(found) == []
    found.append(entry(60, 133))
    assert [f.metric for f in trends.rises(found)] == ["total_s", "slowest_s"]


def write_logs(folder: Path) -> Path:
    folder.mkdir()
    lines = {
        "python-tests-3.15-part-0": ["a.py::t\t2.0", "a.py::u\t1.0"],
        "python-tests-3.14-part-1": ["b.py::t\t5.5"],
    }
    for name, tests in lines.items():
        text = [
            f"{durations.MARK}\t{t.split(chr(9))[1]}\t0.0\t{t.split(chr(9))[0]}\t" for t in tests
        ]
        text.append(f"{durations.SKIPPED}\tneeds PostgreSQL\tc.py::v")
        (folder / f"{name}.txt").write_text("\n".join(text) + "\n", "utf-8")
    (folder / "python-lint.txt").write_text("ruff: all good\n", "utf-8")
    return folder


def test_an_entry_holds_the_total_and_the_slowest_part_from_the_logs(tmp_path: Path) -> None:
    found = trends.measure(
        write_logs(tmp_path / "logs"),
        run=7,
        workflow="python",
        sha="abc",
        date="2026-11-07T00:00:00Z",
        conclusion="success",
        url="u",
    )
    assert found.profile == "full"
    assert found.total_s == 8.5
    assert (found.slowest_part, found.slowest_s) == ("python-tests-3.14-part-1", 5.5)
    assert found.tests == 5  # three timed, two skipped
    assert found.skipped == {"needs PostgreSQL": 2}
    nightly = trends.measure(
        tmp_path / "logs",
        run=8,
        workflow="property-nightly",
        sha="",
        date="",
        conclusion="",
        url="",
    )
    assert nightly.profile == "nightly"
    with pytest.raises(ValueError, match="unknown workflow"):
        trends.measure(tmp_path / "logs", 9, "web", "", "", "", "")


def test_update_appends_once_per_run_and_reports(tmp_path: Path) -> None:
    logs = write_logs(tmp_path / "logs")
    trend = tmp_path / "test-trends.json"
    summary = tmp_path / "summary.md"
    arguments = [
        "--history",
        str(trend),
        "--logs",
        str(logs),
        "--run",
        "7",
        "--workflow",
        "python",
        "--sha",
        "abc",
        "--date",
        "2026-11-07T00:00:00Z",
        "--conclusion",
        "success",
        "--url",
        "https://example.invalid/runs/7",
        "--summary",
        str(summary),
    ]
    assert trends.main(arguments) == 0  # a missing history starts a new one
    assert trends.main(arguments) == 0  # the same run again is not counted twice
    saved = json.loads(trend.read_text("utf-8"))
    assert [e["run"] for e in saved] == [7]
    text = summary.read_text("utf-8")
    assert "| full | success |" in text
    assert "| nightly | no run recorded |" in text
    assert "needs PostgreSQL" in text
    assert "Tests in no profile: none" in text
    assert "No sustained rise" in text


def test_the_report_shows_a_flag_and_the_tests_in_no_profile() -> None:
    found = history(*STEADY, 130, 131, 129, 135, 132)
    text = trends.report(found, ["tools/test_stray.py"])
    assert "Sustained rise" in text
    assert "`tools/test_stray.py`" in text


def test_no_test_file_is_in_no_profile(tmp_path: Path) -> None:
    assert trends.unprofiled(REPOSITORY_ROOT) == []
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = []\n', "utf-8"
    )
    for name in ("tests/test_a.py", "stray/test_b.py"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("", "utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    assert trends.unprofiled(tmp_path, discovered=".github/workflows/scripts") == [
        "stray/test_b.py"
    ]
