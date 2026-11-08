"""Record each test's setup time and fixtures in the JUnit report (`-p tools.ci.timing`, #375).

JUnit's `time` is a test's whole duration. This plugin adds two properties to each test case:
`setup_s`, the seconds its fixtures took to set up, and `fixtures`, the fixtures it used.
`tools/ci/durations.py` reads them to tell the cost of a test from the cost of what it needs.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, object]:
    yield
    if call.when == "setup":
        item.user_properties.append(("setup_s", f"{call.duration:.4f}"))
        names = getattr(item, "fixturenames", ())
        item.user_properties.append(("fixtures", ",".join(sorted(names))))
