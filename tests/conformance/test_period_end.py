"""P2-14 (#326): distinct counts recomputed at the requested grain; period-end balances."""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie.reader import read
from orvex.store.json_encoding import arrow_to_json

from conformance.runners import corpus_execution as ce
from tests.conformance.test_execution import ask, corpus_case, lake_for, tiny_lake
from tests.support.policies import VENDORS, extension

Rows = list[dict[str, str | None]]
PERIOD_END = [
    {"dimension": "day", "behavior": "period_end"},
    {"entity": "account", "behavior": "additive"},
]


def snapshots_model(
    *,
    key: tuple[str, ...] = ("account_id", "day"),
    axes: list[Any] | None = None,
    extra_metrics: list[dict[str, Any]] | None = None,
    day: str = "DATE",
    zone: str = "UTC",
) -> tuple[CompiledModel, dict[str, Any]]:
    columns = {
        "snapshot_id": SourceColumn("BIGINT", False),
        "account_id": SourceColumn("BIGINT", False),
        "day": SourceColumn(day, False),
        "region": SourceColumn("VARCHAR"),
        "customer": SourceColumn("VARCHAR"),
        "balance": SourceColumn("DECIMAL(18,2)"),
    }
    document = {
        "version": "0.2.0.dev0",
        "name": "snapshots",
        "datasets": [
            {
                "name": "t",
                "source": "t",
                "primary_key": list(key),
                "fields": [{"name": n, "expression": {"sql": n}} for n in columns],
                "custom_extensions": [
                    extension(
                        {
                            "element": "dataset",
                            "time_roles": {"snapshot": "day"},
                            "entity_keys": [
                                {"entity": "account", "fields": ["account_id"], "coarser": True}
                            ],
                        }
                    )
                ],
            }
        ],
        "metrics": [
            {
                "name": "balance",
                "expression": {"sql": "SUM(t.balance)"},
                "custom_extensions": [
                    extension(
                        {
                            "element": "metric",
                            "additivity": "semi_additive",
                            "non_additive_axes": axes if axes is not None else PERIOD_END,
                            "period_end": {"order_by": ["t.day"]},
                        }
                    )
                ],
            },
            {
                "name": "customers",
                "expression": {"sql": "COUNT(DISTINCT t.customer)"},
                "custom_extensions": [
                    extension(
                        {
                            "element": "metric",
                            "aggregate_class": "holistic",
                            "additivity": "non_additive",
                        }
                    )
                ],
            },
            *(extra_metrics or []),
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": zone})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/snapshots.ossie.json", VENDORS)
    return compile_model(loaded, {"t": columns}, VENDORS), {"t": columns}


def rows(*values: tuple[int, str, str, str | None]) -> Rows:
    return [
        {
            "snapshot_id": str(i),
            "account_id": str(a),
            "day": d,
            "region": r,
            "customer": None,
            "balance": b,
        }
        for i, (a, d, r, b) in enumerate(values, 1)
    ]


def query(*metrics: str, group_by: list[Any] | None = None, **more: Any) -> dict[str, Any]:  # noqa: ANN401
    found: dict[str, Any] = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": m} for m in metrics],
    }
    if group_by:
        found["group_by"] = group_by
    return found | more


BY_MONTH = [{"member": "t.region"}, {"member": "t.day", "bucket": {"unit": "month"}}]


def answer_of(compiled: CompiledModel, catalog: dict[str, Any], data: Rows, q: Any) -> Any:  # noqa: ANN401
    with tiny_lake(catalog, data) as lake:
        return ask(q, compiled, lake)


# --- the corpus -----------------------------------------------------------------------------


def test_case_007_takes_each_accounts_month_end_balance() -> None:
    compiled, _, _ = corpus_case("CASE-007")
    with lake_for("CASE-007") as lake:
        found = ask(ce.QUERIES["CASE-007"], compiled, lake)
    assert ce.rows_of(found) == [
        ["North", "2026-01-01", "300.00"],
        ["North", "2026-02-01", "330.00"],
    ]
    obligations = {o["obligation"] for o in found.receipt.document()["plan"]["obligations"]}
    assert "C2.period_end:balance" in obligations


def test_case_008_recomputes_distinct_counts_never_adding_them() -> None:
    compiled, _, _ = corpus_case("CASE-008")
    with lake_for("CASE-008") as lake:
        by_region = ce.rows_of(ask(ce.QUERIES["CASE-008"], compiled, lake))
        overall = ce.rows_of(ask(query("active_customers"), compiled, lake))
    assert by_region == [["North", "2"], ["South", "2"]]
    assert overall == [["3"]]
    added = sum(int(str(r[1])) for r in by_region)
    assert (added, overall[0][0]) == (4, "3")  # the sum of the regions is never the answer


def test_case_020_refuses_a_balance_without_its_time_axis() -> None:
    compiled, _, _ = corpus_case("CASE-020")
    with lake_for("CASE-020") as lake, pytest.raises(SemanticError) as caught:
        ask(ce.QUERIES["CASE-020"], compiled, lake)
    assert caught.value.code == "ILLEGAL_TEMPORAL_REAGGREGATION"


# --- period end -----------------------------------------------------------------------------


def test_missing_snapshots_and_nulls_never_change_the_selection() -> None:
    compiled, catalog = snapshots_model()
    data = rows(
        (1, "2026-01-05", "N", "10.00"),
        (1, "2026-01-31", "N", "20.00"),  # account 1, January: the latest
        (2, "2026-01-10", "N", "5.00"),
        (2, "2026-01-20", "N", None),  # account 2, January: its latest is NULL, never 5.00
        (1, "2026-02-03", "N", "7.00"),  # account 2 has no February snapshot
    )
    found = answer_of(compiled, catalog, data, query("balance", group_by=BY_MONTH))
    assert ce.rows_of(found) == [["N", "2026-01-01", "20.00"], ["N", "2026-02-01", "7.00"]]
    only_null = rows((2, "2026-01-10", "N", "5.00"), (2, "2026-01-20", "N", None))
    found = answer_of(compiled, catalog, only_null, query("balance", group_by=BY_MONTH))
    assert ce.rows_of(found) == [["N", "2026-01-01", None]]


def test_the_physical_order_of_rows_changes_nothing() -> None:
    compiled, catalog = snapshots_model()
    data = rows(
        *[(a, f"2026-01-{d:02d}", "N", f"{a * d}.00") for a in (1, 2, 3) for d in (3, 17, 28)]
    )
    shuffled = data[:]
    random.Random(7).shuffle(shuffled)
    one = answer_of(compiled, catalog, data, query("balance", group_by=BY_MONTH))
    two = answer_of(compiled, catalog, shuffled, query("balance", group_by=BY_MONTH))
    assert ce.rows_of(one) == ce.rows_of(two) == [["N", "2026-01-01", "168.00"]]
    assert one.result.canonical(arrow_to_json) == two.result.canonical(arrow_to_json)


def test_a_key_wider_than_entity_and_axis_is_refused_even_without_ties() -> None:
    compiled, catalog = snapshots_model(key=("snapshot_id",))
    with pytest.raises(SemanticError) as caught:
        answer_of(
            compiled,
            catalog,
            rows((1, "2026-01-05", "N", "1.00")),
            query("balance", group_by=BY_MONTH),
        )
    assert dict(caught.value.details) == {"operation": "period_end_ties"}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"group_by": [{"member": "t.region"}]}, "ILLEGAL_TEMPORAL_REAGGREGATION"),
        (
            {
                "filters": [
                    {"scope": "board", "member": "t.day", "predicate": {"op": "is_not_null"}}
                ]
            },
            "period_end_filter",
        ),
    ],
)
def test_a_balance_needs_its_axis_and_no_filter_on_it(
    change: dict[str, Any], expected: str
) -> None:
    compiled, catalog = snapshots_model()
    q = query("balance", group_by=BY_MONTH) | change
    with pytest.raises(SemanticError) as caught:
        answer_of(compiled, catalog, rows((1, "2026-01-05", "N", "1.00")), q)
    assert expected in (caught.value.code, caught.value.details.get("operation"))


def test_a_balance_with_another_metric_or_two_time_axes_is_refused() -> None:
    compiled, catalog = snapshots_model()
    with pytest.raises(SemanticError) as caught:
        answer_of(
            compiled,
            catalog,
            rows((1, "2026-01-05", "N", "1.00")),
            query("balance", "customers", group_by=BY_MONTH),
        )
    assert dict(caught.value.details) == {"operation": "mixed_period_end"}
    two = [
        {"dimension": "day", "behavior": "period_end"},
        {"dimension": "account_id", "behavior": "period_end"},
    ]
    compiled, catalog = snapshots_model(axes=two)
    with pytest.raises(SemanticError) as caught:
        answer_of(
            compiled,
            catalog,
            rows((1, "2026-01-05", "N", "1.00")),
            query("balance", group_by=BY_MONTH),
        )
    assert dict(caught.value.details) == {"operation": "semi_additive"}


@settings(max_examples=15, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(1, 3), st.integers(0, 58), st.integers(-500, 500)),
        min_size=1,
        max_size=12,
        unique_by=lambda r: (r[0], r[1]),
    )
)
def test_period_end_equals_a_python_reference(snapshots: list[tuple[int, int, int]]) -> None:
    compiled, catalog = snapshots_model()
    start = date(2026, 1, 1)
    data = rows(
        *[(a, (start + timedelta(days=d)).isoformat(), "N", f"{v}.00") for a, d, v in snapshots]
    )
    latest: dict[tuple[int, str], tuple[int, int]] = {}
    for a, d, v in snapshots:
        month = (start + timedelta(days=d)).isoformat()[:7]
        if (a, month) not in latest or d > latest[(a, month)][0]:
            latest[(a, month)] = (d, v)
    reference: dict[str, Decimal] = {}
    for (_, month), (_, v) in latest.items():
        reference[month] = reference.get(month, Decimal(0)) + Decimal(v)
    expected = [["N", f"{m}-01", f"{total:.2f}"] for m, total in sorted(reference.items())]
    found = answer_of(compiled, catalog, data, query("balance", group_by=BY_MONTH))
    assert ce.rows_of(found) == expected


# --- distinct counts ------------------------------------------------------------------------


def test_a_customer_in_two_regions_counts_once_overall_and_nulls_never_count() -> None:
    compiled, catalog = snapshots_model()
    data = [
        {
            "snapshot_id": "1",
            "account_id": "1",
            "day": "2026-01-01",
            "region": "N",
            "customer": "ana",
            "balance": None,
        },
        {
            "snapshot_id": "2",
            "account_id": "2",
            "day": "2026-01-01",
            "region": "S",
            "customer": "ana",
            "balance": None,
        },
        {
            "snapshot_id": "3",
            "account_id": "3",
            "day": "2026-01-01",
            "region": "S",
            "customer": None,
            "balance": None,
        },
    ]
    by_region = answer_of(
        compiled, catalog, data, query("customers", group_by=[{"member": "t.region"}])
    )
    assert ce.rows_of(by_region) == [["N", "1"], ["S", "1"]]
    assert ce.rows_of(answer_of(compiled, catalog, data, query("customers"))) == [["1"]]


def test_an_approximate_count_stays_refused() -> None:
    sketch = {
        "name": "approx",
        "expression": {"sql": "APPROX_COUNT_DISTINCT(t.customer)"},
        "custom_extensions": [
            extension(
                {
                    "element": "metric",
                    "aggregate_class": "sketch",
                    "additivity": "non_additive",
                    "approximate": {"error_bound": "0.02"},
                }
            )
        ],
    }
    compiled, catalog = snapshots_model(extra_metrics=[sketch])
    with pytest.raises(SemanticError) as caught:
        answer_of(compiled, catalog, rows((1, "2026-01-05", "N", "1.00")), query("approx"))
    assert dict(caught.value.details) == {"operation": "approximate"}


@pytest.mark.parametrize(
    "key", [("account_id", "day", "snapshot_id"), ("account_id", "day", "region")]
)
def test_a_key_containing_the_axis_and_more_is_refused(key: tuple[str, ...]) -> None:
    compiled, catalog = snapshots_model(key=key)
    data = rows((1, "2026-01-05", "N", "100.00"), (1, "2026-01-20", "N", "200.00"))
    with pytest.raises(SemanticError) as caught:
        answer_of(compiled, catalog, data, query("balance", group_by=BY_MONTH))
    assert dict(caught.value.details) == {"operation": "period_end_ties"}


def test_a_distinct_count_declaring_time_axes_is_refused() -> None:
    declared = {
        "name": "cust_pe",
        "expression": {"sql": "COUNT(DISTINCT t.customer)"},
        "custom_extensions": [
            extension(
                {
                    "element": "metric",
                    "aggregate_class": "holistic",
                    "additivity": "semi_additive",
                    "non_additive_axes": PERIOD_END,
                    "period_end": {"order_by": ["t.day"]},
                }
            )
        ],
    }
    compiled, catalog = snapshots_model(extra_metrics=[declared])
    with pytest.raises(SemanticError) as caught:
        answer_of(
            compiled,
            catalog,
            rows((1, "2026-01-05", "N", "1.00")),
            query("cust_pe", group_by=[{"member": "t.region"}]),
        )
    assert dict(caught.value.details) == {"operation": "holistic"}


def test_a_bucket_finer_than_a_date_axis_is_refused_by_binding() -> None:
    compiled, catalog = snapshots_model()
    hourly = query("balance", group_by=[{"member": "t.day", "bucket": {"unit": "hour"}}])
    with pytest.raises(SemanticError) as caught:
        answer_of(compiled, catalog, rows((1, "2026-01-05", "N", "1.00")), hourly)
    assert caught.value.code == "QUERY_INVALID"  # an hour of a DATE does not exist (P2-05)


@settings(max_examples=15, deadline=None)
@given(
    st.lists(
        st.tuples(st.sampled_from("NS"), st.sampled_from(["ana", "bo", "cy", None])),
        min_size=1,
        max_size=10,
    )
)
def test_distinct_counts_equal_a_python_reference(pairs: list[tuple[str, str | None]]) -> None:
    compiled, catalog = snapshots_model()
    data: Rows = [
        {
            "snapshot_id": str(i),
            "account_id": str(i),
            "day": "2026-01-01",
            "region": r,
            "customer": c,
            "balance": None,
        }
        for i, (r, c) in enumerate(pairs, 1)
    ]
    by_region = ce.rows_of(
        answer_of(compiled, catalog, data, query("customers", group_by=[{"member": "t.region"}]))
    )
    overall = ce.rows_of(answer_of(compiled, catalog, data, query("customers")))
    expected = {r: len({c for rr, c in pairs if rr == r and c}) for r, _ in pairs}
    assert by_region == [[r, str(n)] for r, n in sorted(expected.items())]
    total = len({c for _, c in pairs if c})
    assert overall == [[str(total)]]
    assert total <= sum(expected.values())


def test_a_period_end_over_instants_takes_its_period_in_the_declared_zone() -> None:
    """P2-19 (#337): both snapshots are on 31 January in La Paz; the later one is the balance."""
    compiled, catalog = snapshots_model(day="TIMESTAMP WITH TIME ZONE", zone="America/La_Paz")
    data = rows(
        (1, "2026-01-31T14:00:00Z", "N", "10.00"), (1, "2026-02-01T03:00:00Z", "N", "20.00")
    )
    found = answer_of(compiled, catalog, data, query("balance", group_by=BY_MONTH))
    assert ce.rows_of(found) == [["N", "2026-01-01", "20.00"]]
