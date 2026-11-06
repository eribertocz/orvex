"""P2-23 (#345): totals and subtotals through grouping sets, in the same statement as the detail
rows, from the same authorized rows, with every correctness rule at every level."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.correctness.discharge import DischargeError, verify_physical, verify_plan
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import read_policies
from orvex.semantic.result import read_result
from orvex.semantic.result.descriptors import InvalidResult

from conformance.runners import corpus_execution as ce
from tests.conformance import test_asof_windows as windows
from tests.conformance import test_bridges as bridges
from tests.conformance import test_conformed as conformed
from tests.conformance import test_period_end as period
from tests.conformance.test_execution import Spy, ask, corpus_case, lake_for, tiny_lake
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
Rows = list[list[str | None]]
CENT = Decimal("0.01")

COLUMNS = {
    "id": SourceColumn("BIGINT", False),
    "region": SourceColumn("VARCHAR"),
    "channel": SourceColumn("VARCHAR"),
    "customer": SourceColumn("VARCHAR"),
    "tenant": SourceColumn("VARCHAR", False),
    "amount": SourceColumn("DECIMAL(18,2)", False),
    "qty": SourceColumn("DECIMAL(18,2)", False),
}
CATALOG = {"t": COLUMNS}


def model() -> CompiledModel:
    ratio = {
        "element": "metric",
        "additivity": "recomputable",
        "components": {"numerator": "SUM(t.amount)", "denominator": "SUM(t.qty)"},
        "zero_denominator": "null",
        "rounding": {"scale": 2, "mode": "half_away_from_zero"},
    }
    distinct = {"element": "metric", "aggregate_class": "holistic", "additivity": "non_additive"}
    document = {
        "version": "0.2.0.dev0",
        "name": "totals",
        "datasets": [
            {
                "name": "t",
                "source": "t",
                "primary_key": ["id"],
                "fields": [{"name": n, "expression": {"sql": n}} for n in COLUMNS],
            }
        ],
        "metrics": [
            {"name": "revenue", "expression": {"sql": "SUM(t.amount)"}},
            {
                "name": "price",
                "expression": {"sql": "SUM(t.amount) / SUM(t.qty)"},
                "custom_extensions": [extension(ratio)],
            },
            {
                "name": "customers",
                "expression": {"sql": "COUNT(DISTINCT t.customer)"},
                "custom_extensions": [extension(distinct)],
            },
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/totals.ossie.json", VENDORS)
    return compile_model(loaded, CATALOG, VENDORS)


COMPILED = model()
METRICS = ("revenue", "price", "customers")


def query(
    totals: str | None = "rollup",
    group_by: tuple[str, ...] = ("t.region", "t.channel"),
    **more: Any,  # noqa: ANN401
) -> Document:
    found: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": m} for m in METRICS],
    }
    if group_by:
        found["group_by"] = [{"member": g} for g in group_by]
    if totals is not None:
        found["totals"] = totals
    return found | more


Sale = tuple[str | None, str | None, str | None, int, int]  # region, channel, customer, cents, qty


def table(sales: list[Sale], tenants: list[str] | None = None) -> list[dict[str, str | None]]:
    return [
        {
            "id": str(n),
            "region": r,
            "channel": c,
            "customer": who,
            "tenant": tenants[n] if tenants else "A",
            "amount": f"{Decimal(cents) / 100:.2f}",
            "qty": f"{qty}.00",
        }
        for n, (r, c, who, cents, qty) in enumerate(sales)
    ]


def answer(document: Document, sales: list[Sale], **more: Any) -> Any:  # noqa: ANN401
    with tiny_lake(CATALOG, table(sales, more.pop("tenants", None))) as lake:
        return ask(document, COMPILED, lake, **more)


def shown(value: Decimal | int | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return str(value)
    return f"{abs(value) if value == 0 else value:.2f}"  # a decimal zero has no sign


def reference(
    sales: list[Sale], levels: tuple[int, ...], descending: bool = True
) -> list[tuple[str, int, Rows]]:
    """Every level by its own rule, in the pivot-table order (Owner decision on #345)."""
    groups: dict[tuple[int, tuple[str | None, ...]], list[Sale]] = {}
    for sale in sales:
        for level in (2, *levels):
            dims: tuple[str | None, ...] = (sale[0], sale[1])[:level]
            groups.setdefault((level, dims), []).append(sale)

    def values(found: list[Sale]) -> list[Decimal | int | None]:
        revenue = sum((Decimal(s[3]) / 100 for s in found), Decimal(0))
        qty = sum(s[4] for s in found)
        price = (revenue / qty).quantize(CENT, ROUND_HALF_UP) if qty else None
        return [revenue, price, len({s[2] for s in found if s[2] is not None})]

    computed = {key: values(found) for key, found in groups.items()}

    def key(entry: tuple[int, tuple[str | None, ...]]) -> list[object]:
        level, dims = entry
        out: list[object] = []
        for i in (1, 2):
            out.append(level < i)
            ancestor = computed.get((i, dims[:i])) if level >= i else None
            value = ancestor[0] if ancestor else None
            out += [value is None, (-value if descending else value) if value is not None else 0]
            dim = dims[i - 1] if level >= i else None
            out += [dim is None, dim or ""]
        return out

    found = []
    for entry in sorted(computed, key=key):
        level, dims = entry
        role = "detail" if level == 2 else "total" if level == 0 else "subtotal"
        cells = [*dims, *([None] * (2 - level)), *(shown(v) for v in computed[entry])]
        found.append((role, level, [cells]))
    return found


def got(found: Any) -> list[tuple[str, int, Rows]]:  # noqa: ANN401
    roles = found.result.envelope()["row_roles"]
    return [(r["role"], r["level"], [row]) for r, row in zip(roles, ce.rows_of(found), strict=True)]


# --- every level, by its own rule ---------------------------------------------------------------

SALES: list[Sale] = [
    ("North", "web", "a", 10000, 4),
    ("North", "shop", "b", 30000, 3),
    ("South", "web", "a", 25000, 5),
    ("South", "web", "c", 5000, 1),
    (None, "web", "a", 100, 1),
]


def test_rollup_gives_every_level_in_pivot_order() -> None:
    found = got(answer(query(order=[{"by": "revenue", "direction": "desc"}]), SALES))
    assert found == [
        ("detail", 2, [["North", "shop", "300.00", "100.00", "1"]]),
        ("detail", 2, [["North", "web", "100.00", "25.00", "1"]]),
        ("subtotal", 1, [["North", None, "400.00", "57.14", "2"]]),  # 400 / 7, never averaged
        ("detail", 2, [["South", "web", "300.00", "50.00", "2"]]),
        ("subtotal", 1, [["South", None, "300.00", "50.00", "2"]]),
        ("detail", 2, [[None, "web", "1.00", "1.00", "1"]]),  # a real NULL region
        ("subtotal", 1, [[None, None, "1.00", "1.00", "1"]]),
        ("total", 0, [[None, None, "701.00", "50.07", "3"]]),  # 3 customers, never 1 + 2 + 2 + 1
    ]


def test_grand_gives_the_detail_rows_and_one_total() -> None:
    found = got(answer(query("grand"), SALES))
    assert [(role, level) for role, level, _ in found] == [("detail", 2)] * 4 + [("total", 0)]
    assert found[-1][2] == [[None, None, "701.00", "50.07", "3"]]


def test_a_real_null_and_a_rolled_up_group_are_told_apart() -> None:
    found = answer(query(), SALES)
    roles = found.result.envelope()["row_roles"]
    rows = ce.rows_of(found)
    null_region = [r["role"] for r, row in zip(roles, rows, strict=True) if row[0] is None]
    assert null_region == ["detail", "subtotal", "total"]  # same cells, three meanings
    read_result(json.dumps(found.result.envelope() | {"rows": rows}), found.result.arrow())


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["North", "South", None]),
            st.sampled_from(["web", "shop"]),
            st.sampled_from(["a", "b", "c", None]),
            st.integers(-99999, 99999),
            st.integers(0, 9),
        ),
        min_size=1,
        max_size=12,
    ),
    st.sampled_from(["rollup", "grand"]),
    st.booleans(),
)
def test_every_level_equals_its_reference(sales: list[Sale], totals: str, descending: bool) -> None:
    order = [{"by": "revenue", "direction": "desc" if descending else "asc"}]
    levels = (1, 0) if totals == "rollup" else (0,)
    assert got(answer(query(totals, order=order), sales)) == reference(sales, levels, descending)


def test_none_and_absent_are_the_same_question() -> None:
    absent = answer(query(None), SALES)
    none = answer(query("none"), SALES)
    assert (
        absent.result.envelope()["query_fingerprint"] == none.result.envelope()["query_fingerprint"]
    )
    assert ce.rows_of(absent) == ce.rows_of(none)
    assert "row_roles" not in absent.result.envelope()


def test_a_limit_never_changes_a_total() -> None:
    whole = got(answer(query(), SALES))
    for limit in range(1, len(whole) + 1):
        assert got(answer(query(limit=str(limit)), SALES)) == whole[:limit]


# --- the other rules at a total -----------------------------------------------------------------


def test_a_balance_at_a_level_without_its_time_axis_is_each_entity_s_latest_value() -> None:
    compiled, catalog = period.snapshots_model()
    data = period.rows(
        (1, "2026-01-31", "North", "100.00"),
        (2, "2026-01-31", "North", "200.00"),
        (1, "2026-02-28", "North", "110.00"),
        (3, "2026-01-31", "South", "50.00"),  # account 3 stops in January
    )
    by_month = {"member": "t.day", "bucket": {"unit": "month"}}
    region = {"member": "t.region"}
    with tiny_lake(catalog, data) as lake:
        region_first = ce.rows_of(
            ask(
                period.query("balance", group_by=[region, by_month], totals="rollup"),
                compiled,
                lake,
            )
        )
        month_first = ce.rows_of(
            ask(
                period.query("balance", group_by=[by_month, region], totals="rollup"),
                compiled,
                lake,
            )
        )
    assert region_first == [
        ["North", "2026-01-01", "300.00"],
        ["North", "2026-02-01", "110.00"],
        ["North", None, "310.00"],  # 110 + 200: each account's latest, never 300 + 110
        ["South", "2026-01-01", "50.00"],
        ["South", None, "50.00"],
        [None, None, "360.00"],
    ]
    assert month_first == [
        ["2026-01-01", "North", "300.00"],
        ["2026-01-01", "South", "50.00"],
        ["2026-01-01", None, "350.00"],  # the month kept: its own period end
        ["2026-02-01", "North", "110.00"],
        ["2026-02-01", None, "110.00"],
        [None, None, "360.00"],
    ]


def test_an_allocation_total_is_exact_then_rounded_once() -> None:
    data = bridges.DATA | {
        "orders": [["1", "North", "0.05"], ["2", "North", "0.05"], ["3", "South", "0.05"]],
        "order_campaign": [[str(o), c, "0.50"] for o in (1, 2, 3) for c in ("A", "B")],
    }
    # The shown groups are 0.08 each; the total is 0.15, never their sum (decision 5).
    assert bridges.rows(data, bridges.query(totals="grand")) == [
        ["Campaign A", "0.08"],
        ["Campaign B", "0.08"],
        [None, "0.15"],
    ]


def test_metrics_over_metrics_are_computed_after_alignment_at_every_level() -> None:
    document = conformed.query(
        "gross_revenue", "refunds", "net_revenue", "attainment", totals="rollup"
    )
    with conformed.lake_of(conformed.DATA) as lake:
        found = ce.rows_of(ask(document, conformed.COMPILED, lake))
    assert found == [
        ["2026-01-01", "North", "300.00", "20.00", "280.00", "0.75"],
        ["2026-01-01", "South", "100.00", "5.00", "95.00", "1.25"],
        ["2026-01-01", None, "400.00", "25.00", "375.00", "0.83"],  # 400 / 480, recomputed
        ["2026-02-01", "East", "50.00", None, None, None],
        ["2026-02-01", None, "50.00", None, None, None],
        [None, None, "450.00", "25.00", "425.00", "0.94"],
    ]


def test_a_running_total_is_undefined_at_every_total_level() -> None:
    document = windows.query(
        "cumulative_by_region", group_by=("daily.day", "daily.region"), totals="rollup"
    )
    with windows.lake_of(windows.DAYS, windows.DAILY_CATALOG) as lake:
        found = ask(document, windows.DAILY, lake)
    roles = [r["role"] for r in found.result.envelope()["row_roles"]]
    states = {(s["row"], s["state"]) for s in found.result.envelope()["cell_states"]}
    assert states == {(i, "undefined_at_total") for i, role in enumerate(roles) if role != "detail"}
    detail = [row for row, role in zip(ce.rows_of(found), roles, strict=True) if role == "detail"]
    assert detail == [
        ["2026-01-01", "North", "100.00"],
        ["2026-01-02", "North", "150.00"],
        ["2026-01-02", "South", "10.00"],
        ["2026-01-03", "North", "175.00"],
        ["2026-01-03", "South", "15.00"],
    ]


# --- security ---------------------------------------------------------------------------------

TENANT = {"name": "own", "dataset": "t", "kind": "predicate", "predicate": "tenant = $tenant"}


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.tuples(
                st.sampled_from(["North", "South"]),
                st.sampled_from(["web", "shop"]),
                st.sampled_from(["a", "b"]),
                st.integers(-9999, 9999),
                st.integers(0, 5),
            ),
            st.sampled_from("AB"),
        ),
        min_size=1,
        max_size=10,
    )
)
def test_every_level_reads_authorized_rows_only(entries: list[tuple[Sale, str]]) -> None:
    policy = read_policies(
        yaml.safe_dump({"version": 1, "row_policies": [TENANT]}),
        COMPILED,
        groups=frozenset(),
        managed_tables={},
    )
    context = ce.viewer_context((), {"tenant": {"type": "text", "value": "A"}})
    sales = [s for s, _ in entries]
    document = query(order=[{"by": "revenue"}])
    found = answer(document, sales, tenants=[t for _, t in entries], policy=policy, context=context)
    authorized = [s for s, t in entries if t == "A"]
    expected = (
        reference(authorized, (1, 0), descending=False)
        if authorized
        else [("total", 0, [[None, None, None, None, "0"]])]
    )
    assert got(found) == expected
    assert found.physical.sql.count('"#p"') >= 1  # one statement, restricted before every level


# --- refusals, obligations and the result contract --------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        {
            "kind": "semantic_query",
            "version": 1,
            "mode": "detail",
            "fields": [{"member": "t.region"}],
            "order": [{"by": "t.region"}],
            "page": {"size": 10},
            "totals": "grand",
        },
        query("rollup", group_by=()),
    ],
)
def test_totals_where_they_mean_nothing_are_refused_before_any_read(document: Document) -> None:
    spy = Spy()
    with tiny_lake(CATALOG, table(SALES)) as lake, pytest.raises(SemanticError) as caught:
        ask(document, COMPILED, lake, read=spy)
    assert caught.value.code == "QUERY_INVALID"
    assert spy.calls == 0


def test_totals_over_a_running_total_s_shown_rows_are_refused_before_any_read() -> None:
    document = windows.query(
        "cumulative",
        group_by=("daily.day",),
        filters=[windows.date_filter("daily.day", "2026-01-02", "2026-01-04")],
        totals="grand",
    )
    spy = Spy()
    with (
        windows.lake_of(windows.DAYS, windows.DAILY_CATALOG) as lake,
        pytest.raises(SemanticError) as caught,
    ):
        ask(document, windows.DAILY, lake, read=spy)
    assert (caught.value.code, dict(caught.value.details)) == (
        "UNSUPPORTED_OPERATION",
        {"operation": "totals_window_filter"},
    )
    assert spy.calls == 0


def test_every_level_s_obligations_are_discharged_with_evidence() -> None:
    found = answer(query(), SALES)
    recorded = {
        o["obligation"]: o["evidence"] for o in found.receipt.document()["plan"]["obligations"]
    }
    for level in ("", "@1", "@0"):
        assert recorded[f"C1.aggregate:revenue{level}"] == f"aggregate:t{level}"
        assert recorded[f"C2.recompute:price{level}"] == f"aggregate:t{level}"
        assert recorded[f"C2.distinct:customers{level}"] == f"aggregate:t{level}"


def test_a_plan_or_statement_without_its_levels_never_runs() -> None:
    found = answer(query(), SALES)
    with pytest.raises(DischargeError):
        verify_plan(replace(found.plan, levels=(0,)))
    with pytest.raises(DischargeError):
        verify_plan(replace(found.plan, totals=None))
    flat = replace(
        found.physical, sql=found.physical.sql.replace("GROUPING SETS", "GROUP BY ROLLUP")
    )
    with pytest.raises(DischargeError):
        verify_physical(found.plan, flat)
    with pytest.raises(DischargeError):
        verify_physical(found.plan, replace(found.physical, level=None))


def test_the_corpus_cases_answer_with_totals() -> None:
    compiled, _, _ = corpus_case("CASE-008")
    with lake_for("CASE-008") as lake:
        found = ask(ce.QUERIES["CASE-008"] | {"totals": "grand"}, compiled, lake)
    assert ce.rows_of(found)[-1] == [None, "3"]  # three customers, never 2 + 2


def test_a_result_whose_row_roles_lie_is_refused_by_the_reader() -> None:
    found = answer(query(), SALES)
    envelope, rows = found.result.envelope(), ce.rows_of(found)
    roles = envelope["row_roles"]
    for bad in (
        roles[:-1],
        [{"role": "subtotal", "level": 2}, *roles[1:]],
        [*roles[:-1], {"role": "total", "level": 1}],
    ):
        with pytest.raises(InvalidResult):
            read_result(json.dumps(envelope | {"rows": rows, "row_roles": bad}))
    held = [["North", "web", *row[2:]] if i == len(rows) - 1 else row for i, row in enumerate(rows)]
    with pytest.raises(InvalidResult):  # a rolled-up group holding a value
        read_result(json.dumps(envelope | {"rows": held}))
