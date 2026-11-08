"""The ClickHouse dialect adapter, its remaining capabilities (P2-31 PR 3, #225; ADR-0027 §8):
`grouping_sets`, `window`, `time_zone`, `detail_pages`, `multi_fact`, `bridge` and `as_of`.

Each capability is asked with the fixtures its own managed-lake suite uses, on ClickHouse and on
the managed lake, and the two answers are compared: the envelope's columns, row roles, cell
states and page, every row, or the same refusal. The rows ClickHouse reads are copied from the
reference lake, so both engines read the same values. The server is the one
`test_clickhouse_adapter` uses (`SEMANTIC_TEST_CLICKHOUSE`): required in CI, skipped elsewhere.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any

import pyarrow as pa
import pytest
from orvex.contracts.store.v1 import Parameter, ReadRequest
from orvex.semantic import zonedata
from orvex.semantic.adapters import clickhouse as ch
from orvex.semantic.adapters.dialect import quote, table
from orvex.semantic.adapters.duckdb import compile_plan as duckdb_compile
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.correctness import analyze
from orvex.semantic.errors import PlanError, SemanticError
from orvex.semantic.planning import plan
from orvex.semantic.query.binding import Limits as QueryLimits
from orvex.semantic.query.binding import bind_query
from orvex.store.errors import StoreError
from orvex.store.json_encoding import arrow_to_json
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.conformance import test_asof_windows as asof
from tests.conformance import test_bridges as bridges
from tests.conformance import test_clickhouse_adapter as base
from tests.conformance import test_conformed as conformed
from tests.conformance import test_detail_rows as details
from tests.conformance import test_row_policies as rp
from tests.conformance import test_totals as totals
from tests.conformance import test_zones as zones
from tests.conformance.test_clickhouse_adapter import server  # noqa: F401 - the fixture
from tests.conformance.test_execution import ask, no_policies, tiny_lake

Document = dict[str, Any]
Catalog = Mapping[str, Mapping[str, SourceColumn]]
Builder = Callable[[], AbstractContextManager[Lake]]


def copied(lake: Lake, catalog: Catalog) -> dict[str, list[list[str | None]]]:
    """Every table's rows in the lake, as text (an instant in UTC with its microseconds)."""
    found: dict[str, list[list[str | None]]] = {}
    for name, columns in catalog.items():
        listed = ", ".join(
            f"strftime(timezone('UTC', {quote(c)}), '%Y-%m-%d %H:%M:%S.%f')"
            if column.type.upper().startswith("TIMESTAMP")
            else f"CAST({quote(c)} AS VARCHAR)"
            for c, column in columns.items()
        )
        request = ReadRequest(
            kind="read",
            sql=f"SELECT {listed} FROM {table(name)}",
            parameters=[],
            snapshot=lake.current_snapshot().id,
            batch_size=10_000,
        )
        rows = lake.read(request).read_all().to_pylist()
        found[name] = [list(row.values()) for row in rows]
    return found


def outcome(run: Callable[[], Any]) -> Any:  # noqa: ANN401
    """What a question gives: its envelope's shape and rows, or its refusal."""
    try:
        found = run()
    except (SemanticError, StoreError, PlanError) as error:
        return (
            type(error).__name__,
            getattr(error, "code", None),
            dict(getattr(error, "details", {})),
        )
    envelope = found.result.envelope()
    page = dict(envelope.get("page") or {})
    keys = ("columns", "row_roles", "cell_states")
    return {k: envelope.get(k) for k in keys} | {"page": page, "rows": ce.rows_of(found)}


def parity(
    server: base.Server,  # noqa: F811
    compiled: CompiledModel,
    catalog: Catalog,
    lake: Lake,
    document: Document,
    **more: Any,  # noqa: ANN401
) -> tuple[Any, Any, base.OnClickHouse]:
    """(ClickHouse's outcome, the managed lake's, the adapter) for one question."""
    base.load(server, catalog, copied(lake, catalog))
    adapter = base.OnClickHouse(server, base.names_of(catalog))
    routed: dict[str, Any] = {
        "routing": base.everywhere(compiled),
        "adapters": {adapter.connection: adapter},
    }
    reference = outcome(lambda: ask(document, compiled, lake, **more))
    found = outcome(lambda: ask(document, compiled, lake, **more, **routed))
    return found, reference, adapter


def same(server: base.Server, compiled: Any, catalog: Any, lake: Builder, document: Any) -> Any:  # noqa: ANN401, F811
    with lake() as opened:
        found, reference, adapter = parity(server, compiled, catalog, opened, document)
    assert found == reference
    assert adapter.statements  # read on ClickHouse
    return found


# --- grouping_sets: totals and subtotals (P2-23) ----------------------------------------------


def sales() -> AbstractContextManager[Lake]:
    return tiny_lake(totals.CATALOG, totals.table(totals.SALES))


TOTALS = {
    "rollup, largest first": totals.query(order=[{"by": "revenue", "direction": "desc"}]),
    "rollup, smallest first": totals.query(order=[{"by": "revenue", "direction": "asc"}]),
    "grand": totals.query("grand"),
    "rollup cut by a limit": totals.query(limit="4"),
    "by a member's own order": totals.query(order=[{"by": "t.region", "direction": "desc"}]),
}


@pytest.mark.parametrize("document", TOTALS.values(), ids=list(TOTALS))
def test_every_total_level_answers_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
    document: Document,
) -> None:
    found = same(server, totals.COMPILED, totals.CATALOG, sales, document)
    assert found["row_roles"]


def test_totals_with_the_other_capabilities_answer_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
) -> None:
    every = conformed.query("gross_revenue", "refunds", "net_revenue", "attainment")
    asked = [
        (
            conformed.COMPILED,
            conformed.CATALOG,
            lambda: conformed.lake_of(conformed.DATA),
            every | {"totals": "rollup"},
        ),
        (
            bridges.COMPILED,
            bridges.CATALOG,
            lambda: bridges.lake_of(bridges.DATA),
            bridges.query(totals="grand"),
        ),
        (
            asof.DAILY,
            asof.DAILY_CATALOG,
            lambda: asof.lake_of(asof.DAYS, asof.DAILY_CATALOG),
            asof.query(
                "cumulative_by_region", group_by=("daily.day", "daily.region"), totals="rollup"
            ),
        ),
    ]
    for compiled, catalog, lake, document in asked:
        same(server, compiled, catalog, lake, document)


def test_a_pivot_table_is_ordered_in_the_statement_that_converts_its_rows() -> None:
    plan_ = base.plan_of(totals.query(), totals.COMPILED)
    statement = ch.compile_plan(plan_, base.names_of(totals.CATALOG), {}).sql
    # An order inside a subquery is not kept by the statement around it.
    assert re.search(r' AS "#r" ORDER BY "#r"\."#o0" ASC NULLS LAST, "#r"\."#o1" ', statement)
    assert statement.count("ORDER BY") == 1


# --- window: running totals (S9) --------------------------------------------------------------

NULL_FIRST = {"daily": [["2026-01-01", "North", ""], *asof.DAYS["daily"][1:]]}
WINDOWS = {
    "over every row": (asof.DAYS, asof.query("cumulative", group_by=("daily.day",))),
    "restarted by a partition": (
        asof.DAYS,
        asof.query("cumulative_by_region", group_by=("daily.day", "daily.region")),
    ),
    "a filter shows rows without restarting": (
        asof.DAYS,
        asof.query(
            "daily_revenue",
            "cumulative",
            group_by=("daily.day",),
            filters=[asof.date_filter("daily.day", "2026-01-03", "2026-01-04")],
        ),
    ),
    "NULL while every value so far is NULL": (
        NULL_FIRST,
        asof.query("cumulative_by_region", group_by=("daily.day", "daily.region")),
    ),
}


@pytest.mark.parametrize(("data", "document"), WINDOWS.values(), ids=list(WINDOWS))
def test_a_running_total_answers_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
    data: Any,  # noqa: ANN401
    document: Document,
) -> None:
    same(
        server,
        asof.DAILY,
        asof.DAILY_CATALOG,
        lambda: asof.lake_of(data, asof.DAILY_CATALOG),
        document,
    )


def test_a_running_total_is_summed_in_256_bits_and_null_over_null_values() -> None:
    plan_ = base.plan_of(asof.query("cumulative", group_by=("daily.day",)), asof.DAILY)
    statement = ch.compile_plan(plan_, base.names_of(asof.DAILY_CATALOG), {}).sql
    window = 'ORDER BY "day" ASC NULLS LAST ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW'
    assert (
        f'if(count("#daily_revenue") OVER ({window}) = 0, NULL, '
        f'sum(toDecimal256("#daily_revenue", 2)) OVER ({window}))'
    ) in statement
    assert "SUM(" not in statement


# --- time_zone: instants cut in their zone (C6) -----------------------------------------------

INSTANTS = [
    "2026-01-01T02:00:00Z",  # 22:00 on 31 December in La Paz
    "2026-03-08T06:59:59.999999Z",  # New York springs forward a microsecond later
    "2026-03-08T07:00:00Z",
    "2026-11-01T05:30:00Z",  # 01:30 in New York, twice
    "2026-11-01T06:30:00Z",
    "2026-01-01T00:10:00Z",
    "1965-06-01T03:00:00.25Z",  # before 1970, and after 2106: past a 32-bit DateTime
    "2150-01-01T00:30:00Z",
]
ZONED = [
    (zone, unit)
    for zone in ("America/La_Paz", "America/New_York", "Asia/Kolkata", "Asia/Kathmandu")
    for unit in ("day", "week", "month", "quarter", "year", "hour", "minute")
]


@pytest.mark.parametrize(("zone", "unit"), ZONED, ids=[f"{z}-{u}" for z, u in ZONED])
def test_an_instant_is_cut_in_its_zone_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
    zone: str,
    unit: str,
) -> None:
    found = same(
        server,
        zones.model(default=zone),
        zones.CATALOG,
        lambda: zones.lake_of(INSTANTS),
        zones.by(unit),
    )
    assert len(found["rows"]) > 1


def test_a_business_time_field_s_own_zone_answers_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
) -> None:
    document = zones.by(None, member="events.ny_day")
    same(server, zones.COMPILED, zones.CATALOG, lambda: zones.lake_of(INSTANTS), document)


def test_an_instant_bucket_never_uses_a_32_bit_datetime() -> None:
    for unit in ("day", "hour", "minute"):
        plan_ = base.plan_of(zones.by(unit), zones.COMPILED)
        query = ch.compile_plan(plan_, base.names_of(zones.CATALOG), {})
        assert "toStartOf" not in query.sql
        assert "toTimeZone(toDateTime64(" in query.sql
        assert query.zones == ("America/La_Paz",)
        assert [p["value"] for p in query.parameters] == ["America/La_Paz"]  # bound, never text


def points(zone: str) -> tuple[dict[str, str], ...]:
    return tuple({k: str(v) for k, v in p.items()} for p in zonedata.parameters(zone))


@pytest.mark.parametrize("zone", ["America/New_York", "Asia/Kathmandu", "Europe/Dublin"])
def test_the_zone_check_counts_each_point_clickhouse_converts_differently(
    server: base.Server,  # noqa: F811
    zone: str,
) -> None:
    ch.check_statement(ch.ZONE_CHECK_SQL, ())
    assert zonedata.check_sql("clickhouse") == ch.ZONE_CHECK_SQL
    assert zonedata.check_sql("duckdb") == zonedata.CHECK_SQL

    def count(parameters: tuple[dict[str, str], ...]) -> int:
        body = server.run(
            ch.ZONE_CHECK_SQL + "\nFORMAT TSV",
            base.bound(parameters),
            as_reader=server.reader,  # type: ignore[attr-defined]
        )
        return int(body.decode().strip())

    agreed = points(zone)
    assert count(agreed) == 0  # ClickHouse's zone data agrees with the pinned `tzdata`
    local = agreed[1]["value"].split(",")
    shifted = [str(int(v) + 3600) if n % 100 == 0 else v for n, v in enumerate(local)]
    changed = (agreed[0], agreed[1] | {"value": ",".join(shifted)}, agreed[2])
    assert count(changed) == len(local[::100])


def test_a_clickhouse_that_disagrees_on_a_zone_refuses_before_reading(
    server: base.Server,  # noqa: F811
) -> None:
    with zones.lake_of(INSTANTS) as lake:
        base.load(server, zones.CATALOG, copied(lake, zones.CATALOG))
        adapter = Disagreeing(server, base.names_of(zones.CATALOG))
        routed: dict[str, Any] = {
            "routing": base.everywhere(zones.COMPILED),
            "adapters": {"warehouse": adapter},
        }
        with pytest.raises(SemanticError) as caught:
            ask(zones.by("day"), zones.COMPILED, lake, **routed)
    assert (caught.value.code, dict(caught.value.details)) == (
        "UNSUPPORTED_OPERATION",
        {"operation": "time_zone_data"},
    )
    assert [s for s, _ in adapter.statements] == [ch.ZONE_CHECK_SQL]  # no data read


class Disagreeing(base.OnClickHouse):
    """A ClickHouse whose zone data differs from the pinned `tzdata` at one point."""

    def read(self, statement: str, parameters: Any, *more: Any) -> pa.RecordBatchReader:  # noqa: ANN401
        if statement == ch.ZONE_CHECK_SQL:
            local = parameters[1]["value"].split(",")
            local[0] = str(int(local[0]) + 1)
            parameters = (parameters[0], parameters[1] | {"value": ",".join(local)}, parameters[2])
        return super().read(statement, parameters, *more)


# --- detail_pages: keyset pages at the base grain (P2-24) -------------------------------------
# Execution refuses a detail page on another engine until a cursor can name its read instant
# (#225 PR 4), so each page's statement is compared here: the same plan, from the same position,
# read on each engine.


def detail_plan(document: Document, start: Any, policy: Any) -> Any:  # noqa: ANN401
    bound = bind_query(
        rp.COMPILED,
        json.dumps(document),
        instant=ce.INSTANT,
        limits=QueryLimits(detail_page_size=1000),
        hidden=lambda _: False,
    )
    policies = policy or no_policies(rp.COMPILED)
    return plan(analyze(bound, rp.COMPILED, policies, ce.viewer_context()), start)


def pages(server: base.Server, document: Document, policy: Any = None) -> list[Any]:  # noqa: ANN401, F811
    """Every page of one question, as (ClickHouse's rows, the managed lake's), each page from
    the last row of the one before."""
    found: list[Any] = []
    with rp.lake_of() as lake:
        base.load(server, rp.CATALOG, copied(lake, rp.CATALOG))
        adapter = base.OnClickHouse(server, base.names_of(rp.CATALOG))
        start = None
        while True:
            planned = detail_plan(document, start, policy)
            duck = duckdb_compile(planned)
            query = adapter.compile(planned, {})
            assert query.sorts == duck.sorts
            reference = lake.read(
                ReadRequest(
                    kind="read",
                    sql=duck.sql,
                    parameters=[Parameter.model_validate(p) for p in duck.parameters],
                    snapshot=lake.current_snapshot().id,
                    batch_size=1000,
                )
            ).read_all()
            read = adapter.read(query.sql, query.parameters, {}, 1000, None).read_all()
            assert read.schema == reference.schema
            found.append(read.to_pylist())
            assert found[-1] == reference.to_pylist()
            size = int(document["page"]["size"])
            if reference.num_rows <= size:
                return found
            last = reference.select([n for n, _ in duck.sorts]).slice(size - 1, 1)
            text = "".join(
                arrow_to_json(pa.RecordBatchReader.from_batches(last.schema, last.to_batches()))
            )
            start = tuple(json.loads(text)["rows"][0])


DETAILS = {
    "by amount, largest first": details.query(),
    "by a field with NULL, NULL first": details.query(
        1, [{"by": "customers.country", "direction": "desc", "nulls": "first"}]
    ),
    "by a joined field, NULL last": details.query(
        3, [{"by": "customers.country", "direction": "asc"}]
    ),
    "by a field not shown": details.query(2, fields=("sales.sale_id", "sales.region")),
}


@pytest.mark.parametrize("document", DETAILS.values(), ids=list(DETAILS))
def test_every_detail_page_reads_on_clickhouse_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
    document: Document,
) -> None:
    assert len(pages(server, document)) > 1  # a page from a position was read on ClickHouse


def test_a_masked_field_is_its_mask_on_clickhouse_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
) -> None:
    found = pages(server, details.query(4), details.policies(column_policies=[details.MASKED]))
    assert {row["region"] for row in found[0]} == {"###"}


def test_a_detail_page_on_another_engine_stays_refused_until_its_cursor_names_a_read_instant(
    server: base.Server,  # noqa: F811
) -> None:
    with rp.lake_of() as lake:
        more = details.Pager(lake).engine()
        found, _, adapter = parity(server, rp.COMPILED, rp.CATALOG, lake, details.query(), **more)
    assert found == ("SemanticError", "UNSUPPORTED_OPERATION", {"operation": "detail_pages"})
    assert adapter.statements == []


def test_a_page_position_is_read_back_exactly_into_its_column_type() -> None:
    writer = ch.writer._ClickHouse(
        base.plan_of(base.question("rows")), base.names_of(base.DIALECT), {}
    )
    assert writer.typed(None, "DECIMAL(18,2)") == "CAST(NULL AS Nullable(Decimal(18, 2)))"
    huge = writer.typed("170141183460469231731687303715884105727", "HUGEINT")
    assert huge == ch.exact.checked("toDecimal256({p0:String}, 0)", "HUGEINT")  # never wrapped
    instant = writer.typed("2026-01-01T00:00:00.000001Z", "TIMESTAMP WITH TIME ZONE")
    assert instant == "parseDateTime64BestEffort({p1:String}, 6, 'UTC')"
    assert writer.typed(0.1, "DOUBLE") == "CAST({p2:String} AS Float64)"
    assert [p["value"] for p in writer.parameters][2] == "0.1"


# --- multi_fact: facts aligned on conformed dimensions (C5) -----------------------------------

FACTS = {
    "a metric over metrics after alignment": conformed.query(
        "gross_revenue", "refunds", "net_revenue"
    ),
    "a ratio over metrics after alignment": conformed.query("gross_revenue", "attainment"),
    "a group in one fact only": conformed.query("refunds", "net_revenue"),
    "no group at all": conformed.query("gross_revenue", "refunds", group_by=()),
}


@pytest.mark.parametrize("document", FACTS.values(), ids=list(FACTS))
def test_aligned_facts_answer_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
    document: Document,
) -> None:
    lake = lambda: conformed.lake_of(conformed.DATA)  # noqa: E731
    same(server, conformed.COMPILED, conformed.CATALOG, lake, document)


def test_a_metric_over_metrics_is_exact_and_checked_into_its_type() -> None:
    plan_ = base.plan_of(conformed.query("net_revenue"), conformed.COMPILED)
    statement = ch.compile_plan(plan_, base.names_of(conformed.CATALOG), {}).sql
    assert '(toDecimal256("#gross_revenue", 2) - toDecimal256("#refunds", 2))' in statement
    assert "IS NOT DISTINCT FROM" in statement  # `NULL` matches `NULL` across facts


def test_is_not_distinct_from_is_no_table_read_but_a_from_still_is() -> None:
    ch.check_statement('SELECT 1 FROM "m" AS "a" WHERE "a"."x" IS NOT DISTINCT FROM "b"', {"m"})
    with pytest.raises(PlanError):
        ch.check_statement(
            'SELECT 1 FROM "m" AS "a" WHERE 1 IS NOT DISTINCT FROM 1 FROM "x"', {"m"}
        )


# --- bridge: many-to-many allocation (C7) -----------------------------------------------------

SHORT = {"order_campaign": [["1", "A", "0.60"], ["1", "B", "0.37"], ["2", "A", "1.00"]]}
BRIDGES = {
    "allocated exactly": (bridges.DATA, bridges.query()),
    "rounded once": (
        bridges.DATA
        | {
            "orders": [["1", "North", "0.05"], ["2", "North", "0.05"], ["3", "South", "0.05"]],
            "order_campaign": [[str(o), c, "0.50"] for o in (1, 2, 3) for c in ("A", "B")],
        },
        bridges.query(),
    ),
    "the largest value times a weight": (
        bridges.DATA
        | {
            "orders": [["1", "North", "9999999999999999.99"]],
            "order_campaign": [["1", "A", "1.00"]],
        },
        bridges.query(),
    ),
    "weights short of the total": (bridges.DATA | SHORT, bridges.query()),
    "a NULL weight": (
        bridges.DATA | {"order_campaign": [["1", "A", "1.00"], ["1", "B", ""], ["2", "A", "1.00"]]},
        bridges.query(),
    ),
    "an order with no bridge rows": (
        bridges.DATA
        | {"orders": [*bridges.DATA["orders"], ["3", "East", "9.00"], ["4", "East", "1.00"]]},
        bridges.query(),
    ),
}


@pytest.mark.parametrize(("data", "document"), BRIDGES.values(), ids=list(BRIDGES))
def test_an_allocation_answers_or_refuses_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
    data: Any,  # noqa: ANN401
    document: Document,
) -> None:
    same(server, bridges.COMPILED, bridges.CATALOG, lambda: bridges.lake_of(data), document)


def test_a_weighted_sum_is_exact_in_76_digits_and_checked_into_38() -> None:
    statement = ch.compile_plan(
        base.plan_of(bridges.query(), bridges.COMPILED), base.names_of(bridges.CATALOG), {}
    ).sql
    product = '(toDecimal256("t0"."order_total", 2) * toDecimal256("t1"."weight", 2))'
    assert ch.exact.checked(product, "DECIMAL(38,4)") in statement
    assert "SUM(" not in statement


def test_a_weighted_product_that_could_pass_76_digits_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planned = base.plan_of(bridges.query(), bridges.COMPILED)
    ch.compile_plan(planned, base.names_of(bridges.CATALOG), {})
    # DECIMAL(18,2) by DECIMAL(5,2): 57 and 14 bits. Below that, the product could wrap.
    monkeypatch.setattr(ch.writer, "WIDE_BITS", 70)
    with pytest.raises(SemanticError) as caught:
        ch.compile_plan(planned, base.names_of(bridges.CATALOG), {})
    assert caught.value.code == "UNSUPPORTED_OPERATION"


# --- as_of: the row valid at each event (C6) --------------------------------------------------

OVERLAP = {
    "customer_history": [
        ["1", "North", "2026-01-01", "2026-02-15"],
        ["1", "South", "2026-02-01", "9999-12-31"],
    ],
    "sales": [["1", "1", "2026-02-10", "7.00"], ["2", "1", "2026-03-01", "1.00"]],
}
GAP = {
    "customer_history": [
        ["1", "North", "2026-01-01", "2026-02-01"],
        ["1", "South", "2026-03-01", "2027-01-01"],
    ],
    "sales": [["1", "1", "2026-01-10", "1.00"], ["2", "1", "2027-06-01", "2.00"]],
}
AS_OF = {
    "the row valid at each sale": asof.CASE_013,
    "the boundary instant belongs to the later row": asof.CASE_013
    | {"sales": [["1", "1", "2026-02-01", "5.00"]]},
    "overlapping validity": OVERLAP,
    "no valid history": GAP,
}


@pytest.mark.parametrize("data", AS_OF.values(), ids=list(AS_OF))
def test_an_as_of_join_answers_or_refuses_as_on_the_managed_lake(
    server: base.Server,  # noqa: F811
    data: Any,  # noqa: ANN401
) -> None:
    document = asof.query("revenue", group_by=asof.BY_REGION)
    lake = lambda: asof.lake_of(data, asof.HISTORY_CATALOG)  # noqa: E731
    same(server, asof.HISTORY, asof.HISTORY_CATALOG, lake, document)


def test_an_unmatched_row_is_counted_with_a_join_never_a_correlated_count() -> None:
    planned = base.plan_of(asof.query("revenue", group_by=asof.BY_REGION), asof.HISTORY)
    statement = ch.compile_plan(planned, base.names_of(asof.HISTORY_CATALOG), {}).sql
    assert 'countIf("#h"."valid_from" <= "#u"."#r2" AND "#u"."#r2" < "#h"."valid_to")' in statement
    assert '"#u"."#r2" < "#h"."valid_to") <> 1' not in statement
