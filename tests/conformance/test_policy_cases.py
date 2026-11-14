"""The normative policy cases, run (#441, #442, #443, #444): every `semantic-query`, `catalog`,
`explain` and `sequence` case of the `rows`, `columns`, `indirect-leakage`, `direct-query`,
`missing-attributes` and `cache` areas that is not pending,
on every engine it lists (each
catalog of `lake.toml` for the existing DuckLake), with its caller and its control, against the
fixture it is written for. The engines are served by `tests/support/security_engines.py`. On an
engine the case names in `pending_engines`, the request must be refused before any read there.

Each answer is checked whole: the outcome, the exact rows in order, the notice or the code, and
that no forbidden value is a whole token anywhere the caller receives: the result, its notices,
the caller's own view of the receipt, or the error's code and details. #447 runs every other
area through the same fixtures and engines. A `sequence` is run step by step with one result
cache (`tests/support/security_sequences.py`): its last answer is checked, and its control runs
the same steps without their changes, on a place of its own.
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
from tests.support import security_sequences as sequences
from tests.support.security_engines import Received, ask, served

AREAS = frozenset(
    {"rows", "columns", "indirect-leakage", "direct-query", "missing-attributes", "cache"}
)
KINDS = frozenset({"semantic-query", "catalog", "explain", "sequence"})


def _ours(case: Mapping[str, Any]) -> bool:
    """A case this module runs: a single request of its areas, or a `cache` sequence (#444);
    other areas' sequences (revocation, pages, exports) are #447's."""
    kind = case["request"]["kind"]
    if kind == "sequence":
        return bool(case["area"] == "cache")
    return case["area"] in AREAS and kind in KINDS


def _runnable() -> list[tuple[str, str, str]]:
    """(case, engine, catalog) for every case this module runs, its pending engines included:
    there the case must be refused before any read (`test_a_pending_engine_refuses_...`)."""
    found = []
    for name, case in sorted(cases().items()):
        if not _ours(case):
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


# Cases whose caller two policies withhold rows from, each with its own notice (#443): SEC-DQ-001
# (an allow-list without a row, a deny-list without a registration) and SEC-MISSING-003 (the
# `readings` and `consumers` allow-lists). Their control callers get none.
REPEATED = {"SEC-DQ-001": 2, "SEC-MISSING-003": 2}


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
        assert "pages" not in expect, (label, "not an outcome of these requests")
        missing = sorted(set(expect.get("text", ())) - tokens)
        missing += sorted(set(expect.get("members", ())) - received.members)
        assert not missing, (label, missing)
    if outcome == "absent":  # a catalog that omits these members entirely
        assert not set(expect["members"]) & received.members, (label, received.members)
    assert outcome in {"rows", "zero-rows", "error", "contains", "absent"}, (label, outcome)
    # Exact: the notice the case names (or lists among the texts it must contain), or none;
    # once per policy that withholds rows, so a few cases name it more than once (`REPEATED`).
    named = [expect["notice"]] * REPEATED.get(label, 1) if "notice" in expect else []
    if outcome == "contains":
        named = [t for t in expect.get("text", ()) if t in received.notices]
    assert received.notices == named, (label, received.notices)
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
    if case["request"]["kind"] == "sequence":
        control = case["control"]
        steps = case["request"]["steps"]
        # The control asks the last step as its own caller, the changes left out when it says.
        controlled = {
            **case["request"],
            "steps": [*steps[:-1], {**steps[-1], "caller": control["caller"]}],
        }
        for request, changes, expect, label in (
            (case["request"], True, case["expect"], name),
            (controlled, not control.get("without_changes"), {**control, "forbidden": ()},
             f"{name} control"),
        ):  # fmt: skip
            with served(found, engine, catalog) as place:
                *_, last = sequences.run(found, place, request, changes)
            check(last, expect, label)
        return
    with served(found, engine, catalog) as place:
        check(ask(found, place, case["request"], case["caller"]), case["expect"], name)
        control = case["control"]
        check(
            ask(found, place, control.get("request", case["request"]), control["caller"]),
            {**control, "forbidden": ()},
            f"{name} control",
        )


def test_the_cases_this_module_runs_are_the_issues_cases() -> None:
    """#441 AC2, #442 AC1, #443 AC1 and #444 AC1: the cases they took out of pending, and
    nothing else silently dropped."""
    taken = {n for n, e, _ in RUNNABLE if e not in cases()[n].get("pending_engines", {})}
    expected = {f"SEC-ROWS-00{i}" for i in range(1, 9)} | {"SEC-LEAK-001", "SEC-LEAK-002"}
    expected |= {f"SEC-DQ-00{i}" for i in range(1, 6)}
    expected |= {f"SEC-COLUMNS-00{i}" for i in range(1, 9)}  # #442
    expected |= {"SEC-LEAK-003", "SEC-LEAK-004"} | {f"SEC-LEAK-00{i}" for i in range(6, 10)}
    expected |= {f"SEC-MISSING-00{i}" for i in range(1, 5)}  # #443
    expected |= {f"SEC-CACHE-00{i}" for i in range(1, 5)}  # #444
    assert expected <= taken
    for name, case in cases().items():
        if _ours(case):
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
    request = case["request"]
    asked = [s["request"] for s in request["steps"]] if request["kind"] == "sequence" else [request]
    with served(found, engine, catalog) as place:
        for caller in (case["caller"], case["control"]["caller"]):
            for each in asked:
                received = ask(found, place, each, caller)
                assert received.code == "UNSUPPORTED_OPERATION", received
                assert received.rows is None
        assert place.reads == []


def test_the_pending_engines_are_the_existing_lakes_detail_pages() -> None:
    """#441 (Owner, option A): only the existing DuckLake waits, for its detail pages (P5-14);
    SEC-CACHE-004 asks a detail page too, so it waits there with them (#444)."""
    waiting = {(n, e): cases()[n]["pending_engines"][e] for n, e, _ in PENDING}
    assert waiting == {
        **{(f"SEC-DQ-00{i}", "existing-ducklake"): "P5-14" for i in range(1, 6)},
        ("SEC-CACHE-004", "existing-ducklake"): "P5-14",  # a detail page there (#444)
    }
