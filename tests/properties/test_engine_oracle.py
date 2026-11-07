"""P2-28 (#367): generated questions against two independent oracles.

A valid, supported question must be answered, and the answer must equal the pure-Python
`Decimal` oracle exactly; questions without the ratio must also equal plain DuckDB SQL. A
refusal on a valid question fails the test, so an engine that rejects everything cannot pass
(Owner correction 2)."""

from __future__ import annotations

import json

import pyarrow as pa
from hypothesis import example, given
from orvex.semantic.canonical import canonicalize
from orvex.semantic.receipt.viewer import CallerViewer
from orvex.server.semantic_http import RECEIPT_KEY, arrow_answer

from conformance.runners import corpus_execution as ce
from conformance.runners.corpus_http import equivalent
from tests.properties import oracle
from tests.properties.world import (
    COMPILED,
    NO_POLICIES,
    PROFILE,
    Document,
    World,
    aggregate_queries,
    ask,
    detail_queries,
    lake_of,
    sorted_rows,
    worlds,
)

# Committed counterexample material: the DST instants, a zero denominator, an orphan buyer,
# a NULL amount and a NULL channel together.
EDGE = World(
    customers=[
        {"customer_id": "1", "region": "North", "segment": "a"},
        {"customer_id": "2", "region": None, "segment": None},
    ],
    sales=[
        {
            "sale_id": "1",
            "customer_id": "1",
            "amount": "0.05",
            "qty": "2.00",
            "sold_on": "2026-01-31",
            "sold_at": "2026-02-01 02:59:00+00",  # January in Santiago, February in UTC
            "channel": "web",
        },
        {
            "sale_id": "2",
            "customer_id": "9",
            "amount": None,
            "qty": "0.00",
            "sold_on": "2026-02-01",
            "sold_at": "2026-04-05 03:00:00+00",
            "channel": None,
        },
        {
            "sale_id": "3",
            "customer_id": None,
            "amount": "-0.01",
            "qty": "0.50",
            "sold_on": "2026-01-01",
            "sold_at": "2026-09-06 04:00:00+00",
            "channel": "shop",
        },
    ],
)
EVERYTHING: Document = {
    "kind": "semantic_query",
    "version": 1,
    "mode": "aggregate",
    "metrics": [{"metric": "revenue"}, {"metric": "buyers"}, {"metric": "unit_price"}],
    "group_by": [
        {"member": "customers.region"},
        {"member": "sales.sold_at", "bucket": {"unit": "month"}},
    ],
}
PAGED: Document = {
    "kind": "semantic_query",
    "version": 1,
    "mode": "detail",
    "fields": [{"member": "sales.sale_id"}, {"member": "sales.amount"}],
    "order": [{"by": "sales.sale_id", "direction": "asc"}],
    "page": {"size": "2"},
}


def three_way(world: World, query: Document) -> list[list[str | None]]:
    """The engine's rows, asserted equal to both oracles; obligations all discharged."""
    with lake_of(world) as lake:
        found = ask(query, lake)
    got = sorted_rows(ce.rows_of(found))
    assert got == sorted_rows(oracle.expected(world.customers, world.sales, query))
    with_ratio = any(entry["metric"] == "unit_price" for entry in query.get("metrics", ()))
    if query["mode"] == "aggregate" and not with_ratio:
        expected = oracle.duckdb_expected(world.customers, world.sales, query)
        assert got == sorted_rows(expected)
    obligations = found.receipt.document()["plan"]["obligations"]
    assert obligations
    assert all(entry.get("evidence") for entry in obligations)  # C9: every one discharged
    return got


@PROFILE
@example(EDGE, EVERYTHING)
@given(worlds(), aggregate_queries())
def test_a_valid_aggregate_equals_both_oracles_exactly(world: World, query: Document) -> None:
    three_way(world, query)


@PROFILE
@example(EDGE, EVERYTHING)
@given(worlds(), aggregate_queries())
def test_grand_totals_reconcile_with_the_oracle_by_each_metric_s_own_rule(
    world: World, query: Document
) -> None:
    """C2 and C5: the total level recomputed, never summed — the additive sum of the engine's
    own groups equals its grand total, and the distinct count's grand total is the oracle's
    recount over all visible rows, not a sum of the groups'."""
    from decimal import Decimal

    grand = {key: value for key, value in query.items() if key != "group_by"}
    with lake_of(world) as lake:
        grouped = ce.rows_of(ask(query, lake))
        total = ce.rows_of(ask(grand, lake))
    assert sorted_rows(total) == sorted_rows(oracle.expected(world.customers, world.sales, grand))
    metrics = [entry["metric"] for entry in query["metrics"]]
    offset = len(query.get("group_by", []))
    (total_row,) = total
    if "revenue" in metrics:
        column = offset + metrics.index("revenue")
        values = [Decimal(text) for row in grouped if (text := row[column]) is not None]
        summed = sum(values, Decimal(0)) if values else None
        total_text = total_row[metrics.index("revenue")]  # the grand total has no dimensions
        assert summed == (None if total_text is None else Decimal(total_text))  # additive
    if "buyers" in metrics:
        column = offset + metrics.index("buyers")
        naive = sum(int(text) for row in grouped if (text := row[column]) is not None)
        total_text = total_row[metrics.index("buyers")]
        assert total_text is not None
        assert int(total_text) <= naive  # non-additive: recounted, never summed


@PROFILE
@example(EDGE, PAGED)
@given(worlds(), detail_queries())
def test_detail_pages_concatenate_to_every_row_exactly_once_in_order(
    world: World, query: Document
) -> None:
    rows: list[list[str | None]] = []
    pages = 0
    with lake_of(world) as lake:
        document = query
        while True:
            found = ask(document, lake)
            envelope = found.result.envelope()
            rows += ce.rows_of(found)
            pages += 1
            cursor = envelope["page"].get("next")
            if cursor is None:
                break
            assert len(rows) == pages * int(query["page"]["size"])  # full pages before the last
            document = {**query, "page": {**query["page"], "after": cursor}}
    expected = oracle.expected(world.customers, world.sales, query)
    assert rows == sorted(expected, key=lambda row: int(str(row[0])))  # by the unique key


@PROFILE
@example(EDGE, EVERYTHING)
@given(worlds(), aggregate_queries())
def test_the_same_question_twice_is_the_same_answer(world: World, query: Document) -> None:
    """Semantic determinism (Owner correction 6): equal canonical results and equal execution
    fingerprints; volatile metadata (receipt ids, timings) is outside both."""
    from orvex.store.json_encoding import arrow_to_json

    with lake_of(world) as lake:
        first = ask(query, lake)
        second = ask(query, lake)
    assert first.result.canonical(arrow_to_json) == second.result.canonical(arrow_to_json)
    one = first.receipt.document()
    two = second.receipt.document()
    assert one["execution_fingerprint"] == two["execution_fingerprint"]


@PROFILE
@example(EDGE, EVERYTHING)
@given(worlds(), aggregate_queries())
def test_arrow_and_json_are_the_same_generated_answer(world: World, query: Document) -> None:
    """Owner correction 3 (HTTP): the #363 equivalence, on generated answers."""
    from orvex.store.json_encoding import arrow_to_json

    with lake_of(world) as lake:
        found = ask(query, lake)
    context = {**ce.viewer_context(), "role": "builder"}
    receipt = found.receipt.view(CallerViewer(COMPILED, NO_POLICIES, context))
    document = {"result": json.loads(found.result.wire(arrow_to_json)), "receipt": receipt}
    table = arrow_answer(found.result, receipt)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    arrived = pa.ipc.open_stream(sink.getvalue()).read_all()
    assert arrived.schema.metadata[RECEIPT_KEY] == canonicalize(receipt)
    equivalent(document, arrived)
