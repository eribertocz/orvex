"""A pytest plugin: in CI a critical test that skips, expects to fail or unexpectedly passes is a
failure (#427). Which tests are critical: `tools.ci.critical`. Outside CI it changes nothing."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest
from pluggy import Result

from tools.ci import critical
from tools.repository import REPOSITORY_ROOT

PATHS = critical.critical_paths()


def critical_path(path: object) -> bool:
    """Whether a collected file is in the critical set; only ever true in CI."""
    if not os.environ.get("CI"):
        return False
    try:
        relative = Path(str(path)).resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return False
    return critical.is_critical(relative, PATHS)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, Result[pytest.TestReport]]:
    outcome: Result[pytest.TestReport] = yield
    report = outcome.get_result()
    expected_failure = hasattr(report, "wasxfail")
    if not (report.skipped or (report.passed and expected_failure)):
        return
    if critical_path(item.path):
        if report.passed:
            kind = "marked to fail while it passes"
        else:
            kind = "expected to fail" if expected_failure else "skipped"
        report.outcome = "failed"
        report.longrepr = f"a critical test may not be {kind} in CI: {report.longrepr}"


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, Result[pytest.CollectReport]]:
    """A module skipped while collecting (`allow_module_level`, `importorskip`) reports no test,
    so the check is on the collection report itself."""
    outcome: Result[pytest.CollectReport] = yield
    report = outcome.get_result()
    if report.skipped and isinstance(collector, pytest.File) and critical_path(collector.path):
        report.outcome = "failed"
        report.longrepr = f"a critical module may not be skipped in CI: {report.longrepr}"
