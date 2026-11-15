"""A pytest plugin that records one run of the end-to-end result-cache suite (#486 G1): each
case and its outcome, each result-cache operation a case called through its contract's
binding, and the implementation that answered. The record is written as JSON to the file
named by `SUBSTITUTION_RECORD`."""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from orvex.contracts.binding import Bound

CONTRACT = "result-cache"
RECORD: dict[str, Any] = {"cases": [], "operations": [], "implementations": []}
_CURRENT = {"case": ""}


def _recording(call: Any) -> Any:  # noqa: ANN401 - wraps `Bound.call`
    def recorded(self: Bound, operation: str, *arguments: object, **keywords: object) -> object:
        if self.contract.name == CONTRACT:
            RECORD["operations"].append([_CURRENT["case"], operation])
            implementation = type(object.__getattribute__(self, "_implementation"))
            name = f"{implementation.__module__}.{implementation.__qualname__}"
            if name not in RECORD["implementations"]:
                RECORD["implementations"].append(name)
        return call(self, operation, *arguments, **keywords)

    return recorded


def pytest_configure(config: pytest.Config) -> None:
    Bound.call = _recording(Bound.call)  # type: ignore[method-assign]


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    _CURRENT["case"] = item.name
    return (yield)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call" or (report.when == "setup" and not report.passed):
        RECORD["cases"].append([report.nodeid.rsplit("::", 1)[-1], report.outcome])


def pytest_sessionfinish(session: pytest.Session) -> None:
    Path(os.environ["SUBSTITUTION_RECORD"]).write_text(json.dumps(RECORD), encoding="utf-8")
