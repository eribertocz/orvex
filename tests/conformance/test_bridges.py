"""P2-18 (#334): a metric allocated through a bridge with validated weights (C7)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pyarrow as pa
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
    "orders": ["order_id", "region", "order_total"],
    "order_campaign": ["order_id", "campaign_id", "weight"],
    "campaign": ["campaign_id", "name", "channel_id"],
    "campaign_channel": ["campaign_id", "channel_id", "share"],
    "channel": ["channel_id", "label"],
}
TYPES = {
    "order_total": "DECIMAL(18,2)",
    "weight": "DECIMAL(5,2)",
    "share": "DECIMAL(5,2)",
    "region": "VARCHAR",
    "name": "VARCHAR",
    "label": "VARCHAR",
    "campaign_id": "VARCHAR",
}


def bridge(left: str, right: str, weight: str, entity: str, tolerance: str) -> Document:
    return {
        "left": left,
        "right": right,
        "weight": weight,
        "total": "1.00",
        "tolerance": tolerance,
        "normalization_scope": {"entities": [entity]},
    }


def model(
    *, tolerance: str = "0.00", rounding: Document | None = None, metric: str = "SUM"
) -> tuple[CompiledModel, dict[str, dict[str, SourceColumn]]]:
    rounding = rounding if rounding is not None else {"scale": 2, "mode": "half_away_from_zero"}
    catalog = {
        t: {c: SourceColumn(TYPES.get(c, "BIGINT"), c not in ("order_id",)) for c in cols}
        for t, cols in TABLES.items()
    }
    payloads: dict[str, Document] = {
        "orders": {"entity": "order"},
        "order_campaign": {
            "entity": "order_campaign",
            "bridge": bridge("order", "campaign", "weight", "order", tolerance),
        },
        "campaign": {"entity": "campaign"},
        "campaign_channel": {
            "entity": "campaign_channel",
            "bridge": bridge("of_campaign", "channel", "share", "campaign", "0.00"),
        },
        "channel": {"entity": "channel"},
    }
    keys = {
        "order_campaign": ["order_id", "campaign_id"],
        "campaign_channel": ["campaign_id", "channel_id"],
    }
    allocated = {"element": "metric", "allocation": {"bridge": "order_campaign"}}
    if rounding:
        allocated["rounding"] = rounding
    document = {
        "version": "0.2.0.dev0",
        "name": "bridges",
        "datasets": [
            {
                "name": t,
                "source": t,
                "primary_key": keys.get(t, [cols[0]]),
                "fields": [{"name": c, "expression": {"sql": c}} for c in cols],
                "custom_extensions": [extension({"element": "dataset", **payloads[t]})],
            }
            for t, cols in TABLES.items()
        ],
        "relationships": [
            {"name": n, "from": f, "to": t, "from_columns": [fc], "to_columns": [tc]}
            for n, f, t, fc, tc in (
                ("order", "order_campaign", "orders", "order_id", "order_id"),
                ("campaign", "order_campaign", "campaign", "campaign_id", "campaign_id"),
                ("of_campaign", "campaign_channel", "campaign", "campaign_id", "campaign_id"),
                ("channel", "campaign_channel", "channel", "channel_id", "channel_id"),
            )
        ],
        "metrics": [
            {
                "name": "allocated_revenue",
                "expression": {"sql": f"{metric}(orders.order_total)"},
                "custom_extensions": [
                    extension(
                        allocated | ({"additivity": "non_additive"} if metric == "MAX" else {})
                    )
                ],
            },
            {"name": "revenue", "expression": {"sql": "SUM(orders.order_total)"}},
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/bridges.ossie.json", VENDORS)
    return compile_model(loaded, catalog, VENDORS), catalog


COMPILED, CATALOG = model()

DATA = {
    "orders": [["1", "North", "100.00"], ["2", "South", "50.00"]],
    "order_campaign": [["1", "A", "0.60"], ["1", "B", "0.40"], ["2", "A", "1.00"]],
    "campaign": [["A", "Campaign A", "1"], ["B", "Campaign B", "1"]],
    "campaign_channel": [["A", "1", "1.00"], ["B", "1", "1.00"]],
    "channel": [["1", "web"]],
}


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


def query(*metrics: str, group_by: tuple[str, ...] = ("campaign.name",), **more: Any) -> Document:  # noqa: ANN401
    found: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": m} for m in metrics or ("allocated_revenue",)],
    }
    if group_by:
        found["group_by"] = [{"member": g} for g in group_by]
    return found | more


def rows(
    data: dict[str, list[list[str]]],
    document: Document | None = None,
    compiled: Any = None,  # noqa: ANN401
) -> Rows:
    with lake_of(data) as lake:
        return ce.rows_of(ask(document or query(), compiled or COMPILED, lake))


def invalid(
    data: dict[str, list[list[str]]], compiled: CompiledModel | None = None
) -> dict[str, Any]:
    with lake_of(data) as lake, pytest.raises(SemanticError) as caught:
        ask(query(), compiled or COMPILED, lake)
    assert caught.value.code == "BRIDGE_WEIGHTS_INVALID"
    return dict(caught.value.details)


def refused(
    document: Document, compiled: CompiledModel | None = None, policy: PolicyModel | None = None
) -> tuple[str, dict[str, Any]]:
    spy = Spy()
    with lake_of(DATA) as lake, pytest.raises(SemanticError) as caught:
        ask(document, compiled or COMPILED, lake, policy=policy, read=spy)
    assert spy.calls == 0  # refused before any read
    return caught.value.code, dict(caught.value.details)


# --- CASE-011 ---------------------------------------------------------------------------------


def test_case_011_allocates_revenue_by_campaign_exactly() -> None:
    compiled, _, _ = corpus_case("CASE-011")
    with lake_for("CASE-011") as lake:
        found = ask(ce.QUERIES["CASE-011"], compiled, lake)
    assert ce.rows_of(found) == [["Campaign A", "110.00"], ["Campaign B", "40.00"]]
    ids = {o.id for o in found.report.obligations}
    assert {"C7.weights:order_campaign", "C7.allocate:allocated_revenue"} <= ids
    (aggregate,) = found.plan.aggregates
    assert {"C7.weights:order_campaign", "C7.allocate:allocated_revenue"} <= set(
        aggregate.discharges
    )


# --- conservation and rounding ----------------------------------------------------------------


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.integers(min_value=-99999, max_value=99999),
            st.integers(min_value=0, max_value=100),
        ),
        min_size=1,
        max_size=6,
    )
)
def test_the_exact_allocation_adds_up_to_the_fact_s_total(
    orders: list[tuple[int, int]],
) -> None:
    """Whole amounts times weights in hundredths need no rounding at scale 2, so the groups show
    the exact allocation: they add up exactly to the fact's total."""
    data: dict[str, list[list[str]]] = {
        **DATA,
        "orders": [],
        "order_campaign": [],
    }
    total = Decimal(0)
    for n, (units, split) in enumerate(orders):
        value = Decimal(units)
        total += value
        data["orders"].append([str(n), "North", f"{value:.2f}"])
        data["order_campaign"] += [
            [str(n), "A", f"{Decimal(split) / 100:.2f}"],
            [str(n), "B", f"{Decimal(100 - split) / 100:.2f}"],
        ]
    found = rows(data)
    assert sum(Decimal(str(r[1])) for r in found) == total


def test_the_sum_is_rounded_once_and_never_adjusted() -> None:
    data = DATA | {
        "orders": [["1", "North", "0.05"], ["2", "North", "0.05"], ["3", "South", "0.05"]],
        "order_campaign": [
            ["1", "A", "0.50"],
            ["1", "B", "0.50"],
            ["2", "A", "0.50"],
            ["2", "B", "0.50"],
            ["3", "A", "0.50"],
            ["3", "B", "0.50"],
        ],
    }
    # Each group is 3 * 0.025 = 0.075, rounded once to 0.08; row by row it would be 3 * 0.03.
    assert rows(data) == [["Campaign A", "0.08"], ["Campaign B", "0.08"]]
    # The shown values add up to 0.16, the total is 0.15: no group is adjusted to hide it.
    assert rows(data, query("revenue", group_by=())) == [["0.15"]]


def test_the_largest_value_times_a_weight_is_exact() -> None:
    data = DATA | {
        "orders": [["1", "North", "9999999999999999.99"]],
        "order_campaign": [["1", "A", "1.00"]],
    }
    assert rows(data) == [["Campaign A", "9999999999999999.99"]]


# --- weights ----------------------------------------------------------------------------------


def test_weights_short_of_the_total_refuse_the_whole_result() -> None:
    data = DATA | {"order_campaign": [["1", "A", "0.60"], ["1", "B", "0.37"], ["2", "A", "1.00"]]}
    assert invalid(data) == {"bridge": "order_campaign", "groups": "1"}


def test_weights_within_the_tolerance_are_accepted() -> None:
    compiled, _ = model(tolerance="0.05")
    data = DATA | {"order_campaign": [["1", "A", "0.60"], ["1", "B", "0.37"], ["2", "A", "1.00"]]}
    assert rows(data, compiled=compiled) == [["Campaign A", "110.00"], ["Campaign B", "37.00"]]


def test_a_null_weight_is_invalid_never_zero() -> None:
    data = DATA | {"order_campaign": [["1", "A", "1.00"], ["1", "B", ""], ["2", "A", "1.00"]]}
    assert invalid(data) == {"bridge": "order_campaign", "groups": "1"}


def test_an_order_with_no_bridge_rows_is_invalid() -> None:
    data = DATA | {"orders": [*DATA["orders"], ["3", "East", "9.00"], ["4", "East", "1.00"]]}
    assert invalid(data) == {"bridge": "order_campaign", "groups": "2"}


def test_a_filter_on_the_far_side_still_validates_every_weight_of_the_group() -> None:
    a = {
        "scope": "board",
        "member": "campaign.name",
        "predicate": {"op": "in", "values": [{"type": "text", "value": "Campaign A"}]},
    }
    assert rows(DATA, query(filters=[a])) == [["Campaign A", "110.00"]]
    short = DATA | {"order_campaign": [["1", "A", "0.60"], ["1", "B", "0.30"], ["2", "A", "1.00"]]}
    with lake_of(short) as lake, pytest.raises(SemanticError) as caught:
        ask(query(filters=[a]), COMPILED, lake)
    assert caught.value.code == "BRIDGE_WEIGHTS_INVALID"


def test_the_validation_and_the_allocation_are_one_read() -> None:
    reads: list[Any] = []
    with lake_of(DATA) as lake:

        def counted(request: Any) -> pa.RecordBatchReader:  # noqa: ANN401
            reads.append(request)
            return lake.read(request)

        found = ask(query(), COMPILED, lake, read=counted)
    assert len(reads) == 1
    text = compile_plan(found.plan).sql
    # The rows, the same rows again for the groups read, and the bridge's own weights: one
    # statement on one snapshot.
    assert text.count('"order_campaign"') == 3


def test_a_member_not_through_the_bridge_is_a_plain_sum() -> None:
    assert rows(DATA, query(group_by=("orders.region",))) == [
        ["North", "100.00"],
        ["South", "50.00"],
    ]


# --- what stays refused -----------------------------------------------------------------------


def test_a_metric_with_no_allocation_through_a_bridge_is_refused() -> None:
    found = refused(query("revenue"))
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "bridge_allocation"})


def test_an_allocated_metric_beside_an_unallocated_one_is_refused() -> None:
    found = refused(query("allocated_revenue", "revenue"))
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "bridge_allocation"})


def test_a_metric_that_is_not_a_plain_sum_is_refused() -> None:
    compiled, _ = model(metric="COUNT")  # additive, but not a value to weigh
    found = refused(query(), compiled)
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "bridge_allocation"})


def test_a_non_additive_metric_through_a_bridge_is_refused() -> None:
    compiled, _ = model(metric="MAX")
    found = refused(query(), compiled)
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "non_additive"})


@pytest.mark.parametrize("member", ["order_campaign.weight", "order_campaign.campaign_id"])
@pytest.mark.parametrize("level", ["hidden", "masked"])
def test_a_protected_bridge_field_is_never_read(member: str, level: str) -> None:
    """The weight and the bridge's keys are read by the allocation, not by the metric's own
    expression: one the caller may not see in full refuses, before any read, naming nothing."""
    entry: Document = {"member": member, "default": level} | (
        {"mask": "0" if member.endswith("weight") else "*"} if level == "masked" else {}
    )
    policy = read_policies(
        yaml.safe_dump({"version": 1, "column_policies": [entry]}),
        COMPILED,
        groups=frozenset(),
        managed_tables={},
    )
    found = refused(query(), policy=policy)
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "bridge_allocation"})


def test_two_bridges_on_one_path_are_refused() -> None:
    found = refused(query(group_by=("channel.label",)))
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "bridge_allocation"})


def test_a_rounding_at_another_scale_than_the_metric_s_is_refused() -> None:
    compiled, _ = model(rounding={"scale": 4, "mode": "half_away_from_zero"})
    found = refused(query(), compiled)
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "bridge_allocation"})


def test_an_allocation_without_a_declared_rounding_is_refused() -> None:
    compiled, _ = model(rounding={})
    found = refused(query(), compiled)
    assert found == (
        "MISSING_EXTENSION_DECLARATION",
        {"element": "metric", "name": "allocated_revenue", "declaration": "rounding"},
    )


@pytest.mark.parametrize("dataset", ["order_campaign", "orders", "campaign"])
def test_a_row_policy_on_the_bridge_the_fact_or_the_member_refuses(dataset: str) -> None:
    policy = read_policies(
        yaml.safe_dump(
            {
                "version": 1,
                "row_policies": [
                    {
                        "name": "own",
                        "dataset": dataset,
                        "kind": "predicate",
                        "predicate": f"{TABLES[dataset][0]} IS NOT NULL",
                    }
                ],
            }
        ),
        COMPILED,
        groups=frozenset(),
        managed_tables={},
    )
    found = refused(query(), policy=policy)
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "row_policy"})
