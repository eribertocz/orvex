"""P2-25 (#348): an aggregate table answers in place of its base only with a proof, from the
model alone, of the same answer and the same security, and only once its stored numbers are
verified consistent on the snapshot read (decision 1)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from orvex.contracts.store.v1 import StatementRequest
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.correctness.discharge import DischargeError, verify_physical, verify_plan
from orvex.semantic.evidence import verify
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies

from conformance.runners import corpus_execution as ce
from tests.conformance import test_period_end as period
from tests.conformance.test_execution import ask, tiny_lake
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
CENT = Decimal("0.01")
SALES = {
    "sale_id": SourceColumn("BIGINT", False),
    "month": SourceColumn("DATE", False),
    "day": SourceColumn("DATE", False),
    "category": SourceColumn("VARCHAR", False),
    "tenant": SourceColumn("VARCHAR", False),
    "customer": SourceColumn("VARCHAR"),
    "amount": SourceColumn("DECIMAL(18,2)", False),
    "qty": SourceColumn("DECIMAL(18,2)", False),
}
ROLLUP = {
    "month": SourceColumn("DATE", False),
    "category": SourceColumn("VARCHAR", False),
    "revenue": SourceColumn("DECIMAL(38,2)"),
    "quantity": SourceColumn("DECIMAL(38,2)"),
}
BY_CATEGORY = {"category": SourceColumn("VARCHAR", False), "revenue": SourceColumn("DECIMAL(38,2)")}
CATALOG = {"sales": SALES, "rollup": ROLLUP, "by_category": BY_CATEGORY}


def conformed(name: str, dimension: str) -> Document:
    return {
        "name": name,
        "expression": {"sql": name},
        "custom_extensions": [extension({"element": "field", "conformed_dimension": dimension})],
    }


def table(
    name: str, dimensions: list[str], measures: list[tuple[str, str, str]], declared: bool = True
) -> Document:
    stored = [
        {"name": n, "expression": e} | ({"metric": m} if declared else {}) for n, e, m in measures
    ]
    payload = {
        "element": "dataset",
        "entity": name,
        "grain": {"dimensions": dimensions},
        "aggregate_table": {"measures": stored} | ({"base_dataset": "sales"} if declared else {}),
    }
    return {
        "name": name,
        "source": name,
        "primary_key": dimensions,
        "fields": [conformed(d, d) for d in dimensions]
        + [{"name": c, "expression": {"sql": c}} for c in CATALOG[name] if c not in dimensions],
        "custom_extensions": [extension(payload)],
    }


def model(*tables: Document) -> CompiledModel:
    ratio = {
        "element": "metric",
        "additivity": "recomputable",
        "components": {"numerator": "SUM(sales.amount)", "denominator": "SUM(sales.qty)"},
        "zero_denominator": "null",
        "rounding": {"scale": 2, "mode": "half_away_from_zero"},
    }
    distinct = {"element": "metric", "aggregate_class": "holistic", "additivity": "non_additive"}
    document = {
        "version": "0.2.0.dev0",
        "name": "rollups",
        "datasets": [
            {
                "name": "sales",
                "source": "sales",
                "primary_key": ["sale_id"],
                "fields": [conformed("month", "month"), conformed("category", "category")]
                + [
                    {"name": c, "expression": {"sql": c}}
                    for c in SALES
                    if c not in ("month", "category")
                ],
            },
            *tables,
        ],
        "metrics": [
            {"name": "revenue", "expression": {"sql": "SUM(sales.amount)"}},
            {"name": "quantity", "expression": {"sql": "SUM(sales.qty)"}},
            {
                "name": "price",
                "expression": {"sql": "SUM(sales.amount) / SUM(sales.qty)"},
                "custom_extensions": [extension(ratio)],
            },
            {
                "name": "customers",
                "expression": {"sql": "COUNT(DISTINCT sales.customer)"},
                "custom_extensions": [extension(distinct)],
            },
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/rollups.ossie.json", VENDORS)
    catalog = {t: CATALOG[t] for t in ["sales", *(d["name"] for d in tables)]}
    return compile_model(loaded, catalog, VENDORS)


MONTHLY = table(
    "rollup",
    ["month", "category"],
    [("r", "SUM(revenue)", "revenue"), ("q", "SUM(quantity)", "quantity")],
)
COMPILED = model(MONTHLY)

Sale = tuple[str, str, str, str | None, int, int]  # month, category, tenant, customer, cents, qty


def lake_rows(sales: list[Sale]) -> dict[str, list[dict[str, str | None]]]:
    base = [
        {
            "sale_id": str(n),
            "month": m,
            "day": m,
            "category": c,
            "tenant": t,
            "customer": who,
            "amount": f"{Decimal(cents) / 100:.2f}",
            "qty": f"{q}.00",
        }
        for n, (m, c, t, who, cents, q) in enumerate(sales)
    ]
    groups: dict[tuple[str, str], list[Decimal]] = {}
    for m, c, _, _, cents, q in sales:
        entry = groups.setdefault((m, c), [Decimal(0), Decimal(0), Decimal(0)])
        entry[0] += Decimal(cents) / 100
        entry[1] += q
        entry[2] += 1
    rollup: list[dict[str, str | None]] = [
        {"month": m, "category": c, "revenue": f"{r:.2f}", "quantity": f"{q:.2f}", "sold": str(n)}
        for (m, c), (r, q, n) in sorted(groups.items())
    ]
    totals: dict[str, Decimal] = {}
    for (_, c), (r, _, _) in groups.items():
        totals[c] = totals.get(c, Decimal(0)) + r
    by_category: list[dict[str, str | None]] = [
        {"category": c, "revenue": f"{r:.2f}"} for c, r in sorted(totals.items())
    ]
    return {"sales": base, "rollup": rollup, "by_category": by_category}


def query(
    *metrics: str,
    group_by: tuple[str, ...] = ("sales.month", "sales.category"),
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


def policies(compiled: CompiledModel, *rules: Document) -> PolicyModel:
    return read_policies(
        yaml.safe_dump({"version": 1, "row_policies": list(rules)}),
        compiled,
        groups=frozenset(),
        managed_tables={},
    )


def run(
    document: Document,
    sales: list[Sale],
    compiled: CompiledModel = COMPILED,
    verified: bool = True,
    catalog: dict[str, dict[str, SourceColumn]] | None = None,
    mappings: dict[str, list[tuple[str, ...]]] | None = None,
    **more: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    tables = catalog or CATALOG
    data = lake_rows(sales)
    datasets = compiled.ossie.document.get("datasets", ())
    names = [str(d["name"]) for d in (datasets if isinstance(datasets, tuple) else ())]
    with TemporaryDirectory() as folder:
        paths = []
        for name in names:
            path = Path(folder) / f"{name}.csv"
            columns = list(tables[name])
            lines = [",".join(columns)] + [
                ",".join(r.get(c) or "" for c in columns) for r in data[name]
            ]
            path.write_text("\n".join(lines) + "\n", "utf-8")
            paths.append(path)
        with ce.case_lake(paths, {n: tables[n] for n in names}) as lake:
            for table_name, values in (mappings or {}).items():
                with lake.transaction() as transaction:
                    schema, name_ = table_name.split(".")
                    transaction.execute(
                        StatementRequest(
                            kind="statement",
                            sql=f'CREATE SCHEMA IF NOT EXISTS "{schema}"',
                            parameters=[],
                        )
                    )
                    transaction.execute(
                        StatementRequest(
                            kind="statement",
                            sql=f'CREATE TABLE "{schema}"."{name_}" '
                            '("user_id" VARCHAR, "category" VARCHAR)',
                            parameters=[],
                        )
                    )
                    for row in values:
                        written = ", ".join(f"'{v}'" for v in row)
                        transaction.execute(
                            StatementRequest(
                                kind="statement",
                                sql=f'INSERT INTO "{schema}"."{name_}" VALUES ({written})',
                                parameters=[],
                            )
                        )
            return ask(document, compiled, lake, evidence=checked(compiled, lake, verified), **more)


def checked(compiled: CompiledModel, lake: Any, verified: bool = True) -> Any:  # noqa: ANN401
    """The records of the lake's snapshot, checked by P2-26's own statements; none when the
    test wants the tables unverified."""
    if not verified:
        return None
    snapshot = lake.current_snapshot().id
    records = verify(compiled, lake.read, snapshot, at=ce.INSTANT).records
    return lambda found: records if found["id"] == snapshot else ()


SAMPLE: list[Sale] = [
    ("2026-01-01", "A", "x", "a", 10000, 4),
    ("2026-01-01", "A", "y", "b", 5000, 1),
    ("2026-01-01", "B", "x", "a", 2000, 2),
    ("2026-02-01", "A", "x", None, 1000, 0),
]


def evidence(found: Any) -> dict[str, str]:  # noqa: ANN401
    return {o["obligation"]: o["evidence"] for o in found.receipt.document()["plan"]["obligations"]}


def receipt_reads(found: Any) -> list[str]:  # noqa: ANN401
    return [d["name"] for d in found.receipt.document()["data"]]


# --- eligible, used only when verified ---------------------------------------------------------


def test_an_eligible_table_is_read_once_verified_and_gives_the_base_answer() -> None:
    used = run(query("revenue", "price"), SAMPLE)
    base = run(query("revenue", "price"), SAMPLE, verified=False)
    assert (
        ce.rows_of(used)
        == ce.rows_of(base)
        == [
            ["2026-01-01", "A", "150.00", "30.00"],
            ["2026-01-01", "B", "20.00", "10.00"],
            ["2026-02-01", "A", "10.00", None],  # zero quantity: the ratio's declared NULL
        ]
    )
    assert receipt_reads(used) == ["rollup"]
    assert used.receipt.document()["execution"]["preaggregation"] == "rollup"
    assert evidence(used)["C8.equivalent:rollup"] == "rollup:rollup"
    assert receipt_reads(base) == ["sales"]
    assert evidence(base)["C8.equivalent:rollup"] == "base:sales; eligible, consistency unverified"
    assert '"sales"' not in used.physical.sql


def test_a_legal_roll_up_and_a_filter_on_the_grain_are_answered_from_the_table() -> None:
    document = query(
        "revenue",
        group_by=("sales.category",),
        filters=[
            {
                "scope": "board",
                "member": "sales.month",
                "predicate": {"op": "in", "values": [{"type": "date", "value": "2026-01-01"}]},
            }
        ],
    )
    used = run(document, SAMPLE)
    assert (
        ce.rows_of(used)
        == ce.rows_of(run(document, SAMPLE, verified=False))
        == [["A", "150.00"], ["B", "20.00"]]
    )
    assert receipt_reads(used) == ["rollup"]


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["2026-01-01", "2026-02-01"]),
            st.sampled_from(["A", "B", "C"]),
            st.sampled_from("xy"),
            st.sampled_from(["a", None]),
            st.integers(-99999, 99999),
            st.integers(0, 5),
        ),
        min_size=1,
        max_size=10,
    ),
    st.sampled_from([("sales.month", "sales.category"), ("sales.category",), ("sales.month",), ()]),
)
def test_the_table_s_answer_is_always_the_base_s(
    sales: list[Sale], group_by: tuple[str, ...]
) -> None:
    document = query("revenue", "quantity", "price", group_by=group_by)
    used = run(document, sales)
    assert receipt_reads(used) == ["rollup"]
    assert ce.rows_of(used) == ce.rows_of(run(document, sales, verified=False))


# --- not eligible, with its reason ------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        (query("revenue", group_by=("sales.tenant",)), "grain"),
        (query("revenue", group_by=("sales.day",)), "grain"),  # the field behind the month
        (
            query(
                "revenue",
                filters=[
                    {
                        "scope": "board",
                        "member": "sales.tenant",
                        "predicate": {"op": "in", "values": [{"type": "text", "value": "x"}]},
                    }
                ],
            ),
            "filter",
        ),
        (query("customers"), "distinct"),
    ],
)
def test_a_question_the_table_cannot_answer_reads_the_base(document: Document, reason: str) -> None:
    found = run(document, SAMPLE)
    assert receipt_reads(found) == ["sales"]
    assert evidence(found)["C8.equivalent:rollup"] == f"base:sales; not eligible: {reason}"


def test_a_ratio_without_both_stored_components_is_not_eligible() -> None:
    revenue_only = model(table("rollup", ["month", "category"], [("r", "SUM(revenue)", "revenue")]))
    found = run(query("price"), SAMPLE, compiled=revenue_only)
    assert receipt_reads(found) == ["sales"]
    assert evidence(found)["C8.equivalent:rollup"] == "base:sales; not eligible: ratio"


def test_a_table_that_does_not_declare_what_it_stores_is_never_a_candidate() -> None:
    undeclared = table(
        "rollup", ["month", "category"], [("r", "SUM(revenue)", "revenue")], declared=False
    )
    found = run(query("revenue"), SAMPLE, compiled=model(undeclared))
    assert receipt_reads(found) == ["sales"]
    assert "C8.equivalent:rollup" not in evidence(found)


# --- security: exact preservation ---------------------------------------------------------------

BY_TENANT = {
    "name": "own_tenant",
    "dataset": "sales",
    "kind": "predicate",
    "predicate": "tenant = $tenant",
}
BY_CATEGORY_POLICY = {
    "name": "own_category",
    "dataset": "sales",
    "kind": "predicate",
    "predicate": "category = $category",
}


def test_a_policy_on_the_grain_is_applied_to_the_table_s_rows() -> None:
    context = ce.viewer_context((), {"category": {"type": "text", "value": "A"}})
    found = run(
        query("revenue"), SAMPLE, policy=policies(COMPILED, BY_CATEGORY_POLICY), context=context
    )
    assert receipt_reads(found) == ["rollup"]
    assert ce.rows_of(found) == [["2026-01-01", "A", "150.00"], ["2026-02-01", "A", "10.00"]]
    assert '(SELECT * FROM "rollup" AS "#p" WHERE' in found.physical.sql


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["2026-01-01"]),
            st.sampled_from(["A", "B"]),
            st.sampled_from("xy"),
            st.none(),
            st.integers(1, 9999),
            st.integers(1, 3),
        ),
        min_size=1,
        max_size=8,
    )
)
def test_a_policy_that_could_split_a_group_keeps_the_table_unused(sales: list[Sale]) -> None:
    """A group of the table can mix two tenants, so a tenant policy cannot be decided per
    group: the table is not eligible, and the answer is the base's over authorized rows."""
    context = ce.viewer_context((), {"tenant": {"type": "text", "value": "x"}})
    found = run(query("revenue"), sales, policy=policies(COMPILED, BY_TENANT), context=context)
    assert receipt_reads(found) == ["sales"]
    assert evidence(found)["C8.equivalent:rollup"] == "base:sales; not eligible: policy"
    expected: dict[tuple[str, str], Decimal] = {}
    for m, c, t, _, cents, _ in sales:
        if t == "x":
            expected[(m, c)] = expected.get((m, c), Decimal(0)) + Decimal(cents) / 100
    assert ce.rows_of(found) == [[m, c, f"{v:.2f}"] for (m, c), v in sorted(expected.items())]


def test_a_policy_on_the_field_behind_a_grain_dimension_keeps_the_table_unused() -> None:
    by_day = {
        "name": "own_days",
        "dataset": "sales",
        "kind": "predicate",
        "predicate": "day = month",
    }
    found = run(query("revenue"), SAMPLE, policy=policies(COMPILED, by_day))
    assert evidence(found)["C8.equivalent:rollup"] == "base:sales; not eligible: policy"


# --- choice and evidence ------------------------------------------------------------------------


def test_the_smallest_eligible_grain_is_chosen_deterministically() -> None:
    both = model(MONTHLY, table("by_category", ["category"], [("r", "SUM(revenue)", "revenue")]))
    found = run(query("revenue", group_by=("sales.category",)), SAMPLE, compiled=both)
    assert receipt_reads(found) == ["by_category"]
    assert evidence(found)["C8.equivalent:by_category"] == "rollup:by_category"
    assert evidence(found)["C8.equivalent:rollup"] == "base:sales; eligible, consistency unverified"
    monthly = run(query("revenue"), SAMPLE, compiled=both)
    assert receipt_reads(monthly) == ["rollup"]  # by_category cannot keep the month


def test_a_plan_or_statement_that_bends_the_proof_never_runs() -> None:
    found = run(query("revenue"), SAMPLE)
    plan = found.plan
    rollup = plan.rollup
    assert rollup is not None
    narrowed = replace(rollup, fields=dict(list(rollup.fields.items())[:1]))
    for broken in (
        replace(plan, rollup=narrowed),
        replace(plan, rollup=replace(rollup, eligible=False)),
    ):
        with pytest.raises(DischargeError):
            verify_plan(broken)
    reading_base = found.physical.sql.replace('"rollup"', '"sales"')
    with pytest.raises(DischargeError):
        verify_physical(plan, replace(found.physical, sql=reading_base))


# --- after review: one field per dimension, the same values, every reason, every policy ---------


def plain(value: Any) -> Any:  # noqa: ANN401
    if hasattr(value, "items"):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value


def variant(
    change: Callable[[Document], None], catalog: dict[str, dict[str, SourceColumn]] | None = None
) -> CompiledModel:
    """The monthly model, its document changed by `change`, compiled on `catalog`."""
    document = plain(COMPILED.ossie.document)
    change(document)
    used = catalog or {"sales": SALES, "rollup": ROLLUP}
    loaded = read(json.dumps(document).encode(), "semantic/rollups.ossie.json", VENDORS)
    return compile_model(loaded, used, VENDORS)


def field_of(document: Document, dataset: str, name: str) -> Document:
    found: Document = next(
        f
        for d in document["datasets"]
        if d["name"] == dataset
        for f in d["fields"]
        if f["name"] == name
    )
    return found


def test_a_field_behind_a_conformed_dimension_never_passes_for_it() -> None:
    """`sales.day` declared of the `month` dimension too: grouping by it is not grouping by the
    table's month, so the table is not eligible, and the day's own rows are answered."""

    def day_is_month(document: Document) -> None:
        field_of(document, "sales", "day").update(conformed("day", "month"))

    compiled = variant(day_is_month)
    data: list[Sale] = [
        ("2026-01-01", "A", "x", None, 10000, 1),
        ("2026-01-01", "A", "x", None, 5000, 1),
    ]
    found = run(query("revenue", group_by=("sales.day", "sales.category")), data, compiled=compiled)
    assert receipt_reads(found) == ["sales"]
    assert evidence(found)["C8.equivalent:rollup"] == "base:sales; not eligible: grain"
    month = run(query("revenue"), data, compiled=compiled)
    assert evidence(month)["C8.equivalent:rollup"] == "base:sales; not eligible: grain"


@pytest.mark.parametrize(
    ("column", "eligible"),
    [
        (SourceColumn("DOUBLE"), False),  # an exact decimal never becomes a float
        (SourceColumn("DECIMAL(38,0)"), False),  # another scale is another value
        (SourceColumn("DECIMAL(38,2)"), True),
    ],
)
def test_a_stored_column_must_hold_the_metric_s_own_values(
    column: SourceColumn, eligible: bool
) -> None:
    catalog = {"sales": SALES, "rollup": ROLLUP | {"revenue": column}}
    compiled = variant(lambda document: None, catalog)
    found = run(query("revenue"), SAMPLE, compiled=compiled, catalog=catalog)
    assert receipt_reads(found) == (["rollup"] if eligible else ["sales"])
    if not eligible:
        assert evidence(found)["C8.equivalent:rollup"] == "base:sales; not eligible: metric"


def test_a_count_stored_as_an_integer_keeps_its_integer_type() -> None:
    def count(document: Document) -> None:
        document["metrics"].append(
            {"name": "sales_count", "expression": {"sql": "COUNT(sales.sale_id)"}}
        )
        payload = json.loads(document["datasets"][1]["custom_extensions"][0]["data"])
        payload["aggregate_table"]["measures"].append(
            {"name": "n", "expression": "SUM(sold)", "metric": "sales_count"}
        )
        document["datasets"][1]["custom_extensions"][0]["data"] = json.dumps(payload)
        document["datasets"][1]["fields"].append({"name": "sold", "expression": {"sql": "sold"}})

    catalog = {"sales": SALES, "rollup": ROLLUP | {"sold": SourceColumn("BIGINT")}}
    compiled = variant(count, catalog)
    used = run(query("sales_count"), SAMPLE, compiled=compiled, catalog=catalog)
    base = run(query("sales_count"), SAMPLE, compiled=compiled, catalog=catalog, verified=False)
    assert receipt_reads(used) == ["rollup"]
    assert (
        ce.rows_of(used)
        == ce.rows_of(base)
        == [["2026-01-01", "A", "2"], ["2026-01-01", "B", "1"], ["2026-02-01", "A", "1"]]
    )


def test_a_stored_column_in_another_currency_is_not_eligible() -> None:
    def currencies(document: Document) -> None:
        field_of(document, "sales", "amount")["custom_extensions"] = [
            extension({"element": "field", "semantic_type": "amount", "currency": "BOB"})
        ]
        field_of(document, "rollup", "revenue")["custom_extensions"] = [
            extension({"element": "field", "semantic_type": "amount", "currency": "USD"})
        ]

    found = run(query("revenue"), SAMPLE, compiled=variant(currencies))
    assert evidence(found)["C8.equivalent:rollup"] == "base:sales; not eligible: unit"


def test_a_period_end_balance_is_not_eligible() -> None:
    compiled, catalog = period.snapshots_model()
    document = plain(compiled.ossie.document)
    document["datasets"].append(
        {
            "name": "by_region",
            "source": "by_region",
            "primary_key": ["region"],
            "fields": [
                {"name": "region", "expression": {"sql": "region"}},
                {"name": "balance", "expression": {"sql": "balance"}},
            ],
            "custom_extensions": [
                extension(
                    {
                        "element": "dataset",
                        "entity": "region",
                        "grain": {"dimensions": ["region"]},
                        "aggregate_table": {
                            "base_dataset": "t",
                            "measures": [
                                {"name": "b", "expression": "SUM(balance)", "metric": "balance"}
                            ],
                        },
                    }
                )
            ],
        }
    )
    tables = catalog | {
        "by_region": {"region": SourceColumn("VARCHAR"), "balance": SourceColumn("DECIMAL(38,2)")}
    }
    with_table = compile_model(
        read(json.dumps(document).encode(), "semantic/s.ossie.json", VENDORS), tables, VENDORS
    )
    data = period.rows((1, "2026-01-31", "North", "100.00"))
    with tiny_lake(catalog, data) as lake:
        found = ask(
            period.query("balance", group_by=period.BY_MONTH),
            with_table,
            lake,
            evidence=checked(with_table, lake),
        )
    assert evidence(found)["C8.equivalent:by_region"] == "base:t; not eligible: period_end"


CATEGORY = {
    "name": "own_category",
    "dataset": "sales",
    "kind": "predicate",
    "predicate": "category = $category",
}


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.sampled_from(["2026-01-01", "2026-02-01"]),
            st.sampled_from(["A", "B", "C"]),
            st.sampled_from("xy"),
            st.none(),
            st.integers(-9999, 9999),
            st.integers(0, 3),
        ),
        min_size=1,
        max_size=10,
    ),
    st.sampled_from(["A", "B", "C"]),
    st.booleans(),
)
def test_a_grain_policy_keeps_the_table_and_never_widens_the_answer(
    sales: list[Sale], allowed: str, by_list: bool
) -> None:
    rule = CATEGORY if not by_list else CATEGORY | {"predicate": "category IN ($categories)"}
    value = {"type": "list", "values": [allowed]} if by_list else {"type": "text", "value": allowed}
    context = ce.viewer_context((), {"categories" if by_list else "category": value})
    document = query("revenue", "price")
    used = run(document, sales, policy=policies(COMPILED, rule), context=context)
    base = run(document, sales, policy=policies(COMPILED, rule), context=context, verified=False)
    assert receipt_reads(used) == ["rollup"]
    assert ce.rows_of(used) == ce.rows_of(base)
    assert all(row[1] == allowed for row in ce.rows_of(used))  # never another category


def test_a_mapping_table_policy_on_the_grain_keeps_the_table() -> None:
    rule = CATEGORY | {
        "predicate": "category IN (SELECT category FROM security.allowed WHERE user_id = $user_id)"
    }
    policy = read_policies(
        yaml.safe_dump({"version": 1, "row_policies": [rule]}),
        COMPILED,
        groups=frozenset(),
        managed_tables={"security.allowed": ("user_id", "category")},
    )
    context = ce.viewer_context((), {"user_id": {"type": "text", "value": "u"}}) | {"user": "u"}
    found = run(
        query("revenue"),
        SAMPLE,
        policy=policy,
        context=context,
        mappings={"security.allowed": [("u", "B")]},
    )
    assert receipt_reads(found) == ["rollup"]
    assert ce.rows_of(found) == [["2026-01-01", "B", "20.00"]]


def test_an_exempt_caller_and_a_missing_attribute_behave_as_with_the_base() -> None:
    exempt = CATEGORY | {"exempt_groups": ["auditors"]}
    policy = read_policies(
        yaml.safe_dump({"version": 1, "row_policies": [exempt]}),
        COMPILED,
        groups=frozenset({"auditors"}),
        managed_tables={},
    )
    auditor = run(
        query("revenue"), SAMPLE, policy=policy, context=ce.viewer_context(("auditors",), {})
    )
    assert receipt_reads(auditor) == ["rollup"]
    assert len(ce.rows_of(auditor)) == 3  # every group: the policy does not apply
    nobody = run(
        query("revenue"), SAMPLE, policy=policies(COMPILED, CATEGORY), context=ce.viewer_context()
    )
    assert ce.rows_of(nobody) == []  # no attribute: no rows, from the table as from the base


def test_a_table_field_the_caller_cannot_see_keeps_the_table_unused() -> None:
    masked = {"member": "rollup.category", "default": "masked", "mask": "#"}
    policy = read_policies(
        yaml.safe_dump({"version": 1, "column_policies": [masked]}),
        COMPILED,
        groups=frozenset(),
        managed_tables={},
    )
    found = run(query("revenue"), SAMPLE, policy=policy)
    assert receipt_reads(found) == ["sales"]
    assert evidence(found)["C8.equivalent:rollup"] == "base:sales; not eligible: policy"
