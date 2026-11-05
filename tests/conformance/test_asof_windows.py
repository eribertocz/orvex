"""P2-20 (#340): as-of joins guarded to exactly one match (C6), and running totals computed
after aggregation (S9)."""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pyarrow as pa
import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
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
Data = dict[str, list[list[str]]]


def compile_document(document: Document, catalog: dict[str, Any]) -> CompiledModel:
    loaded = read(json.dumps(document).encode(), "semantic/p220.ossie.json", VENDORS)
    return compile_model(loaded, catalog, VENDORS)


@contextmanager
def lake_of(data: Data, catalog: dict[str, Any]) -> Iterator[Lake]:
    with TemporaryDirectory() as folder:
        paths = []
        for table, rows in data.items():
            path = Path(folder) / f"{table}.csv"
            lines = [",".join(catalog[table])] + [",".join(r) for r in rows]
            path.write_text("\n".join(lines) + "\n", "utf-8")
            paths.append(path)
        with ce.case_lake(paths, catalog) as lake:
            yield lake


def query(*metrics: str, group_by: tuple[str, ...], **more: Any) -> Document:  # noqa: ANN401
    found: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": m} for m in metrics],
    }
    if group_by:
        found["group_by"] = [{"member": g} for g in group_by]
    return found | more


def text_filter(member: str, *values: str) -> Document:
    return {
        "scope": "board",
        "member": member,
        "predicate": {"op": "in", "values": [{"type": "text", "value": v} for v in values]},
    }


def date_filter(member: str, start: str, end: str) -> Document:
    return {
        "scope": "board",
        "member": member,
        "predicate": {
            "op": "range",
            "gte": {"type": "date", "value": start},
            "lt": {"type": "date", "value": end},
        },
    }


# --- as-of joins ------------------------------------------------------------------------------

HISTORY_CATALOG = {
    "sales": {
        "sale_id": SourceColumn("BIGINT", False),
        "customer_id": SourceColumn("BIGINT", False),
        "sale_date": SourceColumn("DATE", False),
        "amount": SourceColumn("DECIMAL(18,2)", False),
    },
    "customer_history": {
        "customer_id": SourceColumn("BIGINT", False),
        "region": SourceColumn("VARCHAR", False),
        "valid_from": SourceColumn("DATE", False),
        "valid_to": SourceColumn("DATE", False),
    },
}


def history_model() -> CompiledModel:
    temporal = {
        "event_time": "sales.sale_date",
        "valid_from": "customer_history.valid_from",
        "valid_to": "customer_history.valid_to",
        "boundary": "closed_open",
        "match": "exactly_one",
    }
    document = {
        "version": "0.2.0.dev0",
        "name": "history",
        "datasets": [
            {
                "name": t,
                "source": t,
                "primary_key": ["sale_id"] if t == "sales" else ["customer_id", "valid_from"],
                "fields": [{"name": c, "expression": {"sql": c}} for c in columns],
            }
            for t, columns in HISTORY_CATALOG.items()
        ],
        "relationships": [
            {
                "name": "customer_at_sale",
                "from": "sales",
                "to": "customer_history",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
                "custom_extensions": [extension({"element": "relationship", "temporal": temporal})],
            }
        ],
        "metrics": [{"name": "revenue", "expression": {"sql": "SUM(sales.amount)"}}],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    return compile_document(document, HISTORY_CATALOG)


HISTORY = history_model()
BY_REGION = ("customer_history.region",)
CASE_013: Data = {
    "sales": [["1", "1", "2026-01-15", "100.00"], ["2", "1", "2026-03-15", "200.00"]],
    "customer_history": [
        ["1", "North", "2026-01-01", "2026-02-01"],
        ["1", "South", "2026-02-01", "9999-12-31"],
    ],
}


def by_region(data: Data, **more: Any) -> Rows:  # noqa: ANN401
    with lake_of(data, HISTORY_CATALOG) as lake:
        return ce.rows_of(ask(query("revenue", group_by=BY_REGION, **more), HISTORY, lake))


def unmatched(data: Data, **more: Any) -> dict[str, Any]:  # noqa: ANN401
    with lake_of(data, HISTORY_CATALOG) as lake, pytest.raises(SemanticError) as caught:
        ask(query("revenue", group_by=BY_REGION, **more), HISTORY, lake)
    assert caught.value.code == "TEMPORAL_MATCH_NOT_UNIQUE"
    return dict(caught.value.details)


def test_case_013_takes_the_region_valid_at_each_sale() -> None:
    compiled, _, _ = corpus_case("CASE-013")
    with lake_for("CASE-013") as lake:
        found = ask(ce.QUERIES["CASE-013"], compiled, lake)
    assert ce.rows_of(found) == [["North", "100.00"], ["South", "200.00"]]
    assert any(o.id.startswith("C6.as_of:") for o in found.report.obligations)


def test_the_boundary_instant_belongs_to_the_later_row() -> None:
    sale = {"sales": [["1", "1", "2026-02-01", "5.00"]]}
    assert by_region(CASE_013 | sale) == [["South", "5.00"]]


def test_overlapping_validity_refuses_the_whole_result() -> None:
    overlap = {
        "customer_history": [
            ["1", "North", "2026-01-01", "2026-02-15"],
            ["1", "South", "2026-02-01", "9999-12-31"],
        ],
        "sales": [["1", "1", "2026-02-10", "7.00"], ["2", "1", "2026-03-01", "1.00"]],
    }
    assert unmatched(overlap) == {"relationship": "customer_at_sale", "violations": "1"}


def test_a_filter_on_the_history_still_counts_every_valid_row() -> None:
    overlap = {
        "customer_history": [
            ["1", "North", "2026-01-01", "2026-02-15"],
            ["1", "South", "2026-02-01", "9999-12-31"],
        ],
        "sales": [["1", "1", "2026-02-10", "7.00"]],
    }
    assert unmatched(overlap, filters=[text_filter("customer_history.region", "North")]) == {
        "relationship": "customer_at_sale",
        "violations": "1",
    }


@pytest.mark.parametrize("day", ["2025-12-31", "2027-06-01"])
def test_a_row_with_no_valid_history_refuses_the_whole_result(day: str) -> None:
    gap = {
        "customer_history": [
            ["1", "North", "2026-01-01", "2026-02-01"],
            ["1", "South", "2026-03-01", "2027-01-01"],
        ],
        "sales": [["1", "1", "2026-01-10", "1.00"], ["2", "1", day, "2.00"]],
    }
    assert unmatched(gap)["violations"] == "1"


def test_the_guard_and_the_answer_are_one_read() -> None:
    reads: list[Any] = []
    with lake_of(CASE_013, HISTORY_CATALOG) as lake:

        def counted(request: Any) -> pa.RecordBatchReader:  # noqa: ANN401
            reads.append(request)
            return lake.read(request)

        ask(query("revenue", group_by=BY_REGION), HISTORY, lake, read=counted)
    assert len(reads) == 1


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(st.integers(min_value=1, max_value=40), min_size=1, max_size=4),
    st.lists(
        st.tuples(st.integers(min_value=0, max_value=99), st.integers(-9999, 9999)),
        min_size=1,
        max_size=8,
    ),
)
def test_revenue_through_history_is_never_double_counted(
    lengths: list[int], sales: list[tuple[int, int]]
) -> None:
    start = date(2026, 1, 1)
    bounds = [start]
    for length in lengths:
        bounds.append(bounds[-1] + timedelta(days=length))
    end = bounds[-1]
    history = [
        ["1", f"R{n}", a.isoformat(), b.isoformat()]
        for n, (a, b) in enumerate(itertools.pairwise(bounds))
    ]
    rows: list[list[str]] = []
    expected: dict[str, Decimal] = {}
    for n, (offset, cents) in enumerate(sales):
        day = start + timedelta(days=offset % (end - start).days)
        region = next(f"R{i}" for i, b in enumerate(bounds[1:]) if day < b)
        rows.append([str(n), "1", day.isoformat(), f"{Decimal(cents) / 100:.2f}"])
        expected[region] = expected.get(region, Decimal(0)) + Decimal(cents) / 100
    found = by_region({"sales": rows, "customer_history": history})
    assert found == [[r, f"{expected[r]:.2f}"] for r in sorted(expected)]


# --- running totals ---------------------------------------------------------------------------

DAILY_CATALOG = {
    "daily": {
        "day": SourceColumn("DATE", False),
        "region": SourceColumn("VARCHAR", False),
        "amount": SourceColumn("DECIMAL(18,2)", True),
    }
}


def window(name: str, partition: list[str], **change: Any) -> Document:  # noqa: ANN401
    declared = {
        "function": "running_total",
        "of": "daily_revenue",
        "partition_by": partition,
        "order_by": ["day"],
        "densify": "none",
    } | change
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": VENDORS[0], "expression": "daily_revenue"}]},
        "custom_extensions": [extension({"element": "metric", "window": declared})],
    }


def daily_model() -> CompiledModel:
    document = {
        "version": "0.2.0.dev0",
        "name": "daily",
        "datasets": [
            {
                "name": "daily",
                "source": "daily",
                "primary_key": ["day", "region"],
                "fields": [{"name": c, "expression": {"sql": c}} for c in DAILY_CATALOG["daily"]],
            }
        ],
        "metrics": [
            {"name": "daily_revenue", "expression": {"sql": "SUM(daily.amount)"}},
            window("cumulative", []),
            window("cumulative_by_region", ["region"]),
            window("moving", [], function="moving_average"),
            window("densified", [], densify="calendar"),
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    return compile_document(document, DAILY_CATALOG)


DAILY = daily_model()
DAYS: Data = {
    "daily": [
        ["2026-01-01", "North", "100.00"],
        ["2026-01-02", "North", "50.00"],
        ["2026-01-02", "South", "10.00"],
        ["2026-01-03", "North", "25.00"],
        ["2026-01-03", "South", "5.00"],
    ]
}


def daily(document: Document, data: Data = DAYS, policy: PolicyModel | None = None) -> Rows:
    with lake_of(data, DAILY_CATALOG) as lake:
        return ce.rows_of(ask(document, DAILY, lake, policy=policy))


def refused(document: Document, policy: PolicyModel | None = None) -> tuple[str, dict[str, Any]]:
    spy = Spy()
    with lake_of(DAYS, DAILY_CATALOG) as lake, pytest.raises(SemanticError) as caught:
        ask(document, DAILY, lake, policy=policy, read=spy)
    assert spy.calls == 0
    return caught.value.code, dict(caught.value.details)


def test_case_021_accumulates_after_aggregating_by_date() -> None:
    compiled, _, _ = corpus_case("CASE-021")
    with lake_for("CASE-021") as lake:
        found = ask(ce.QUERIES["CASE-021"], compiled, lake)
    assert ce.rows_of(found) == [
        ["2026-01-01", "100.00", "100.00"],
        ["2026-01-02", "50.00", "150.00"],
        ["2026-01-03", "25.00", "175.00"],
    ]
    (step,) = found.plan.windows
    assert step.discharges == ("S9.window:cumulative_revenue",)


def test_the_running_total_runs_over_the_aggregated_rows_only() -> None:
    with lake_of(DAYS, DAILY_CATALOG) as lake:
        found = ask(query("cumulative", group_by=("daily.day",)), DAILY, lake)
    assert ce.rows_of(found) == [
        ["2026-01-01", "100.00"],
        ["2026-01-02", "160.00"],
        ["2026-01-03", "190.00"],
    ]
    text = found.physical.sql
    inner = text[text.index(" FROM (") :]
    assert "OVER (" not in inner  # nothing inside the aggregation is a window
    assert re.search(r"SUM\(\"#daily_revenue\"\) OVER \(ORDER BY \"day\"", text)


def test_a_partition_restarts_the_running_total() -> None:
    found = daily(query("cumulative_by_region", group_by=("daily.day", "daily.region")))
    assert found == [
        ["2026-01-01", "North", "100.00"],
        ["2026-01-02", "North", "150.00"],
        ["2026-01-02", "South", "10.00"],
        ["2026-01-03", "North", "175.00"],
        ["2026-01-03", "South", "15.00"],
    ]


def test_a_filter_shows_rows_without_restarting_the_running_total() -> None:
    every = daily(query("daily_revenue", "cumulative", group_by=("daily.day",)))
    third = date_filter("daily.day", "2026-01-03", "2026-01-04")
    shown = daily(query("daily_revenue", "cumulative", group_by=("daily.day",), filters=[third]))
    assert shown == [every[2]] == [["2026-01-03", "30.00", "190.00"]]


def test_a_filter_on_another_member_restricts_the_rows_summed() -> None:
    north = text_filter("daily.region", "North")
    found = daily(query("cumulative", group_by=("daily.day",), filters=[north]))
    assert found == [["2026-01-01", "100.00"], ["2026-01-02", "150.00"], ["2026-01-03", "175.00"]]


def test_a_null_never_becomes_zero() -> None:
    data = {
        "daily": [
            ["2026-01-01", "North", ""],
            ["2026-01-02", "North", "5.00"],
            ["2026-01-03", "North", ""],
        ]
    }
    found = daily(query("daily_revenue", "cumulative", group_by=("daily.day",)), data)
    assert found == [
        ["2026-01-01", None, None],
        ["2026-01-02", "5.00", "5.00"],
        ["2026-01-03", None, "5.00"],
    ]


@pytest.mark.parametrize(
    ("metric", "group_by"),
    [
        ("cumulative", ()),
        ("cumulative", ("daily.region",)),
        ("cumulative", ("daily.day", "daily.region")),
        ("cumulative_by_region", ("daily.day",)),
    ],
)
def test_another_grain_than_its_order_and_partition_is_ambiguous(
    metric: str, group_by: tuple[str, ...]
) -> None:
    assert refused(query(metric, group_by=group_by)) == (
        "METRIC_ALIGNMENT_AMBIGUOUS",
        {"metric": metric, "missing": "grain"},
    )


@pytest.mark.parametrize("metric", ["moving", "densified"])
def test_other_windows_stay_refused_before_any_read(metric: str) -> None:
    assert refused(query(metric, group_by=("daily.day",))) == (
        "UNSUPPORTED_OPERATION",
        {"operation": "ordered"},
    )


def test_a_filter_on_a_bucketed_order_is_refused() -> None:
    document = query("cumulative", group_by=())
    document["group_by"] = [{"member": "daily.day", "bucket": {"unit": "month"}}]
    document["filters"] = [date_filter("daily.day", "2026-01-02", "2026-02-01")]
    assert refused(document) == ("UNSUPPORTED_OPERATION", {"operation": "window_filter"})


def test_a_hidden_source_hides_the_running_total() -> None:
    policy = read_policies(
        yaml.safe_dump(
            {"version": 1, "column_policies": [{"member": "daily.amount", "default": "hidden"}]}
        ),
        DAILY,
        groups=frozenset(),
        managed_tables={},
    )
    assert refused(query("cumulative", group_by=("daily.day",)), policy) == (
        "MEMBER_NOT_FOUND",
        {"member": "cumulative"},
    )


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.one_of(st.none(), st.integers(-99999, 99999)), min_size=1, max_size=10))
def test_running_totals_match_a_python_reference(values: list[int | None]) -> None:
    start = date(2026, 1, 1)
    data = {
        "daily": [
            [
                (start + timedelta(days=n)).isoformat(),
                "North",
                "" if v is None else f"{v / 100:.2f}",
            ]
            for n, v in enumerate(values)
        ]
    }
    found = daily(query("cumulative", group_by=("daily.day",)), data)
    total: Decimal | None = None
    expected = []
    for n, value in enumerate(values):
        if value is not None:
            total = (total or Decimal(0)) + Decimal(value) / 100
        expected.append(
            [(start + timedelta(days=n)).isoformat(), None if total is None else f"{total:.2f}"]
        )
    assert found == expected
