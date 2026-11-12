"""A row policy holds at every level of an answer (#441 AC4), on the `utility` fixture, for a
caller mapped to zones Z1 and Z2 (readings 1, 2, 3 and 5): the grand total and every subtotal of a
rollup, every page of a detail query, counts and distinct counts, and a sort with a limit, which
chooses among the caller's rows only. Each expected answer is computed here from the fixture's
rows, never read back from the engine.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from orvex.semantic.execution import answer

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from tests.conformance.test_detail_rows import FORM
from tests.conformance.test_security_scenarios import cases

Document = dict[str, Any]
FOUND = sf.fixture("utility", ce.VENDORS[0])
CALLER = cases()["SEC-ROWS-001"]["caller"] | {"user": "u-multi"}
ZONES = {"Z1", "Z2"}
ALLOWED = [r for r in FOUND.rows["marts.readings"] if r[3] in ZONES]


def asked(document: Document) -> list[list[str | None]]:
    with sf.managed_lake(FOUND) as lake:
        found = answer(
            json.dumps(document),
            FOUND.compiled,
            FOUND.policies(),
            dict(CALLER),
            ce.engine_for(lake),
        )
    return ce.rows_of(found)


def aggregate(**more: Any) -> Document:  # noqa: ANN401
    return {"kind": "semantic_query", "version": 1, "mode": "aggregate", **more}


def kwh(rows: list[list[str | None]]) -> str:
    return f"{sum(Decimal(r[4]) for r in rows if r[4] is not None):.2f}"


def test_the_grand_total_is_over_the_callers_rows_only() -> None:
    found = asked(aggregate(metrics=[{"metric": "total_kwh"}], totals="grand",
                            group_by=[{"member": "readings.zone_id"}]))  # fmt: skip
    by_zone = {z: kwh([r for r in ALLOWED if r[3] == z]) for z in sorted(ZONES)}
    assert found == [[z, v] for z, v in by_zone.items()] + [[None, kwh(ALLOWED)]]


def test_every_subtotal_of_a_rollup_is_over_the_callers_rows_only() -> None:
    found = asked(
        aggregate(
            metrics=[{"metric": "reading_count"}],
            group_by=[{"member": "readings.zone_id"}, {"member": "readings.consumer_id"}],
            totals="rollup",
            order=[{"by": "readings.zone_id"}, {"by": "readings.consumer_id"}],
        )
    )
    counted = {(r[3], r[1]) for r in ALLOWED}
    zone_totals = [row for row in found if row[0] is not None and row[1] is None]
    assert {(z, n) for z, _, n in zone_totals} == {
        (z, str(sum(1 for r in ALLOWED if r[3] == z))) for z in ZONES
    }
    assert {(z, c) for z, c, _ in found if z is not None and c is not None} == counted
    assert [row for row in found if row[0] is None] == [[None, None, str(len(ALLOWED))]]


def test_counts_and_distinct_counts_count_the_callers_rows_only() -> None:
    found = asked(aggregate(metrics=[{"metric": "reading_count"}, {"metric": "consumer_count"}]))
    assert found == [[str(len(ALLOWED)), str(len({r[1] for r in ALLOWED}))]]


def test_a_sort_with_a_limit_chooses_among_the_callers_rows_only() -> None:
    found = asked(
        aggregate(
            metrics=[{"metric": "total_kwh"}],
            group_by=[{"member": "readings.zone_id"}],
            order=[{"by": "total_kwh", "direction": "desc"}],
            limit="1",
        )
    )
    best = max(ZONES, key=lambda z: Decimal(kwh([r for r in ALLOWED if r[3] == z])))
    assert found == [[best, kwh([r for r in ALLOWED if r[3] == best])]]  # never Z3, the largest


def test_every_page_of_a_detail_query_holds_only_the_callers_rows() -> None:
    document: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "detail",
        "fields": [{"member": "readings.reading_id"}, {"member": "readings.zone_id"}],
        "order": [{"by": "readings.reading_id", "direction": "asc"}],
        "page": {"size": "1"},
    }
    seen: list[list[str | None]] = []
    with sf.managed_lake(FOUND) as lake:
        snapshot = ce.engine_for(lake).snapshot
        pages = {
            "cursors": FORM,
            "snapshot": snapshot,
            "pinned": lambda v: snapshot if v == str(snapshot["id"]) else None,
        }
        while True:
            found = answer(json.dumps(document), FOUND.compiled, FOUND.policies(), dict(CALLER),
                           ce.engine_for(lake, **pages))  # fmt: skip
            seen += ce.rows_of(found)
            following = found.result.envelope()["page"].get("next")
            if following is None:
                break
            document["page"] = {"size": "1", "after": following}
    assert seen == [[r[0], r[3]] for r in ALLOWED]  # one row a page, in order, none hidden
