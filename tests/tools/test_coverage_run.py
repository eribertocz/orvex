"""#429: the coverage plugin measures only when asked, and then saves its data."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tools.repository import REPOSITORY_ROOT


def run_in(folder: Path, measure: bool) -> int:
    (folder / "test_x.py").write_text("def test_x() -> None:\n    assert True\n", "utf-8")
    environment = {k: v for k, v in os.environ.items() if k not in ("PYTEST_COVERAGE", "CI")}
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT)
    if measure:
        environment["PYTEST_COVERAGE"] = "1"
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "tools.ci.coverage_run",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(folder),
            "-c",
            "/dev/null",
            str(folder),
        ],
        cwd=folder,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode


def test_without_the_variable_nothing_is_measured(tmp_path: Path) -> None:
    assert run_in(tmp_path, measure=False) == 0
    assert not list(tmp_path.glob(".coverage*"))


def test_with_the_variable_each_process_saves_its_data(tmp_path: Path) -> None:
    assert run_in(tmp_path, measure=True) == 0
    assert len(list(tmp_path.glob(".coverage.*"))) == 1
