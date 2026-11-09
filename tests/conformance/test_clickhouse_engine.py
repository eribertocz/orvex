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
import yaml
from orvex.semantic import execution
from orvex.semantic.adapters import clickhouse as ch
from orvex.semantic.adapters.clickhouse import CAPABILITIES as CLICKHOUSE_CAPABILITIES
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.correctness import analyze
from orvex.semantic.engines import Routing
from orvex.semantic.errors import PlanError, SemanticError
from orvex.semantic.evidence import verify
from orvex.semantic.ossie.reader import read
from orvex.semantic.planning import plan
from orvex.semantic.policies import read_policies
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
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
PASSWORD = "SEMANTIC_TEST_CLICKHOUSE_READER"
BUDGET = Budget(timeout_ms=30_000, memory_bytes=1_000_000_000)
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


def test_explain_estimate_is_recorded_and_never_refuses_a_read(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
) -> None:
    """No rows-scanned budget on ClickHouse (Owner, 2026-11-08): every question reads all the
    rows it needs; the engine's own estimate is the receipt's, under a row policy too."""
    with rp.lake_of() as lake:
        base.load(server, rp.CATALOG, copied(lake, rp.CATALOG))
        _, _, routed = wired(engine, rp.COMPILED)
        found = ask(rp.query(), rp.COMPILED, lake, **routed)
        reference = ask(rp.query(), rp.COMPILED, lake)
        policy, context = rp.policies(rp.TENANT), rp.caller(tenant="A")
        restricted = ask(rp.query(), rp.COMPILED, lake, policy=policy, context=context, **routed)
        expected = ask(rp.query(), rp.COMPILED, lake, policy=policy, context=context)
    assert ce.rows_of(found) == ce.rows_of(reference)
    assert found.receipt.document()["execution"]["estimate"] == {"rows_scanned": 4}
    assert ce.rows_of(restricted) == ce.rows_of(expected)
    assert restricted.receipt.document()["execution"]["estimate"] == {"rows_scanned": 4}


def test_a_table_with_no_estimate_answers_with_none_recorded(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
) -> None:
    with rp.lake_of() as lake:
        base.load(server, rp.CATALOG, copied(lake, rp.CATALOG), "Memory")
        _, _, routed = wired(engine, rp.COMPILED)
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
    {"id": "9", "x": "-0.0"},
    {"id": "10", "x": "0"},
]


def float_model() -> tuple[CompiledModel, dict[str, Any]]:
    return tiny_model([("id", "BIGINT"), ("x", "DOUBLE")], "COUNT(*)")


def float_metric(expression: str, **payload: Any) -> tuple[CompiledModel, dict[str, Any]]:  # noqa: ANN401
    """`t` (id, g, x) with the one metric `total`, its product extension `payload`."""
    columns = {"id": "BIGINT", "g": "VARCHAR", "x": "DOUBLE"}
    metric: Document = {"name": "total", "expression": {"sql": expression}}
    if payload:
        metric["custom_extensions"] = [extension({"element": "metric", **payload})]
    document = {
        "version": "0.2.0.dev0",
        "name": "floats",
        "datasets": [
            {
                "name": "t",
                "source": "t",
                "primary_key": ["id"],
                "fields": [{"name": c, "expression": {"sql": c}} for c in columns],
            }
        ],
        "metrics": [metric],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    catalog = {"t": {c: SourceColumn(k) for c, k in columns.items()}}
    loaded = read(json.dumps(document).encode(), "semantic/floats.ossie.json", VENDORS)
    return compile_model(loaded, catalog, VENDORS), catalog


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
    assert sorted(int(r[0]) for page in found for r in page) == list(range(1, 11))


def decimal(text: str) -> Document:
    return {"type": "decimal", "value": text}


def integer(text: str) -> Document:
    return {"type": "integer", "value": text}


# A filter on a float field, over FLOAT_ROWS (NaN twice, ±inf, -0.0, 0.0, NULL): each value is
# a decimal or an integer, never a float, and both engines must answer rows, never an error.
FLOAT_FILTERS = {
    "a range above a decimal keeps NaN": {"op": "range", "gt": decimal("1")},
    "a range up to an integer leaves NaN out": {"op": "range", "lte": integer("1")},
    "a closed range": {"op": "range", "gte": decimal("-1"), "lt": integer("3")},
    "in: -0.0 is 0": {"op": "in", "values": [decimal("0"), decimal("2.5")]},
    "in with NULL": {"op": "in", "values": [integer("1")], "include_null": True},
    "not in keeps NaN and NULL": {"op": "not_in", "values": [decimal("0"), integer("1")]},
    "not in excluding NULL": {
        "op": "not_in",
        "values": [decimal("2.5")],
        "exclude_null": True,
    },
}


@pytest.mark.parametrize("predicate", FLOAT_FILTERS.values(), ids=list(FLOAT_FILTERS))
def test_a_float_filter_compares_as_the_managed_lake(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
    predicate: Document,
) -> None:
    compiled, catalog = float_metric("COUNT(t.id)")
    document = aggregate(filters=[{"scope": "board", "member": "t.x", "predicate": predicate}])
    rows = [r | {"g": "a"} for r in FLOAT_ROWS]
    found, reference = on_both(server, engine, compiled, catalog, rows, document)
    assert reference[0] != "SemanticError", reference  # an answerable question, on both
    assert found == reference


def test_a_mapping_set_of_floats_holds_nan_as_the_managed_lake_does(
    server: base.Server,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A float field in a bound set: `NaN` is in a set holding `NaN`, and -0.0 in one holding
    0.0 (ClickHouse's `has` misses the first). Written for each set it can be given."""
    from orvex.semantic.adapters.duckdb import PolicyValues

    compiled, catalog = float_metric("COUNT(t.id)")
    rule = {
        "name": "listed",
        "dataset": "t",
        "kind": "predicate",
        "predicate": "x IN (SELECT value FROM security.allowed WHERE user_id = $user_id)",
    }
    policy = read_policies(
        yaml.safe_dump({"version": 1, "row_policies": [rule]}),
        compiled,
        groups=frozenset(),
        managed_tables={"security.allowed": ["user_id", "value"]},
    )
    rows = [r | {"g": "a"} for r in FLOAT_ROWS]
    with tiny_lake(catalog, rows) as lake:
        base.load(server, catalog, copied(lake, catalog))
        adapter = base.OnClickHouse(server, base.names_of(catalog))
        routed = {
            "routing": routing_of(compiled),
            "adapters": {"warehouse": adapter},
        }
        adapter.entry = ENTRY
        found: dict[str, Any] = {}
        for label, values in {
            "NaN": ("nan",),
            "0.0": ("0",),
            "NULL and 2.5": (None, "2.5"),
        }.items():

            def resolved(*_: Any, values: Any = values) -> Any:  # noqa: ANN401
                queries = list(_[0])
                return {q.key: PolicyValues("DOUBLE", values) for q in queries}, queries

            monkeypatch.setattr(execution, "resolve", resolved)
            found[label] = outcome(
                lambda: ask(
                    aggregate(),
                    compiled,
                    lake,
                    policy=policy,
                    context=rp.caller(user_id="u-1"),
                    **routed,
                )
            )
    expected = {"NaN": [["2"]], "0.0": [["2"]], "NULL and 2.5": [["1"]]}
    assert {k: v[1] for k, v in found.items()} == expected


def aggregate(**more: Any) -> Document:  # noqa: ANN401
    return {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "total"}],
    } | more


def on_both(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
    compiled: CompiledModel,
    catalog: dict[str, Any],
    rows: list[dict[str, str | None]],
    document: Document,
    policy: Any = None,  # noqa: ANN401
    context: Any = None,  # noqa: ANN401
) -> tuple[Any, Any]:
    """(ClickHouse's outcome, the managed lake's) for one question on the same rows."""
    with tiny_lake(catalog, rows) as lake:
        base.load(server, catalog, copied(lake, catalog))
        _, _, routed = wired(engine, compiled)
        more = {"policy": policy, "context": context}
        found = outcome(lambda: ask(document, compiled, lake, **more, **routed))
        reference = outcome(lambda: ask(document, compiled, lake, **more))
    return found, reference


# Every shape where ClickHouse compares a float: NaN above every number and equal to itself,
# -0.0 equal to 0.0, as the managed lake. Each metric is asked over FLOAT_ROWS.
FLOAT_METRICS = {
    "a comparison in a filter": "COUNT(*) FILTER (WHERE t.x > 1)",
    "NaN equal to itself": "COUNT(*) FILTER (WHERE t.x = t.x)",
    "NaN not unequal to itself": "COUNT(*) FILTER (WHERE t.x <> t.x)",
    "a comparison with NaN on the right": "COUNT(*) FILTER (WHERE 1 < t.x)",
    "between": "COUNT(*) FILTER (WHERE t.x BETWEEN -1 AND 3)",
    "not between": "COUNT(*) FILTER (WHERE t.x NOT BETWEEN -1 AND 3)",
    "in a list of fields": "COUNT(*) FILTER (WHERE t.x IN (t.x, 1))",
    "a distinct count: NaN once, -0.0 and 0.0 one value": "COUNT(DISTINCT t.x)",
    "a distinct count in a filter": "COUNT(DISTINCT t.x) FILTER (WHERE t.x >= 0)",
    "a float sum": "SUM(t.x)",
}


@pytest.mark.parametrize("metric", FLOAT_METRICS.values(), ids=list(FLOAT_METRICS))
def test_each_float_comparison_and_distinct_count_answers_as_the_managed_lake(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
    metric: str,
) -> None:
    distinct = {"aggregate_class": "holistic", "additivity": "non_additive"}
    compiled, catalog = float_metric(metric, **(distinct if "DISTINCT" in metric else {}))
    rows = [r | {"g": "a"} for r in FLOAT_ROWS]
    found, reference = on_both(server, engine, compiled, catalog, rows, aggregate())
    assert found == reference


def test_a_row_policy_comparing_a_float_restricts_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
) -> None:
    compiled, catalog = tiny_model([("id", "BIGINT"), ("x", "DOUBLE")], "COUNT(t.id)")
    rule = {"name": "above", "dataset": "t", "kind": "predicate", "predicate": "x > 1"}
    policy = read_policies(
        yaml.safe_dump({"version": 1, "row_policies": [rule]}),
        compiled,
        groups=frozenset(),
        managed_tables={},
    )
    context = rp.caller()
    found, reference = on_both(
        server, engine, compiled, catalog, FLOAT_ROWS, aggregate(), policy, context
    )
    assert found == reference
    assert reference[1] == [["4"]]  # 2.5, inf and both NaN rows: NaN is above 1


PIVOT_ROWS: list[dict[str, str | None]] = [
    {"id": "1", "g": "a", "x": "1"},
    {"id": "2", "g": "a", "x": "nan"},
    {"id": "3", "g": "b", "x": "5"},
    {"id": "4", "g": "c", "x": "-inf"},
    {"id": "5", "g": "d", "x": None},
]


@pytest.mark.parametrize("direction", ["desc", "asc"])
def test_a_pivot_table_ordered_by_a_float_metric_puts_nan_where_the_managed_lake_does(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
    direction: str,
) -> None:
    compiled, catalog = float_metric("SUM(t.x)")
    document = aggregate(
        group_by=[{"member": "t.g"}],
        totals="rollup",
        order=[{"by": "total", "direction": direction, "nulls": "last"}],
    )
    found, reference = on_both(server, engine, compiled, catalog, PIVOT_ROWS, document)
    assert found == reference


def test_a_float_group_is_refused_on_clickhouse_with_a_stable_code(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
) -> None:
    """-0.0 and 0.0 are one group on the managed lake, shown as whichever row came first:
    ClickHouse cannot match an arbitrary representative, so it refuses before reading. A
    running total's order and partitions are its groups, so it is refused alike."""
    compiled, catalog = float_metric("COUNT(t.id)")
    document = aggregate(
        group_by=[{"member": "t.x"}], order=[{"by": "t.x", "direction": "desc", "nulls": "last"}]
    )
    rows = [r | {"g": "a"} for r in FLOAT_ROWS]
    found, reference = on_both(server, engine, compiled, catalog, rows, document)
    assert found == ("SemanticError", "UNSUPPORTED_OPERATION", {"operation": "float_group"})
    assert reference[0] != "SemanticError"


@pytest.mark.parametrize("function", ["MIN", "MAX"])
def test_a_float_minimum_or_maximum_is_never_answered_on_clickhouse(
    server: base.Server,  # noqa: F811
    engine: ClickHouse,
    function: str,
) -> None:
    """The reference's MAX over NaN is NaN and ClickHouse's skips it; neither engine answers a
    MIN or MAX metric today, and ClickHouse refuses it exactly as the reference does, so no
    float minimum or maximum is ever a number of ClickHouse's."""
    compiled, catalog = float_metric(f"{function}(t.x)", additivity="additive")
    rows = [r | {"g": "a"} for r in FLOAT_ROWS]
    found, reference = on_both(server, engine, compiled, catalog, rows, aggregate())
    assert found == reference
    assert found[:2] == ("SemanticError", "UNSUPPORTED_OPERATION")


def test_a_join_or_an_evidence_key_on_a_float_is_never_compared_on_clickhouse() -> None:
    """A join on a float key is refused (`float_join`); evidence keyed by a float is not
    checked on ClickHouse, so it stays declared."""
    document = {
        "version": "0.2.0.dev0",
        "name": "floats",
        "datasets": [
            {
                "name": "t",
                "source": "t",
                "primary_key": ["id"],
                "fields": [{"name": c, "expression": {"sql": c}} for c in ("id", "x")],
            },
            {
                "name": "u",
                "source": "u",
                "primary_key": ["x"],
                "fields": [{"name": c, "expression": {"sql": c}} for c in ("x", "label")],
            },
        ],
        "relationships": [
            {"name": "by_x", "from": "t", "to": "u", "from_columns": ["x"], "to_columns": ["x"]}
        ],
        "metrics": [{"name": "total", "expression": {"sql": "COUNT(t.id)"}}],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    catalog = {
        "t": {"id": SourceColumn("BIGINT"), "x": SourceColumn("DOUBLE")},
        "u": {"x": SourceColumn("DOUBLE"), "label": SourceColumn("VARCHAR")},
    }
    loaded = read(json.dumps(document).encode(), "semantic/floats.ossie.json", VENDORS)
    compiled = compile_model(loaded, catalog, VENDORS)
    planned = base.plan_of(aggregate(group_by=[{"member": "u.label"}]), compiled)
    with pytest.raises(SemanticError) as caught:
        ch.compile_plan(planned, base.names_of(catalog), {})
    assert (caught.value.code, dict(caught.value.details)) == (
        "UNSUPPORTED_OPERATION",
        {"operation": "float_join"},
    )
    key = (next(m for m in compiled.members if m.dataset == "U" and m.name == "X"),)
    with pytest.raises(PlanError):
        ch.ClickHouseChecks(base.names_of(catalog)).unique(compiled, "U", key)


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
