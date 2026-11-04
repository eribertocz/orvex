"""P2-17 (#333): facts aligned on declared conformed dimensions (C5), and metrics over metrics
computed after alignment (S7)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

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
ALIGN = {"grain": ["month", "region"], "time": "month"}


def field(name: str, conformed: str | None = None) -> Document:
    found: Document = {"name": name, "expression": {"sql": name}}
    if conformed:
        found["custom_extensions"] = [
            extension({"element": "field", "conformed_dimension": conformed})
        ]
    return found


def model(
    refunds_fields: list[Document] | None = None, refunds_month: str = "DATE"
) -> tuple[CompiledModel, dict[str, dict[str, SourceColumn]]]:
    """sales and refunds at any grain, targets at month x region; no relationship at all."""
    refunds = refunds_fields or [
        field("id"),
        field("month", "month"),
        field("region", "region"),
        field("channel"),
        field("amount"),
    ]
    tables = {
        "sales": [
            field("id"),
            field("month", "month"),
            field("region", "region"),
            field("channel"),
            field("amount"),
        ],
        "refunds": refunds,
        "targets": [field("month", "month"), field("region", "region"), field("amount")],
    }
    types = {"month": "DATE", "region": "VARCHAR", "channel": "VARCHAR", "amount": "DECIMAL(18,2)"}
    catalog = {
        t: {
            f["name"]: SourceColumn(
                refunds_month
                if (t, f["name"]) == ("refunds", "month")
                else types.get(f["name"], "BIGINT"),
                f["name"] not in ("id", "month"),
            )
            for f in fields
        }
        for t, fields in tables.items()
    }
    ratio_text = "CAST(gross_revenue / target_revenue AS DECIMAL(18, 2))"
    ratio: Document = {
        "element": "metric",
        "additivity": "recomputable",
        "components": {"numerator": "gross_revenue", "denominator": "target_revenue"},
        "zero_denominator": "null",
        "rounding": {"scale": 2, "mode": "half_away_from_zero"},
        "over_metrics": {"expression": ratio_text, "align": ALIGN},
    }
    document = {
        "version": "0.2.0.dev0",
        "name": "conformed",
        "datasets": [
            {
                "name": t,
                "source": t,
                "primary_key": ["month", "region"] if t == "targets" else ["id"],
                "fields": fields,
            }
            for t, fields in tables.items()
        ],
        "metrics": [
            {"name": "gross_revenue", "expression": {"sql": "SUM(sales.amount)"}},
            {"name": "refunds", "expression": {"sql": "SUM(refunds.amount)"}},
            {"name": "target_revenue", "expression": {"sql": "SUM(targets.amount)"}},
            {
                "name": "net_revenue",
                "expression": {
                    "dialects": [{"dialect": VENDORS[0], "expression": "gross_revenue - refunds"}]
                },
                "custom_extensions": [
                    extension(
                        {
                            "element": "metric",
                            "additivity": "additive",
                            "over_metrics": {
                                "expression": "gross_revenue - refunds",
                                "align": ALIGN,
                            },
                        }
                    )
                ],
            },
            {
                "name": "attainment",
                "expression": {"dialects": [{"dialect": VENDORS[0], "expression": ratio_text}]},
                "custom_extensions": [extension(ratio)],
            },
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/conformed.ossie.json", VENDORS)
    return compile_model(loaded, catalog, VENDORS), catalog


COMPILED, CATALOG = model()
BY_MONTH_REGION = ("sales.month", "sales.region")


@contextmanager
def lake_of(
    data: dict[str, list[list[str]]], catalog: dict[str, Any] | None = None
) -> Iterator[Lake]:
    catalog = catalog or CATALOG
    with TemporaryDirectory() as folder:
        paths = []
        for table, rows in data.items():
            path = Path(folder) / f"{table}.csv"
            lines = [",".join(catalog[table])] + [",".join(r) for r in rows]
            path.write_text("\n".join(lines) + "\n", "utf-8")
            paths.append(path)
        with ce.case_lake(paths, catalog) as lake:
            yield lake


def query(*metrics: str, group_by: tuple[str, ...] = BY_MONTH_REGION, **more: Any) -> Document:  # noqa: ANN401
    found: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": m} for m in metrics],
    }
    if group_by:
        found["group_by"] = [{"member": g} for g in group_by]
    return found | more


DATA = {
    "sales": [
        ["1", "2026-01-01", "North", "web", "100.00"],
        ["2", "2026-01-01", "North", "shop", "200.00"],
        ["3", "2026-01-01", "South", "web", "100.00"],
        ["4", "2026-02-01", "East", "web", "50.00"],
    ],
    "refunds": [
        ["1", "2026-01-01", "North", "web", "20.00"],
        ["2", "2026-01-01", "South", "web", "5.00"],
    ],
    "targets": [["2026-01-01", "North", "400.00"], ["2026-01-01", "South", "80.00"]],
}


def rows(document: Document, lake: Lake, compiled: CompiledModel | None = None) -> Rows:
    return ce.rows_of(ask(document, compiled or COMPILED, lake))


def refused(
    document: Document,
    compiled: CompiledModel | None = None,
    policy: PolicyModel | None = None,
) -> tuple[str, dict[str, Any]]:
    spy = Spy()
    with lake_of(DATA) as lake, pytest.raises(SemanticError) as caught:
        ask(document, compiled or COMPILED, lake, policy=policy, read=spy)
    assert spy.calls == 0  # refused before any read
    return caught.value.code, dict(caught.value.details)


# --- the corpus -------------------------------------------------------------------------------


def test_case_009_computes_net_revenue_after_alignment() -> None:
    compiled, _, _ = corpus_case("CASE-009")
    with lake_for("CASE-009") as lake:
        found = ask(ce.QUERIES["CASE-009"], compiled, lake)
    assert ce.rows_of(found) == [
        ["2026-01-01", "North", "300.00", "20.00", "280.00"],
        ["2026-01-01", "South", "100.00", "5.00", "95.00"],
    ]
    assert "C5.align:net_revenue" in {o.id for o in found.report.obligations}
    assert found.plan.derived[0].discharges == ("C5.align:net_revenue",)


def test_case_010_divides_once_after_alignment() -> None:
    compiled, _, _ = corpus_case("CASE-010")
    with lake_for("CASE-010") as lake:
        found = ask(ce.QUERIES["CASE-010"], compiled, lake)
    assert ce.rows_of(found) == [
        ["2026-01-01", "North", "300.00", "400.00", "0.75"],
        ["2026-01-01", "South", "150.00", "120.00", "1.25"],
    ]
    (step,) = found.plan.derived
    assert step.discharges == ("C2.recompute:attainment", "C5.align:attainment")
    assert step.over == ("actual_revenue", "target_revenue")


# --- order: aggregate, align, then derive -----------------------------------------------------


def test_the_derived_metric_is_computed_after_alignment_and_never_aggregated() -> None:
    with lake_of(DATA) as lake:
        found = ask(query("gross_revenue", "refunds", "net_revenue"), COMPILED, lake)
    plan = found.plan
    assert len(plan.facts) == 2
    assert [d.name for d in plan.derived] == ["net_revenue"]
    assert "net_revenue" not in {a.name for a in plan.aggregates}
    text = compile_plan(plan).sql
    # Outside the facts' own aggregations: the derivation wraps the alignment, and neither
    # aggregates anything.
    head = text[: text.index('SELECT "t')]  # before the first fact's aggregation
    tail = text[text.rindex(" GROUP BY ") :]  # after the last one
    for outside in (head, tail):
        assert not re.search(r"\b(SUM|COUNT|AVG|MIN|MAX)\(", outside)
    assert head.index('("gross_revenue" - "refunds")') < head.index("COALESCE(")
    assert text.count("FULL OUTER JOIN") == 1


def test_a_ratio_over_metrics_is_never_computed_inside_one_fact() -> None:
    data = DATA | {
        "sales": [
            ["1", "2026-01-01", "North", "web", "100.00"],
            ["2", "2026-01-01", "North", "web", "100.00"],
            ["3", "2026-01-01", "North", "web", "100.00"],
        ]
    }
    with lake_of(data) as lake:
        found = rows(query("attainment", group_by=BY_MONTH_REGION), lake)
    # 300 / 400, never the average of each invoice's ratio (0.25)
    assert found == [["2026-01-01", "North", "0.75"], ["2026-01-01", "South", None]]


def test_a_derived_metric_asked_alone_reads_its_sources_but_shows_only_itself() -> None:
    with lake_of(DATA) as lake:
        found = ask(query("net_revenue"), COMPILED, lake)
    assert [c["name"] for c in found.result.envelope()["columns"]] == [
        "month",
        "region",
        "net_revenue",
    ]
    assert ce.rows_of(found) == [
        ["2026-01-01", "North", "280.00"],
        ["2026-01-01", "South", "95.00"],
        ["2026-02-01", "East", None],
    ]
    assert {a.name: a.shown for a in found.plan.aggregates} == {
        "#gross_revenue": False,
        "#refunds": False,
    }


# --- alignment --------------------------------------------------------------------------------


def test_a_group_in_one_fact_only_gives_null_and_a_derived_null_never_zero() -> None:
    with lake_of(DATA) as lake:
        found = rows(query("gross_revenue", "refunds", "net_revenue"), lake)
    assert found == [
        ["2026-01-01", "North", "300.00", "20.00", "280.00"],
        ["2026-01-01", "South", "100.00", "5.00", "95.00"],
        ["2026-02-01", "East", "50.00", None, None],
    ]


def test_each_fact_groups_by_its_own_conformed_field() -> None:
    with lake_of(DATA) as lake:
        found = ask(query("gross_revenue", "refunds"), COMPILED, lake)
    by_fact = {
        f: sorted(g.member.dataset or "" for g in found.plan.groups if g.source == f)
        for f in found.plan.facts
    }
    assert sorted(by_fact.values()) == [["REFUNDS", "REFUNDS"], ["SALES", "SALES"]]


def test_a_filter_applies_in_each_fact_through_its_conformed_field() -> None:
    north = {
        "scope": "board",
        "member": "sales.region",
        "predicate": {"op": "in", "values": [{"type": "text", "value": "North"}]},
    }
    with lake_of(DATA) as lake:
        both = rows(query("gross_revenue", "refunds", filters=[north]), lake)
        refunds = rows(query("refunds", group_by=("refunds.month", "refunds.region")), lake)
    assert both == [["2026-01-01", "North", "300.00", "20.00"]]
    assert [both[0][3]] == [r[2] for r in refunds if r[1] == "North"]


# --- conformance is declared, never guessed ---------------------------------------------------


def test_a_same_named_field_without_a_declared_conformance_is_refused() -> None:
    found = refused(query("gross_revenue", "refunds", group_by=("sales.channel",)))
    assert found == (
        "ILLEGAL_GRAIN_TRANSITION",
        {"metric": "refunds", "grain": ["sales.channel"]},
    )


def test_a_conformed_field_of_another_type_is_refused() -> None:
    compiled, _ = model(refunds_month="VARCHAR")
    found = refused(query("gross_revenue", "refunds"), compiled)
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "multi_fact"})


def test_a_fact_missing_a_requested_conformed_dimension_is_refused() -> None:
    compiled, _ = model(
        refunds_fields=[
            field("id"),
            field("month", "month"),
            field("region"),
            field("channel"),
            field("amount"),
        ]
    )
    found = refused(query("gross_revenue", "refunds"), compiled)
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "multi_fact"})


@pytest.mark.parametrize("dataset", ["refunds", "targets"])
def test_a_row_policy_on_any_fact_refuses_before_any_read(dataset: str) -> None:
    policy = read_policies(
        yaml.safe_dump(
            {
                "version": 1,
                "row_policies": [
                    {
                        "name": "own",
                        "dataset": dataset,
                        "kind": "predicate",
                        "predicate": "amount > 0",
                    }
                ],
            }
        ),
        COMPILED,
        groups=frozenset(),
        managed_tables={},
    )
    found = refused(query("gross_revenue", "refunds", "target_revenue"), policy=policy)
    assert found == ("UNSUPPORTED_OPERATION", {"operation": "row_policy"})


@pytest.mark.parametrize("hidden", ["sales.amount", "refunds.amount"])
def test_a_metric_over_a_hidden_source_is_not_found(hidden: str) -> None:
    policy = read_policies(
        yaml.safe_dump(
            {"version": 1, "column_policies": [{"member": hidden, "default": "hidden"}]}
        ),
        COMPILED,
        groups=frozenset(),
        managed_tables={},
    )
    found = refused(query("net_revenue"), policy=policy)
    assert found == ("MEMBER_NOT_FOUND", {"member": "net_revenue"})


# --- grains -----------------------------------------------------------------------------------


def test_a_fact_at_the_requested_grain_keeps_its_rows_values() -> None:
    with lake_of(DATA) as lake:
        found = rows(query("gross_revenue", "target_revenue"), lake)
    assert found == [
        ["2026-01-01", "North", "300.00", "400.00"],
        ["2026-01-01", "South", "100.00", "80.00"],
        ["2026-02-01", "East", "50.00", None],
    ]


def test_a_member_finer_than_a_fact_s_grain_is_refused() -> None:
    found = refused(query("gross_revenue", "target_revenue", group_by=("sales.id",)))
    assert found == (
        "ILLEGAL_GRAIN_TRANSITION",
        {"metric": "target_revenue", "grain": ["sales.id"]},
    )


@pytest.mark.parametrize(
    "group_by", [("sales.month",), ("sales.month", "sales.region", "sales.channel"), ()]
)
def test_a_derived_metric_at_another_grain_is_ambiguous(group_by: tuple[str, ...]) -> None:
    found = refused(query("net_revenue", group_by=group_by))
    assert found == ("METRIC_ALIGNMENT_AMBIGUOUS", {"metric": "net_revenue", "missing": "grain"})


def test_a_month_bucket_on_the_alignment_grain_is_ambiguous() -> None:
    document = query("net_revenue", group_by=())
    document["group_by"] = [
        {"member": "sales.month", "bucket": {"unit": "month"}},
        {"member": "sales.region"},
    ]
    found = refused(document)
    assert found == ("METRIC_ALIGNMENT_AMBIGUOUS", {"metric": "net_revenue", "missing": "grain"})


# --- property ---------------------------------------------------------------------------------

MONTHS = ["2026-01-01", "2026-02-01"]
REGIONS = ["North", "South"]
cells = st.lists(
    st.tuples(
        st.sampled_from(MONTHS),
        st.sampled_from(REGIONS),
        st.integers(min_value=-99999, max_value=99999),
    ),
    max_size=6,
)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(cells, cells)
def test_net_revenue_matches_a_per_fact_reference(
    sales: list[tuple[str, str, int]], refunds: list[tuple[str, str, int]]
) -> None:
    data: dict[str, list[list[str]]] = {"sales": [], "refunds": [], "targets": []}
    totals: dict[str, dict[tuple[str, str], Decimal]] = {"sales": {}, "refunds": {}}
    for table, written in (("sales", sales), ("refunds", refunds)):
        for n, (month, region, cents) in enumerate(written):
            value = Decimal(cents) / 100
            data[table].append([str(n), month, region, "web", f"{value:.2f}"])
            key = (month, region)
            totals[table][key] = totals[table].get(key, Decimal(0)) + value
    with lake_of(data) as lake:
        found = rows(query("net_revenue"), lake)
    keys = sorted(set(totals["sales"]) | set(totals["refunds"]))
    expected = [
        [
            month,
            region,
            f"{totals['sales'][month, region] - totals['refunds'][month, region]:.2f}"
            if (month, region) in totals["sales"] and (month, region) in totals["refunds"]
            else None,
        ]
        for month, region in keys
    ]
    assert found == expected
