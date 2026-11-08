"""The ClickHouse engine wired as the product wires it (P2-31 PR 4, #225; ADR-0027 §4 to §8):
the store's verified read-only connection (`orvex.store.clickhouse`), the server's adapter
(`orvex.server.clickhouse_adapter`) and its evidence schedule (`clickhouse_evidence`), asked
through `orvex.semantic.execution.answer` as `server` asks.

- receipts name the engine, its version, the connection, the read instant and the policies'
  managed snapshots, and are `explainable`;
- `EXPLAIN ESTIMATE` is checked against the rows-scanned budget before the read;
- evidence violated on the schedule blocks the questions relying on it (`EVIDENCE_VIOLATED`),
  and receipts carry the verification instant of what they relied on;
- detail pages run on ClickHouse, page after page through their cursors, each with its own read
  instant; a float is ordered and compared as the managed lake does it, `NaN` included (the
  Reviewer's finding on #418), so a page never skips a `NaN` row.

The server is `SEMANTIC_TEST_CLICKHOUSE` (`test_clickhouse_adapter`): required in CI, skipped
elsewhere.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from orvex.semantic.adapters import clickhouse as ch
from orvex.semantic.adapters.clickhouse import CAPABILITIES as CLICKHOUSE_CAPABILITIES
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.correctness import analyze
from orvex.semantic.engines import Routing
from orvex.semantic.errors import SemanticError
from orvex.semantic.evidence import verify
from orvex.semantic.planning import plan
from orvex.semantic.query.binding import Limits as QueryLimits
from orvex.semantic.query.binding import bind_query
from orvex.server.clickhouse_adapter import ClickHouseAdapter
from orvex.server.clickhouse_engines import tables
from orvex.server.clickhouse_evidence import EvidenceSchedule, checked
from orvex.store.clickhouse import Budget, ClickHouse, ClickHouseTarget
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.conformance import test_clickhouse_adapter as base
from tests.conformance import test_detail_rows as details
from tests.conformance import test_direct_query as dq
from tests.conformance import test_evidence as ev
from tests.conformance import test_row_policies as rp
from tests.conformance.test_clickhouse_adapter import server  # noqa: F401 - the fixture
from tests.conformance.test_clickhouse_capabilities import copied
from tests.conformance.test_execution import ask, no_policies, tiny_lake, tiny_model

Document = dict[str, Any]
PASSWORD = "SEMANTIC_TEST_CLICKHOUSE_READER"
BUDGET = Budget(timeout_ms=30_000, memory_bytes=1_000_000_000, result_rows=1_000_000)
ENTRY = base.ENTRY.__class__(
    "warehouse", "clickhouse", "live", "localhost/test", CLICKHOUSE_CAPABILITIES
)


@pytest.fixture
def engine(server: base.Server, monkeypatch: pytest.MonkeyPatch) -> ClickHouse:  # noqa: F811
    """The store's verified connection, as the server's reader."""
    user, password = server.reader  # type: ignore[attr-defined]
    monkeypatch.setenv(PASSWORD, password)
    target = ClickHouseTarget(server.host, server.port, server.database, user, PASSWORD, tls=False)
    return ClickHouse.connect(target, BUDGET)


def routing_of(compiled: CompiledModel) -> Routing:
    datasets: Any = compiled.ossie.document.get("datasets", ())
    return Routing({str(d["name"]): ENTRY for d in datasets})


def wired(
    engine: ClickHouse,
    compiled: CompiledModel,
    **more: Any,  # noqa: ANN401
) -> tuple[ClickHouseAdapter, EvidenceSchedule, dict[str, Any]]:
    """The adapter, its schedule (checked once) and the engine arguments routing every dataset
    to ClickHouse."""
    _, _, names = tables(engine)
    routing = routing_of(compiled)
    schedule = EvidenceSchedule(
        "warehouse",
        None,
        lambda now: checked(compiled, routing, "warehouse", engine, names, now),
    )
    adapter = ClickHouseAdapter("warehouse", engine, names, evidence=schedule.records, **more)
    return adapter, schedule, {"routing": routing, "adapters": {"warehouse": adapter}}


def outcome(run: Any) -> Any:  # noqa: ANN401
    try:
        found = run()
    except SemanticError as error:
        return ("SemanticError", error.code, dict(error.details))
    return [dict(c) for c in found.result.envelope()["columns"]], ce.rows_of(found)


# --- receipts (law 4) ------------------------------------------------------------------------


def test_a_receipt_names_the_engine_its_version_the_connection_the_read_instant_and_the_policy(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
) -> None:
    policy, context = rp.policies(dq.ZONE), rp.caller(user_id="u-1")
    with rp.lake_of() as lake:
        base.load(server, rp.CATALOG, copied(lake, rp.CATALOG))
        _, schedule, routed = wired(engine, rp.COMPILED)
        before = engine.now()
        reference = ask(rp.query(), rp.COMPILED, lake, policy=policy, context=context)
        found = ask(rp.query(), rp.COMPILED, lake, policy=policy, context=context, **routed)
        snapshot = lake.current_snapshot().id
    assert ce.rows_of(found) == ce.rows_of(reference)
    document = found.receipt.document()
    assert document["execution"]["engine"] == "clickhouse"
    assert document["execution"]["engine_version"] == engine.version
    assert ".".join(engine.version.split(".")[:2]) in ("26.8", "26.9")
    assert document["execution"]["estimate"] == {"rows_scanned": len(rp.DATA["sales"])}
    reads = document["data"]
    live = [r for r in reads if r["route"] == "live"]
    assert live
    assert all(r["connection"] == "warehouse" and not r["retained"] for r in live)
    instants = {dt.datetime.fromisoformat(r["read_at"]) for r in live}
    assert len(instants) == 1
    assert before <= instants.pop() <= engine.now()
    managed = [r for r in reads if r["route"] == "managed"]
    assert managed
    assert all(r["for_policy"] and r["snapshot_id"] == snapshot for r in managed)
    assert document["reproducibility"]["class"] == "explainable"
    assert "live_read" in document["reproducibility"]["reasons"]
    assert schedule.records() == ()  # no check was due yet: evidence stays declared


# --- cost (law 7) -----------------------------------------------------------------------------


def test_explain_estimate_refuses_a_read_over_the_budget_before_it_runs(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
) -> None:
    with rp.lake_of() as lake:
        base.load(server, rp.CATALOG, copied(lake, rp.CATALOG))
        adapter, _, routed = wired(engine, rp.COMPILED, rows_scanned=3)
        read: list[str] = []
        real = adapter.read

        def counted(statement: str, *more: Any) -> Any:  # noqa: ANN401
            read.append(statement)
            return real(statement, *more)

        adapter.read = counted  # type: ignore[method-assign,assignment]
        found = outcome(lambda: ask(rp.query(), rp.COMPILED, lake, **routed))
        policy, context = rp.policies(rp.TENANT), rp.caller(tenant="A")
        restricted = outcome(
            lambda: ask(rp.query(), rp.COMPILED, lake, policy=policy, context=context, **routed)
        )
    assert found == (
        "SemanticError",
        "QUERY_BUDGET_EXCEEDED",
        {"budget": "rows_scanned", "limit": "3", "estimate": "4"},
    )
    # Under a row policy the estimate, which counts the rows it hides, is never named.
    assert restricted == (
        "SemanticError",
        "QUERY_BUDGET_EXCEEDED",
        {"budget": "rows_scanned", "limit": "3"},
    )
    assert read == []  # refused before any read of the question


def test_a_table_with_no_estimate_answers_under_the_limits_with_none_recorded(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
) -> None:
    with rp.lake_of() as lake:
        base.load(server, rp.CATALOG, copied(lake, rp.CATALOG), "Memory")
        _, _, routed = wired(engine, rp.COMPILED, rows_scanned=3)
        found = ask(rp.query(), rp.COMPILED, lake, **routed)
        reference = ask(rp.query(), rp.COMPILED, lake)
    assert ce.rows_of(found) == ce.rows_of(reference)
    assert "estimate" not in found.receipt.document()["execution"]


# --- evidence (ADR-0027 §7) -------------------------------------------------------------------
# The evidence suite's model and rows (`test_evidence`): every kind, checked by ClickHouse's own
# statements and compared with the managed lake's verification of the same rows.

EVIDENCE_ROWS = {
    "clean": ev.GOOD,
    "a duplicated key": ev.with_rows(customers=[["10", "West"]]),
    "an overlapping validity": ev.with_rows(
        customer_history=[["11", "business", "2026-03-01", "2026-04-01"]]
    ),
    "an orphan key": ev.with_rows(
        sales=[["4", "99", "2026-01-07", "1.00"], ["5", "99", "2026-01-08", "1.00"]]
    ),
}


def verified_on_both(server: base.Server, engine: ClickHouse, data: ev.Data) -> tuple[Any, Any]:  # noqa: F811
    """(ClickHouse's records, the managed lake's), as (name, kind) -> (status, violations)."""
    with ev.lake_of(data) as lake:
        base.load(server, ev.CATALOG, copied(lake, ev.CATALOG))
        managed = verify(ev.COMPILED, lake.read, lake.current_snapshot().id, at=ev.AT)
    _, _, names = tables(engine)
    found = checked(ev.COMPILED, routing_of(ev.COMPILED), "warehouse", engine, names, ev.AT)

    def keyed(records: Any) -> dict[tuple[str, str], Any]:  # noqa: ANN401
        return {(r.name, r.kind): (r.status, dict(r.violations)) for r in records}

    return keyed(found), keyed(managed.records)


@pytest.mark.parametrize("data", EVIDENCE_ROWS.values(), ids=list(EVIDENCE_ROWS))
def test_each_kind_of_evidence_is_counted_on_clickhouse_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
    data: ev.Data,
) -> None:
    found, reference = verified_on_both(server, engine, data)
    assert found == reference
    assert {kind for _, kind in found} == {
        "unique",
        "non_overlap",
        "cardinality",
        "referential_integrity",
    }  # append_only: declared


def test_violated_evidence_found_on_the_schedule_blocks_the_questions_relying_on_it(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
) -> None:
    question = ev.query("customers.region")  # joins through `sale_customer`: its cardinality
    with ev.lake_of(EVIDENCE_ROWS["a duplicated key"]) as lake:
        base.load(server, ev.CATALOG, copied(lake, ev.CATALOG))
        adapter, schedule, routed = wired(engine, ev.COMPILED)
        unchecked = outcome(lambda: ask(question, ev.COMPILED, lake, **routed))
        found = schedule.check(dt.datetime.now(dt.UTC))
        blocked = outcome(lambda: ask(question, ev.COMPILED, lake, **routed))
        unrelated = outcome(lambda: ask(ev.query("sales.sale_date"), ev.COMPILED, lake, **routed))
    assert unchecked[0] != "SemanticError"  # not verified yet: declared, and answered
    assert ("sale_customer", "cardinality", "violated") in {
        (r.name.lower(), r.kind, r.status) for r in found
    }
    assert blocked[:2] == ("SemanticError", "EVIDENCE_VIOLATED")
    assert unrelated[0] != "SemanticError"  # a question that does not rely on it answers
    assert adapter.evidence() == found


def test_a_receipt_carries_the_verification_instant_of_the_evidence_it_relied_on(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
) -> None:
    at = dt.datetime(2026, 11, 8, 9, 30, tzinfo=dt.UTC)
    with ev.lake_of(ev.GOOD) as lake:
        base.load(server, ev.CATALOG, copied(lake, ev.CATALOG))
        _, schedule, routed = wired(engine, ev.COMPILED)
        schedule.check(at)
        found = ask(ev.query("customers.region"), ev.COMPILED, lake, **routed)
    relied = [e for e in found.receipt.document()["evidence"] if e["kind"] == "cardinality"]
    assert relied == [
        {
            "dataset": relied[0]["dataset"],
            "kind": "cardinality",
            "status": "verified",
            "verified_at": "2026-11-08T09:30:00.000000Z",
        }
    ]


def test_every_evidence_statement_on_clickhouse_passes_the_allow_lists_and_counts_in_bigint(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
) -> None:
    with ev.lake_of(ev.GOOD) as lake:
        base.load(server, ev.CATALOG, copied(lake, ev.CATALOG))
    _, _, names = tables(engine)
    checks = ch.ClickHouseChecks(names)

    def read(request: Any) -> Any:  # noqa: ANN401
        counts = [(n, "BIGINT") for n in checks.written[request.sql]]
        return engine.read(request.sql, request.parameters, counts, request.timeout_ms)

    verify(ev.COMPILED, read, 0, at=ev.AT, checks=checks)
    assert len(checks.written) >= 6
    for statement in checks.written:  # each passed `check_statement` when it was written
        assert "toInt64(count(*))" in statement
        assert "SETTINGS" not in statement


# --- detail pages, each with its own read instant (P2-24) ------------------------------------


def pages_of(lake: Lake, document: Document, compiled: CompiledModel, **routed: Any) -> Any:  # noqa: ANN401
    """Every page of one question through its cursors: (rows of each page, each receipt)."""
    engine = details.Pager(lake).engine()
    found = ask(document, compiled, lake, **engine, **routed)
    pages, receipts = [ce.rows_of(found)], [found.receipt.document()]
    while "next" in found.result.envelope()["page"]:
        after = found.result.envelope()["page"]["next"]
        paged = document | {"page": {**document["page"], "after": after}}
        found = ask(paged, compiled, lake, **engine, **routed)
        pages.append(ce.rows_of(found))
        receipts.append(found.receipt.document())
    return pages, receipts


DETAILS = {
    "by amount, largest first": details.query(),
    "by a field with NULL, NULL first": details.query(
        1, [{"by": "customers.country", "direction": "desc", "nulls": "first"}]
    ),
    "by a joined field, NULL last": details.query(
        3, [{"by": "customers.country", "direction": "asc"}]
    ),
}


@pytest.mark.parametrize("document", DETAILS.values(), ids=list(DETAILS))
def test_every_detail_page_answers_on_clickhouse_through_its_cursor_with_its_own_read_instant(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
    document: Document,
) -> None:
    with rp.lake_of() as lake:
        base.load(server, rp.CATALOG, copied(lake, rp.CATALOG))
        _, _, routed = wired(engine, rp.COMPILED)
        found, receipts = pages_of(lake, document, rp.COMPILED, **routed)
        reference, _ = pages_of(lake, document, rp.COMPILED)
    assert len(found) > 1
    assert found == reference
    instants = [r["data"][0]["read_at"] for r in receipts]
    assert all(r["data"][0]["route"] == "live" for r in receipts)
    assert len(set(instants)) == len(instants)  # each page read at its own instant
    assert all(r["execution"]["engine"] == "clickhouse" for r in receipts)


# --- floats: NaN ordered and compared as the managed lake does it ----------------------------

FLOAT_ROWS: list[dict[str, str | None]] = [
    {"id": "1", "x": "2.5"},
    {"id": "2", "x": "nan"},
    {"id": "3", "x": None},
    {"id": "4", "x": "-inf"},
    {"id": "5", "x": "inf"},
    {"id": "6", "x": "nan"},
    {"id": "7", "x": "1"},
    {"id": "8", "x": "-0.5"},
]


def float_model() -> tuple[CompiledModel, dict[str, Any]]:
    return tiny_model([("id", "BIGINT"), ("x", "DOUBLE")], "COUNT(*)")


def float_pages(direction: str, nulls: str) -> Document:
    return {
        "kind": "semantic_query",
        "version": 1,
        "mode": "detail",
        "fields": [{"member": "t.id"}, {"member": "t.x"}],
        "order": [{"by": "t.x", "direction": direction, "nulls": nulls}],
        "page": {"size": "2"},
    }


@pytest.mark.parametrize(
    ("direction", "nulls"), [("asc", "last"), ("asc", "first"), ("desc", "last"), ("desc", "first")]
)
def test_a_detail_page_ordered_by_a_float_never_skips_a_nan_row(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
    direction: str,
    nulls: str,
) -> None:
    compiled, catalog = float_model()
    with tiny_lake(catalog, FLOAT_ROWS) as lake:
        base.load(server, catalog, copied(lake, catalog))
        _, _, routed = wired(engine, compiled)
        found, _ = pages_of(lake, float_pages(direction, nulls), compiled, **routed)
        reference, _ = pages_of(lake, float_pages(direction, nulls), compiled)
    assert found == reference
    assert sorted(r[0] for page in found for r in page) == [str(i) for i in range(1, 9)]


FLOAT_QUESTIONS = {
    "a range above a number keeps NaN": {"op": "range", "gt": {"type": "number", "value": "1"}},
    "a range up to a number leaves NaN out": {
        "op": "range",
        "lte": {"type": "number", "value": "1"},
    },
    "a closed range": {
        "op": "range",
        "gte": {"type": "number", "value": "-1"},
        "lt": {"type": "number", "value": "3"},
    },
}


@pytest.mark.parametrize("predicate", FLOAT_QUESTIONS.values(), ids=list(FLOAT_QUESTIONS))
def test_a_float_range_filter_compares_nan_as_the_managed_lake(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
    predicate: Document,
) -> None:
    compiled, catalog = float_model()
    document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "total"}],
        "filters": [{"scope": "board", "member": "t.x", "predicate": predicate}],
    }
    with tiny_lake(catalog, FLOAT_ROWS) as lake:
        base.load(server, catalog, copied(lake, catalog))
        _, _, routed = wired(engine, compiled)
        found = outcome(lambda: ask(document, compiled, lake, **routed))
        reference = outcome(lambda: ask(document, compiled, lake))
    assert found == reference


def test_a_float_group_ordered_by_its_value_puts_nan_where_the_managed_lake_does(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
) -> None:
    compiled, catalog = float_model()
    document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "total"}],
        "group_by": [{"member": "t.x"}],
        "order": [{"by": "t.x", "direction": "desc", "nulls": "last"}],
    }
    with tiny_lake(catalog, FLOAT_ROWS) as lake:
        base.load(server, catalog, copied(lake, catalog))
        _, _, routed = wired(engine, compiled)
        found = outcome(lambda: ask(document, compiled, lake, **routed))
        reference = outcome(lambda: ask(document, compiled, lake))
    assert found == reference


def test_a_float_sort_key_and_keyset_compare_nan_explicitly() -> None:
    compiled, catalog = float_model()
    bound = bind_query(
        compiled,
        json.dumps(float_pages("desc", "last")),
        instant=ce.INSTANT,
        limits=QueryLimits(detail_page_size=1000),
        hidden=lambda _: False,
    )
    report = analyze(bound, compiled, no_policies(compiled), ce.viewer_context())
    statement = ch.compile_plan(plan(report, ("nan", "2")), base.names_of(catalog), {}).sql
    assert 'isNaN("#r"."x") DESC NULLS LAST, "#r"."x" DESC NULLS LAST' in statement
    # After a NaN, descending: a smaller value is one that is not NaN, or a smaller number.
    assert 'if(isNaN("x") OR isNaN(' in statement
    assert (
        '("x" IS NOT DISTINCT FROM' not in statement.split("WHERE", 2)[-1] or "isNaN" in statement
    )
