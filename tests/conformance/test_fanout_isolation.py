"""P2-16 (#331): two facts in one question, each aggregated apart and aligned on the requested
grain only (C3); money in two currencies never added (CASE-017)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from orvex.semantic.adapters.duckdb import compile_plan
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.conformance.test_execution import Spy, ask, corpus_case, lake_for
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
Rows = list[list[str | None]]

TABLES = {
    "orders": ["order_id", "customer_id", "order_total"],
    "order_items": ["item_id", "order_id", "product"],
    "customers": ["customer_id", "region"],
}
TYPES = {"order_total": "DECIMAL(18,2)", "product": "VARCHAR", "region": "VARCHAR"}
NULLABLE = {"region", "customer_id", "order_id"}


def model() -> tuple[CompiledModel, dict[str, dict[str, SourceColumn]]]:
    catalog = {
        t: {c: SourceColumn(TYPES.get(c, "BIGINT"), c in NULLABLE and c != cols[0]) for c in cols}
        for t, cols in TABLES.items()
    }
    document = {
        "version": "0.2.0.dev0",
        "name": "isolation",
        "datasets": [
            {
                "name": t,
                "source": t,
                "primary_key": [cols[0]],
                "fields": [{"name": c, "expression": {"sql": c}} for c in cols],
            }
            for t, cols in TABLES.items()
        ],
        "relationships": [
            {
                "name": "item_order",
                "from": "order_items",
                "to": "orders",
                "from_columns": ["order_id"],
                "to_columns": ["order_id"],
            },
            {
                "name": "order_customer",
                "from": "orders",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
            },
        ],
        "metrics": [
            {"name": "order_revenue", "expression": {"sql": "SUM(orders.order_total)"}},
            {"name": "item_count", "expression": {"sql": "COUNT(order_items.item_id)"}},
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/isolation.ossie.json", VENDORS)
    return compile_model(loaded, catalog, VENDORS), catalog


COMPILED, CATALOG = model()


def policies(**document: Any) -> PolicyModel:  # noqa: ANN401
    return read_policies(
        yaml.safe_dump({"version": 1, **document}), COMPILED, groups=frozenset(), managed_tables={}
    )


@contextmanager
def lake_of(data: dict[str, list[list[str]]]) -> Iterator[Lake]:
    with TemporaryDirectory() as folder:
        paths = []
        for table, rows in data.items():
            path = Path(folder) / f"{table}.csv"
            lines = [",".join(CATALOG[table])] + [",".join(r) for r in rows]
            path.write_text("\n".join(lines) + "\n", "utf-8")
            paths.append(path)
        with ce.case_lake(paths, CATALOG) as lake:
            yield lake


def query(
    *metrics: str,
    group_by: tuple[str, ...] = ("customers.region",),
    **more: Any,  # noqa: ANN401
) -> Document:
    found: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": m} for m in metrics],
    }
    if group_by:
        found["group_by"] = [{"member": g} for g in group_by]
    return found | more


def rows(document: Document, lake: Lake) -> Rows:
    return ce.rows_of(ask(document, COMPILED, lake))


def refused(document: Document, policy: PolicyModel | None = None) -> tuple[str, dict[str, Any]]:
    spy = Spy()
    with lake_of(DATA) as lake, pytest.raises(SemanticError) as caught:
        ask(document, COMPILED, lake, policy=policy, read=spy)
    assert spy.calls == 0  # refused before any read
    return caught.value.code, dict(caught.value.details)


# Every order has two or three items, so joining orders through items would multiply revenue.
DATA = {
    "customers": [["1", "North"], ["2", "South"], ["3", ""]],
    "orders": [["10", "1", "100.00"], ["11", "2", "80.00"], ["12", "3", "7.00"]],
    "order_items": [
        ["1", "10", "pen"],
        ["2", "10", "ink"],
        ["3", "10", "pad"],
        ["4", "11", "pen"],
        ["5", "11", "pad"],
        ["6", "12", "pen"],
        ["7", "12", "ink"],
    ],
}
REGION = ("customers.region",)
NORTH = {
    "scope": "board",
    "member": "customers.region",
    "predicate": {"op": "in", "values": [{"type": "text", "value": "North"}]},
}


# --- CASE-004 ---------------------------------------------------------------------------------


def test_case_004_aligns_two_facts_exactly_with_their_isolation_obligations() -> None:
    compiled, _, _ = corpus_case("CASE-004")
    with lake_for("CASE-004") as lake:
        found = ask(ce.QUERIES["CASE-004"], compiled, lake)
    assert ce.rows_of(found) == [["North", "150.00", "3"], ["South", "80.00", "3"]]
    isolated = sorted(o.id for o in found.report.obligations if o.rule == "C3")
    assert isolated == ["C3.isolate:item_count", "C3.isolate:order_revenue"]
    aggregates = {a.name: a.discharges for a in found.plan.aggregates}
    assert "C3.isolate:order_revenue" in aggregates["order_revenue"]
    assert "C3.isolate:item_count" in aggregates["item_count"]


# --- no fan-out -------------------------------------------------------------------------------


def test_each_metric_equals_its_value_asked_alone() -> None:
    with lake_of(DATA) as lake:
        both = rows(query("order_revenue", "item_count"), lake)
        revenue = rows(query("order_revenue"), lake)
        items = rows(query("item_count"), lake)
    assert both == [["North", "100.00", "3"], ["South", "80.00", "2"], [None, "7.00", "2"]]
    assert [[r[0], r[1]] for r in both] == revenue
    assert [[r[0], r[2]] for r in both] == items


def test_the_question_s_order_of_metrics_is_kept() -> None:
    with lake_of(DATA) as lake:
        found = ask(query("item_count", "order_revenue"), COMPILED, lake)
    names = [c["name"] for c in found.result.envelope()["columns"]]
    assert names == ["region", "item_count", "order_revenue"]
    assert ce.rows_of(found)[0] == ["North", "3", "100.00"]


def test_with_no_grain_each_fact_gives_its_one_total() -> None:
    with lake_of(DATA) as lake:
        assert rows(query("order_revenue", "item_count", group_by=()), lake) == [["187.00", "7"]]


# --- alignment --------------------------------------------------------------------------------


def test_a_group_one_fact_lacks_is_null_for_its_metrics_counts_included() -> None:
    data = {
        "customers": [["1", "North"], ["2", "East"]],
        "orders": [["10", "1", "100.00"], ["11", "2", "5.00"]],
        # item 9 belongs to no known order: its region is NULL, a group only items have.
        "order_items": [["1", "10", "pen"], ["9", "99", "pen"]],
    }
    with lake_of(data) as lake:
        found = rows(query("order_revenue", "item_count"), lake)
    assert found == [["East", "5.00", None], ["North", "100.00", "1"], [None, None, "1"]]


def test_a_null_group_appears_once() -> None:
    with lake_of(DATA) as lake:
        found = rows(query("order_revenue", "item_count"), lake)
    assert [r[0] for r in found].count(None) == 1
    assert len({r[0] for r in found}) == len(found)  # no grain value repeats


def test_the_alignment_joins_on_exactly_the_requested_grain() -> None:
    with lake_of(DATA) as lake:
        found = ask(query("order_revenue", "item_count"), COMPILED, lake)
    plan = found.plan
    assert len(plan.facts) == 2
    by_fact = [
        [(g.name, g.member, g.bucket) for g in plan.groups if g.source == f] for f in plan.facts
    ]
    assert by_fact[0] == by_fact[1]  # the same member, role and bucket in each fact
    text = compile_plan(plan).sql
    assert text.count("FULL OUTER JOIN") == 1
    on = text.rsplit(" ON ", 1)[1]
    keys = 'COALESCE("f0"."region") IS NOT DISTINCT FROM "f1"."region"'
    assert on == f'{keys} ORDER BY "region" ASC NULLS LAST'


# --- filters ----------------------------------------------------------------------------------


def test_a_filter_both_facts_reach_applies_inside_each_aggregation() -> None:
    with lake_of(DATA) as lake:
        both = rows(query("order_revenue", "item_count", filters=[NORTH]), lake)
        revenue = rows(query("order_revenue", filters=[NORTH]), lake)
        items = rows(query("item_count", filters=[NORTH]), lake)
    assert both == [["North", "100.00", "3"]]
    assert [[r[0], r[1]] for r in both] == revenue
    assert [[r[0], r[2]] for r in both] == items
    with lake_of(DATA) as lake:
        found = ask(query("order_revenue", "item_count", filters=[NORTH]), COMPILED, lake)
    assert len(found.plan.filters) == 2  # one in each fact
    assert {f.source for f in found.plan.filters} <= {s.alias for s in found.plan.sources}


def test_a_filter_one_fact_cannot_reach_refuses_before_any_read() -> None:
    pen = {
        "scope": "board",
        "member": "order_items.product",
        "predicate": {"op": "in", "values": [{"type": "text", "value": "pen"}]},
    }
    found = refused(query("order_revenue", "item_count", filters=[pen]))
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "fanout"})


# --- policies and what stays refused ----------------------------------------------------------


@pytest.mark.parametrize("dataset", ["order_items", "orders", "customers"])
def test_a_row_policy_any_fact_reaches_refuses_before_any_read(dataset: str) -> None:
    column = TABLES[dataset][0]
    policy = policies(
        row_policies=[
            {"name": "own", "dataset": dataset, "kind": "predicate", "predicate": f"{column} > 0"}
        ]
    )
    found = refused(query("order_revenue", "item_count"), policy)
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "row_policy"})


def test_grouping_by_a_one_to_many_member_is_still_fanout() -> None:
    found = refused(query("order_revenue", "item_count", group_by=("order_items.product",)))
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "fanout"})


# --- CASE-017 ---------------------------------------------------------------------------------


def test_case_017_adding_usd_to_bob_is_refused_naming_both() -> None:
    with pytest.raises(SemanticError) as caught:
        corpus_case("CASE-017")
    assert caught.value.code == "INCOMPATIBLE_UNITS"
    assert list(cast(list[str], caught.value.details["units"])) == ["USD", "BOB"]


# --- property ---------------------------------------------------------------------------------

REGIONS = ["North", "South", None]


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.sampled_from(REGIONS),
            st.integers(min_value=-99999, max_value=99999),
            st.integers(min_value=0, max_value=3),
        ),
        min_size=1,
        max_size=8,
    )
)
def test_each_metric_matches_a_per_fact_reference(
    orders: list[tuple[str | None, int, int]],
) -> None:
    customers = [[str(i), region or ""] for i, region in enumerate(REGIONS)]
    data: dict[str, list[list[str]]] = {"customers": customers, "orders": [], "order_items": []}
    revenue: dict[str | None, Decimal] = {}
    items: dict[str | None, int] = {}
    for n, (region, cents, count) in enumerate(orders):
        total = Decimal(cents) / 100
        data["orders"].append([str(n), str(REGIONS.index(region)), f"{total:.2f}"])
        revenue[region] = revenue.get(region, Decimal(0)) + total
        for _ in range(count):
            data["order_items"].append([str(len(data["order_items"])), str(n), "pen"])
            items[region] = items.get(region, 0) + 1
    with lake_of(data) as lake:
        found = rows(query("order_revenue", "item_count"), lake)
    expected = {
        region: [region, f"{revenue[region]:.2f}", str(items[region]) if region in items else None]
        for region in revenue
    }
    order = sorted(expected, key=lambda r: (r is None, r or ""))
    assert found == [expected[r] for r in order]
