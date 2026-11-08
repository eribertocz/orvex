"""#354: an aggregate table answers under a row policy reached by propagation only when the
base's columns of the policy's first link are grain fields the table keeps. Every group then
links to one related row, which decides the whole group; any other propagated policy keeps the
table unused, and the base is read with the policy."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.correctness.discharge import DischargeError, verify_plan
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies

from conformance.runners import corpus_execution as ce
from tests.conformance import test_materialization as m
from tests.conformance.test_execution import ask
from tests.support.policies import VENDORS

Document = dict[str, Any]
Sale = m.Sale

CATEGORIES = {"category": SourceColumn("VARCHAR", False), "owner": SourceColumn("VARCHAR")}
BUYERS = {"customer": SourceColumn("VARCHAR", False), "region": SourceColumn("VARCHAR")}
CATALOG = {**m.CATALOG, "categories": CATEGORIES, "buyers": BUYERS}
OWNERS = {"A": "u1", "B": "u2"}  # category C has no row: an orphan
REGIONS = {"a": "north", "b": "south"}


def _dataset(name: str, columns: dict[str, SourceColumn], key: str) -> Document:
    return {
        "name": name,
        "source": name,
        "primary_key": [key],
        "fields": [{"name": c, "expression": {"sql": c}} for c in columns],
    }


def _relationship(name: str, column: str, target: str, key: str) -> Document:
    return {
        "name": name,
        "from": "sales",
        "to": target,
        "from_columns": [column],
        "to_columns": [key],
    }


def _model(*relationships: Document) -> CompiledModel:
    document = m.plain(m.COMPILED.ossie.document)
    document["datasets"] += [
        _dataset("categories", CATEGORIES, "category"),
        _dataset("buyers", BUYERS, "customer"),
    ]
    document["relationships"] = list(relationships)
    loaded = read(json.dumps(document).encode(), "semantic/rollups.ossie.json", VENDORS)
    return compile_model(loaded, CATALOG, VENDORS)


BY_CATEGORY = _relationship("sales_category", "category", "categories", "category")
BY_BUYER = _relationship("sales_buyer", "customer", "buyers", "customer")
# A second path to `categories`, by a column the table does not keep: a role path.
BY_TENANT = _relationship("sales_tenant", "tenant", "categories", "category")
COMPILED = _model(BY_CATEGORY, BY_BUYER)
ROLES = _model(BY_CATEGORY, BY_TENANT, BY_BUYER)

OWNED = {
    "name": "own_categories",
    "dataset": "categories",
    "kind": "predicate",
    "predicate": "owner = $user_id",
}
IN_REGION = {
    "name": "own_region",
    "dataset": "buyers",
    "kind": "predicate",
    "predicate": "region = $region",
}


def _policies(compiled: CompiledModel, *rules: Document, managed: Any = None) -> PolicyModel:  # noqa: ANN401
    return read_policies(
        yaml.safe_dump({"version": 1, "row_policies": list(rules)}),
        compiled,
        groups=frozenset(),
        managed_tables=managed or {},
    )


def _context(**attributes: str) -> dict[str, Any]:
    return ce.viewer_context((), {k: {"type": "text", "value": v} for k, v in attributes.items()})


def _run(
    sales: list[Sale],
    compiled: CompiledModel,
    policy: PolicyModel,
    context: dict[str, Any],
    verified: bool = True,
    mappings: dict[str, list[tuple[str, ...]]] | None = None,
    group_by: tuple[str, ...] = ("sales.month", "sales.category"),
) -> Any:  # noqa: ANN401
    """The question `revenue` by `group_by`, on a lake holding the sales, the rollup, the
    categories and the buyers."""
    data = m.lake_rows(sales)
    data["categories"] = [{"category": c, "owner": o} for c, o in OWNERS.items()]
    data["buyers"] = [{"customer": c, "region": r} for c, r in REGIONS.items()]
    names = ["sales", "rollup", "by_category", "categories", "buyers"]
    with TemporaryDirectory() as folder:
        paths = []
        for name in names:
            path = Path(folder) / f"{name}.csv"
            columns = list(CATALOG[name])
            lines = [",".join(columns)] + [
                ",".join(r.get(c) or "" for c in columns) for r in data[name]
            ]
            path.write_text("\n".join(lines) + "\n", "utf-8")
            paths.append(path)
        with ce.case_lake(paths, {n: CATALOG[n] for n in names}) as lake:
            m.add_mappings(lake, mappings or {})
            return ask(
                m.query("revenue", group_by=group_by),
                compiled,
                lake,
                evidence=m.checked(compiled, lake, verified),
                policy=policy,
                context=context,
            )


def _expected(sales: list[Sale], allowed: Any) -> list[list[str]]:  # noqa: ANN401
    totals: dict[tuple[str, str], Decimal] = {}
    for month, category, tenant, customer, cents, _ in sales:
        if allowed(category, tenant, customer):
            key = (month, category)
            totals[key] = totals.get(key, Decimal(0)) + Decimal(cents) / 100
    return [[mo, c, f"{v:.2f}"] for (mo, c), v in sorted(totals.items())]


SALES = st.lists(
    st.tuples(
        st.sampled_from(["2026-01-01", "2026-02-01"]),
        st.sampled_from(["A", "B", "C"]),
        st.sampled_from(["A", "B"]),
        st.sampled_from(["a", "b", None]),
        st.integers(-9999, 9999),
        st.integers(0, 3),
    ),
    min_size=1,
    max_size=10,
)
FEW = settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@FEW
@given(SALES, st.sampled_from(["u1", "u2", "nobody"]), st.booleans())
def test_a_policy_propagated_along_a_grain_key_is_applied_to_the_table(
    sales: list[Sale], user: str, visible: bool
) -> None:
    rule = OWNED | ({"orphans": "visible"} if visible else {})
    policy, context = _policies(COMPILED, rule), _context(user_id=user)
    used = _run(sales, COMPILED, policy, context)
    base = _run(sales, COMPILED, policy, context, verified=False)
    assert m.receipt_reads(used) == ["rollup"]
    assert '"categories" AS "#c1"' in used.physical.sql  # the related row, inside the relation
    assert ce.rows_of(used) == ce.rows_of(base)

    def allowed(category: str, _tenant: str, _customer: str | None) -> bool:
        owner = OWNERS.get(category)
        return owner == user or (owner is None and visible)

    assert ce.rows_of(used) == _expected(sales, allowed)  # never a row the policy removes


@FEW
@given(SALES, st.sampled_from(["north", "south"]))
def test_a_policy_propagated_along_any_other_key_keeps_the_table_unused(
    sales: list[Sale], region: str
) -> None:
    found = _run(sales, COMPILED, _policies(COMPILED, IN_REGION), _context(region=region))
    assert "rollup" not in m.receipt_reads(found)
    assert m.evidence(found)["C8.equivalent:rollup"] == "base:sales; not eligible: policy"

    def allowed(_category: str, _tenant: str, customer: str | None) -> bool:
        return customer is not None and REGIONS[customer] == region

    assert ce.rows_of(found) == _expected(sales, allowed)


def test_a_role_path_through_a_kept_key_keeps_the_table_and_the_other_role_does_not() -> None:
    sales = m.SAMPLE
    by_role = _policies(ROLES, OWNED | {"roles": ["sales_category"]})
    used = _run(sales, ROLES, by_role, _context(user_id="u1"))
    assert m.receipt_reads(used)[0] == "rollup"
    both = _run(sales, ROLES, _policies(ROLES, OWNED), _context(user_id="u1"))
    assert m.evidence(both)["C8.equivalent:rollup"] == "base:sales; not eligible: policy"
    by_tenant = _run(
        sales, ROLES, _policies(ROLES, OWNED | {"roles": ["sales_tenant"]}), _context(user_id="u1")
    )
    assert m.evidence(by_tenant)["C8.equivalent:rollup"] == "base:sales; not eligible: policy"


def test_a_mapping_table_on_the_related_dataset_keeps_the_table() -> None:
    rule = {
        "name": "listed_categories",
        "dataset": "categories",
        "kind": "predicate",
        "predicate": "owner IN (SELECT category FROM security.allowed WHERE user_id = $user_id)",
    }
    policy = _policies(COMPILED, rule, managed={"security.allowed": ("user_id", "category")})
    found = _run(
        m.SAMPLE,
        COMPILED,
        policy,
        _context(user_id="u") | {"user": "u"},
        mappings={"security.allowed": [("u", "u2")]},
    )
    assert m.receipt_reads(found)[0] == "rollup"
    assert ce.rows_of(found) == [["2026-01-01", "B", "20.00"]]


def test_a_plan_whose_table_does_not_keep_the_link_never_runs() -> None:
    # By month only: the category is kept for the policy's link, not as a group, so only the
    # link check stands between the narrowed table and a group the policy would split.
    found = _run(
        m.SAMPLE,
        COMPILED,
        _policies(COMPILED, OWNED),
        _context(user_id="u1"),
        group_by=("sales.month",),
    )
    plan = found.plan
    rollup = plan.rollup
    assert rollup is not None
    assert ce.rows_of(found) == [["2026-01-01", "150.00"], ["2026-02-01", "10.00"]]
    narrowed = replace(
        rollup, fields={k: v for k, v in rollup.fields.items() if k.name.lower() != "category"}
    )
    report = replace(
        plan.report,
        materials=tuple(narrowed if c is rollup else c for c in plan.report.materials),
    )
    with pytest.raises(DischargeError, match="link of a propagated policy"):
        verify_plan(replace(plan, rollup=narrowed, report=report))


# --- after review: a path back to the base ------------------------------------------------------

BY_TENANT_GRAIN = {"tenant": SourceColumn("VARCHAR", False)} | m.ROLLUP


@pytest.mark.parametrize("to", ["category", "customer"])
def test_a_path_that_reads_the_base_again_keeps_the_table_unused(
    to: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-relationship (`sales.tenant` to another sales column) propagates a policy on
    `sales` back onto `sales`: its later step reads the base's rows, which the table does not
    replace, so the table is not eligible and the question is answered from the base."""
    monkeypatch.setitem(m.CATALOG, "rollup3", BY_TENANT_GRAIN)
    rollup = m.table("rollup3", ["month", "category", "tenant"], [("r", "SUM(revenue)", "revenue")])
    document = m.plain(m.model(rollup).ossie.document)
    for field in document["datasets"][0]["fields"]:
        if field["name"] == "tenant":
            field.update(m.conformed("tenant", "tenant"))
    document["relationships"] = [
        {
            "name": "parent",
            "from": "sales",
            "to": "sales",
            "from_columns": ["tenant"],
            "to_columns": [to],
        }
    ]
    loaded = read(json.dumps(document).encode(), "semantic/rollups.ossie.json", VENDORS)
    compiled = compile_model(loaded, {"sales": m.SALES, "rollup3": BY_TENANT_GRAIN}, VENDORS)
    original = m.lake_rows

    def with_tenants(sales: list[Sale]) -> dict[str, list[dict[str, str | None]]]:
        sums: dict[tuple[str, str, str], Decimal] = {}
        for month, category, tenant, _, cents, _ in sales:
            key = (month, category, tenant)
            sums[key] = sums.get(key, Decimal(0)) + Decimal(cents) / 100
        rows: list[dict[str, str | None]] = [
            {"month": k[0], "category": k[1], "tenant": k[2], "revenue": f"{v:.2f}"}
            for k, v in sorted(sums.items())
        ]
        return original(sales) | {"rollup3": rows}

    monkeypatch.setattr(m, "lake_rows", with_tenants)
    policy = _policies(compiled, m.BY_CATEGORY_POLICY)
    context = ce.viewer_context((), {"category": {"type": "text", "value": "A"}})
    found = m.run(
        m.query("revenue"),
        m.SAMPLE,
        compiled=compiled,
        catalog={"sales": m.SALES, "rollup3": BY_TENANT_GRAIN},
        policy=policy,
        context=context,
    )
    assert m.receipt_reads(found) == ["sales"]
    assert m.evidence(found)["C8.equivalent:rollup3"] == "base:sales; not eligible: policy"
