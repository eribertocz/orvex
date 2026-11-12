"""The normative row-policy cases, run (#441): every `semantic-query` case of the `rows`,
`indirect-leakage` and `direct-query` areas that is not pending, on every engine it lists (each
catalog of `lake.toml` for the existing DuckLake), with its caller and its control, against the
fixture it is written for. The engines are served by `tests/support/security_engines.py`. On an
engine the case names in `pending_engines`, the request must be refused before any read there.

Each answer is checked whole: the outcome, the exact rows in order, the notice or the code, and
that no forbidden value is a whole token anywhere the caller receives: the result, its notices,
the caller's own view of the receipt, or the error's code and details. #447 runs every other
area through the same fixtures and engines.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from typing import Any

import pytest

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from tests.conformance.test_security_scenarios import SEPARATOR, cases
from tests.support.security_engines import Received, ask, served

AREAS = frozenset({"rows", "indirect-leakage", "direct-query"})


def _runnable() -> list[tuple[str, str, str]]:
    """(case, engine, catalog) for every case this module runs, its pending engines included:
    there the case must be refused before any read (`test_a_pending_engine_refuses_...`)."""
    found = []
    for name, case in sorted(cases().items()):
        if case["area"] not in AREAS or case["request"]["kind"] != "semantic-query":
            continue
        if "pending" in case:
            continue
        for engine in case["engines"]:
            if engine == "existing-ducklake":
                settings = sf.fixture(case["fixture"], ce.VENDORS[0]).lake_settings()
                found += [(name, engine, c) for c in settings["catalogs"]]
            else:
                found.append((name, engine, ""))
    return found


RUNNABLE = _runnable()
HOLDING = [(n, e, c) for n, e, c in RUNNABLE if e not in cases()[n].get("pending_engines", {})]


def expected_rows(text: str) -> tuple[list[str], list[list[str | None]]]:
    lines = list(csv.reader(io.StringIO(text)))
    return lines[0], [[None if v == "NULL" else v for v in row] for row in lines[1:]]


def check(received: Received, expect: Mapping[str, Any], label: str) -> None:
    outcome = expect["outcome"]
    if outcome == "error":
        assert received.code == expect["code"], (label, received)
    else:
        assert received.code is None, (label, received)
    if outcome == "rows":
        columns, rows = expected_rows(expect["rows"])
        assert received.columns == columns, (label, received.columns)
        assert received.rows == rows, (label, received.rows)
    if outcome == "zero-rows":
        assert received.rows == [], (label, received.rows)
    tokens = {token for text in received.texts for token in SEPARATOR.split(text)}
    if outcome == "contains":
        assert not {"members", "pages"} & set(expect), (label, "not a semantic-query outcome")
        missing = sorted(set(expect["text"]) - tokens)
        assert not missing, (label, missing)
    assert outcome in {"rows", "zero-rows", "error", "contains"}, (label, outcome)
    # Exact: the notice the case names (or lists among the texts it must contain), or none.
    named = [expect["notice"]] if "notice" in expect else []
    if outcome == "contains":
        named = [t for t in expect["text"] if t in received.notices]
    assert received.notices == named, (
        label,
        received.notices,
    )
    leaked = sorted(set(expect.get("forbidden", ())) & tokens)
    assert not leaked, (label, leaked)


@pytest.mark.parametrize(
    ("name", "engine", "catalog"),
    HOLDING,
    ids=[f"{n}-{e}{'-' + c if c else ''}" for n, e, c in HOLDING],
)
def test_a_row_policy_case_holds_on_its_engine(name: str, engine: str, catalog: str) -> None:
    case = cases()[name]
    found = sf.fixture(case["fixture"], ce.VENDORS[0])
    with served(found, engine, catalog) as place:
        check(ask(found, place, case["request"], case["caller"]), case["expect"], name)
        control = case["control"]
        check(
            ask(found, place, control.get("request", case["request"]), control["caller"]),
            {**control, "forbidden": ()},
            f"{name} control",
        )


def test_the_cases_this_module_runs_are_the_issues_cases() -> None:
    """#441 AC2: the 15 cases it took out of pending, and nothing else silently dropped."""
    taken = {n for n, e, _ in RUNNABLE if e not in cases()[n].get("pending_engines", {})}
    expected = {f"SEC-ROWS-00{i}" for i in range(1, 9)} | {"SEC-LEAK-001", "SEC-LEAK-002"}
    expected |= {f"SEC-DQ-00{i}" for i in range(1, 6)}
    assert expected <= taken
    for name, case in cases().items():
        if case["area"] in AREAS and case["request"]["kind"] == "semantic-query":
            assert name in taken or "pending" in case, name


PENDING = [(n, e, c) for n, e, c in RUNNABLE if e in cases()[n].get("pending_engines", {})]


@pytest.mark.parametrize(
    ("name", "engine", "catalog"), PENDING, ids=[f"{n}-{e}-{c}" for n, e, c in PENDING]
)
def test_a_pending_engine_refuses_before_any_read(name: str, engine: str, catalog: str) -> None:
    """A case pending on one engine is never counted as passing there: its request, for its
    caller and its control, is refused before that engine reads anything."""
    case = cases()[name]
    found = sf.fixture(case["fixture"], ce.VENDORS[0])
    with served(found, engine, catalog) as place:
        for caller in (case["caller"], case["control"]["caller"]):
            received = ask(found, place, case["request"], caller)
            assert received.code == "UNSUPPORTED_OPERATION", received
            assert received.rows is None
        assert place.reads == []


def test_the_pending_engines_are_the_existing_lakes_detail_pages() -> None:
    """#441 (Owner, option A): only the existing DuckLake waits, for its detail pages (P5-14)."""
    waiting = {(n, e): cases()[n]["pending_engines"][e] for n, e, _ in PENDING}
    assert waiting == {(f"SEC-DQ-00{i}", "existing-ducklake"): "P5-14" for i in range(1, 6)}
