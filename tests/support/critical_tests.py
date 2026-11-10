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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, Result[pytest.TestReport]]:
    outcome: Result[pytest.TestReport] = yield
    report = outcome.get_result()
    if not os.environ.get("CI") or not report.skipped:
        return
    try:
        relative = Path(item.path).resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return
    if critical.is_critical(relative, PATHS):
        kind = "expected to fail" if hasattr(report, "wasxfail") else "skipped"
        report.outcome = "failed"
        report.longrepr = f"a critical test may not be {kind} in CI: {report.longrepr}"
