"""P2-15 (#327): relationship paths, roles and ambiguity rejection (C4)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.correctness import analyze
from orvex.semantic.correctness.graph import Graph
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import read_policies
from orvex.semantic.query.binding import Limits, bind_query
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.conformance.test_execution import ask, corpus_case, lake_for
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]


def model(
    tables: dict[str, list[str]], relationships: list[tuple[str, str, str, str, str]], metric: str
) -> tuple[CompiledModel, dict[str, dict[str, SourceColumn]]]:
    """Datasets `tables` (first column the key); relationships (name, from, to, from column,
    to column), all many-to-one."""
    types = {"amount": "DECIMAL(18,2)", "name": "VARCHAR", "country": "VARCHAR"}
    catalog = {
        t: {c: SourceColumn(types.get(c, "BIGINT"), c != "name" and c != "country") for c in cols}
        for t, cols in tables.items()
    }
    document = {
        "version": "0.2.0.dev0",
        "name": "paths",
        "datasets": [
            {
                "name": t,
                "source": t,
                "primary_key": [cols[0]],
                "fields": [{"name": c, "expression": {"sql": c}} for c in cols],
            }
            for t, cols in tables.items()
        ],
        "relationships": [
            {"name": n, "from": f, "to": t, "from_columns": [fc], "to_columns": [tc]}
            for n, f, t, fc, tc in relationships
        ],
        "metrics": [{"name": "total", "expression": {"sql": metric}}],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/paths.ossie.json", VENDORS)
    return compile_model(loaded, catalog, VENDORS), catalog


@contextmanager
def lake_of(catalog: dict[str, Any], data: dict[str, list[list[str]]]) -> Iterator[Lake]:
    with TemporaryDirectory() as folder:
        paths = []
        for table, rows in data.items():
            path = Path(folder) / f"{table}.csv"
            lines = [",".join(catalog[table])] + [",".join(r) for r in rows]
            path.write_text("\n".join(lines) + "\n", "utf-8")
            paths.append(path)
        with ce.case_lake(paths, catalog) as lake:
            yield lake


def query(*group_by: Document, **more: Any) -> Document:  # noqa: ANN401
    found: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "total"}],
    }
    if group_by:
        found["group_by"] = list(group_by)
    return found | more


def refusal(compiled: CompiledModel, document: Document, **policies: Any) -> SemanticError:  # noqa: ANN401
    policy = read_policies(
        yaml.safe_dump({"version": 1, **policies}), compiled, groups=frozenset(), managed_tables={}
    )

    def attempt() -> None:
        bound = bind_query(
            compiled,
            json.dumps(document),
            instant=datetime(2026, 11, 1, tzinfo=UTC),
            limits=Limits(1000),
            hidden=lambda m: False,
        )
        analyze(bound, compiled, policy, ce.viewer_context())

    with pytest.raises(SemanticError) as caught:
        attempt()
    return caught.value


# Two three-step paths from f to w: a1 a2 a3 (through x, z) and b1 b2 b3 (through y, v).
LONG = {
    "f": ["id", "x_id", "y_id", "amount"],
    "x": ["id", "z_id"],
    "z": ["id", "w_id"],
    "y": ["id", "v_id"],
    "v": ["id", "w_id"],
    "w": ["id", "name"],
}
LONG_RELATIONSHIPS = [
    ("a1", "f", "x", "x_id", "id"),
    ("a2", "x", "z", "z_id", "id"),
    ("a3", "z", "w", "w_id", "id"),
    ("b1", "f", "y", "y_id", "id"),
    ("b2", "y", "v", "v_id", "id"),
    ("b3", "v", "w", "w_id", "id"),
]
LONG_DATA = {
    "f": [["1", "1", "2", "10.00"], ["2", "1", "2", "5.00"]],
    "x": [["1", "1"]],
    "z": [["1", "1"]],
    "y": [["2", "2"]],
    "v": [["2", "2"]],
    "w": [["1", "via-a"], ["2", "via-b"]],
}


# --- the corpus -------------------------------------------------------------------------------


def test_case_016_names_both_roles_for_a_builder() -> None:
    compiled, _, _ = corpus_case("CASE-016")
    error = refusal(compiled, ce.QUERIES["CASE-016"])
    assert error.code == "AMBIGUOUS_RELATIONSHIP_PATH"
    builder = error.to_json(audience="builder")
    assert "billing_customer" in builder
    assert "shipping_customer" in builder


# --- the rule -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "name"), [("a1", "via-a"), ("a2", "via-a"), ("a3", "via-a"), ("b2", "via-b")]
)
def test_a_role_at_any_step_selects_its_path(role: str, name: str) -> None:
    compiled, catalog = model(LONG, LONG_RELATIONSHIPS, "SUM(f.amount)")
    with lake_of(catalog, LONG_DATA) as lake:
        found = ask(query({"member": "w.name", "role": role}), compiled, lake)
    assert ce.rows_of(found) == [[name, "15.00"]]


def test_no_role_with_two_paths_is_ambiguous() -> None:
    compiled, _ = model(LONG, LONG_RELATIONSHIPS, "SUM(f.amount)")
    error = refusal(compiled, query({"member": "w.name"}))
    assert error.code == "AMBIGUOUS_RELATIONSHIP_PATH"


def test_two_roles_side_by_side_multiply_nothing() -> None:
    tables = {"f": ["id", "bill_id", "ship_id", "amount"], "c": ["id", "country"]}
    rels = [("billing", "f", "c", "bill_id", "id"), ("shipping", "f", "c", "ship_id", "id")]
    compiled, catalog = model(tables, rels, "SUM(f.amount)")
    data = {
        "f": [["1", "1", "2", "10.00"], ["2", "1", "1", "5.00"], ["3", "2", "2", "1.00"]],
        "c": [["1", "BO"], ["2", "PE"]],
    }
    side_by_side = query(
        {"member": "c.country", "role": "billing"}, {"member": "c.country", "role": "shipping"}
    )
    with lake_of(catalog, data) as lake:
        found = ask(side_by_side, compiled, lake)
        alone = ask(query(), compiled, lake)
    names = [c["name"] for c in found.result.envelope()["columns"]]
    assert names == ["country_billing", "country_shipping", "total"]
    counted, _ = model(tables, rels, "COUNT(f.id)")
    with lake_of(catalog, data) as lake:
        rows_seen = ce.rows_of(ask(side_by_side, counted, lake))
    assert sum(int(str(r[2])) for r in rows_seen) == 3  # the fact's own row count
    assert ce.rows_of(found) == [["BO", "BO", "5.00"], ["BO", "PE", "10.00"], ["PE", "PE", "1.00"]]
    total = sum(float(str(r[2])) for r in ce.rows_of(found))
    assert f"{total:.2f}" == ce.rows_of(alone)[0][0] == "16.00"  # no row was repeated


def test_misused_roles_are_refused() -> None:
    compiled, _ = model(LONG, LONG_RELATIONSHIPS, "SUM(f.amount)")
    # b1 reaches x only through one-to-many steps: refused as fan-out, never another path.
    fanning = refusal(compiled, query({"member": "x.id", "role": "b1"}))
    assert (fanning.code, dict(fanning.details)) == (
        "UNSUPPORTED_OPERATION",
        {"operation": "fanout"},
    )
    on_home = refusal(compiled, query({"member": "f.amount", "role": "a1"}))
    assert on_home.code == "QUERY_INVALID"
    assert dict(on_home.details)["problems"] == [{"pointer": "/group_by/0/role", "rule": "role"}]
    missing = refusal(compiled, query({"member": "w.name", "role": "nowhere"}))
    assert missing.code == "MEMBER_NOT_FOUND"


def test_a_role_shared_by_two_paths_still_leaves_them_ambiguous() -> None:
    tables = {
        "f": ["id", "x_id", "y_id", "amount"],
        "x": ["id", "w_id"],
        "y": ["id", "w_id"],
        "w": ["id", "name"],
    }
    rels = [("p", "f", "x", "x_id", "id"), ("q", "f", "y", "y_id", "id")]
    rels += [("to_w_from_x", "x", "w", "w_id", "id"), ("to_w_from_y", "y", "w", "w_id", "id")]
    rels.insert(0, ("m", "f", "m_", "id", "id"))
    tables["m_"] = ["id"]
    compiled, _ = model(tables, rels, "SUM(f.amount)")
    assert refusal(compiled, query({"member": "w.name"})).code == "AMBIGUOUS_RELATIONSHIP_PATH"
    # `p` is on one path only and selects it; neither path crosses `m`, which matches none.
    for role in ("m",):
        error = refusal(compiled, query({"member": "w.name", "role": role}))
        assert error.code == "AMBIGUOUS_RELATIONSHIP_PATH"
        assert len(cast(list[str], error.details["roles"])) >= 2


# --- cycles and self-relationships ---------------------------------------------------------


def test_a_self_relationship_is_a_one_step_path_and_never_loops() -> None:
    tables = {"e": ["id", "manager_id", "name"]}
    rels = [("manager", "e", "e", "manager_id", "id")]
    compiled, catalog = model(tables, rels, "COUNT(e.id)")
    tables_data = {"e": [["1", "1", "boss"], ["2", "1", "ana"], ["3", "1", "bo"]]}
    with lake_of(catalog, tables_data) as lake:
        own = ce.rows_of(ask(query({"member": "e.name"}), compiled, lake))
        managers = ce.rows_of(ask(query({"member": "e.name", "role": "manager"}), compiled, lake))
    assert own == [["ana", "1"], ["bo", "1"], ["boss", "1"]]
    assert managers == [["boss", "3"]]


def test_a_cycle_gives_a_finite_documented_answer() -> None:
    tables = {"f": ["id", "a_id", "amount"], "a": ["id", "b_id"], "b": ["id", "a_id"]}
    rels = [
        ("fa", "f", "a", "a_id", "id"),
        ("ab", "a", "b", "b_id", "id"),
        ("ba", "b", "a", "a_id", "id"),
    ]
    compiled, _ = model(tables, rels, "SUM(f.amount)")
    graph = Graph(compiled)
    paths = [p.relationships for p in graph.paths("F", "B")]
    # Each relationship at most once and each dataset once: two paths, the second through `ba`
    # walked from its one side (one-to-many), so only the first is many-to-one.
    assert paths == [("FA", "AB"), ("FA", "BA")]
    assert analyze(
        bind_query(
            compiled,
            json.dumps(query({"member": "b.id"})),
            instant=datetime(2026, 11, 1, tzinfo=UTC),
            limits=Limits(1000),
            hidden=lambda m: False,
        ),
        compiled,
        read_policies("version: 1\n", compiled, groups=frozenset(), managed_tables={}),
        ce.viewer_context(),
    ).obligations


# --- policies -------------------------------------------------------------------------------


def test_a_role_never_avoids_a_row_policy() -> None:
    compiled, _, _ = corpus_case("CASE-016")
    policy = read_policies(
        yaml.safe_dump(
            {
                "version": 1,
                "row_policies": [
                    {
                        "name": "own_country",
                        "dataset": "customer",
                        "kind": "predicate",
                        "predicate": "country = $country",
                    }
                ],
            }
        ),
        compiled,
        groups=frozenset(),
        managed_tables={},
    )
    # P2-21 (#341): the order is visible only when the customer on every role path passes:
    # its billing customer is in BO and its shipping customer in PE, so no country sees it.
    for country in ("BO", "PE"):
        caller = ce.viewer_context(attributes={"country": {"type": "text", "value": country}})
        for role in ("billing_customer", "shipping_customer"):
            document = ce.QUERIES["CASE-016"] | {
                "group_by": [{"member": "customer.country", "role": role}]
            }
            with lake_for("CASE-016") as lake:
                found = ask(document, compiled, lake, policy=policy, context=caller)
            assert ce.rows_of(found) == []
    # Named to the billing role only, the policy no longer restricts the shipping path.
    billing = read_policies(
        yaml.safe_dump(
            {
                "version": 1,
                "row_policies": [
                    {
                        "name": "own_country",
                        "dataset": "customer",
                        "kind": "predicate",
                        "predicate": "country = $country",
                        "roles": ["billing_customer"],
                    }
                ],
            }
        ),
        compiled,
        groups=frozenset(),
        managed_tables={},
    )
    caller = ce.viewer_context(attributes={"country": {"type": "text", "value": "BO"}})
    document = ce.QUERIES["CASE-016"] | {
        "group_by": [{"member": "customer.country", "role": "billing_customer"}]
    }
    with lake_for("CASE-016") as lake:
        found = ask(document, compiled, lake, policy=billing, context=caller)
    assert ce.rows_of(found) == [["BO", "100.00"]]


# --- receipts -------------------------------------------------------------------------------


def test_the_receipt_shows_a_role_only_with_its_member() -> None:
    from tests.support.receipts import SampleViewer

    compiled, _, _ = corpus_case("CASE-016")
    document = ce.QUERIES["CASE-016"] | {
        "group_by": [{"member": "customer.country", "role": "billing_customer"}]
    }
    with lake_for("CASE-016") as lake:
        found = ask(document, compiled, lake)
    sees = SampleViewer(members=frozenset({"revenue", "customer.country"}))
    blind = SampleViewer(members=frozenset({"revenue"}))
    assert found.receipt.view(sees)["question"]["group_by"] == [
        {"member": "customer.country", "role": "billing_customer"}
    ]
    assert "billing_customer" not in json.dumps(found.receipt.view(blind))


# --- property ---------------------------------------------------------------------------------


def forward_paths(edges: list[tuple[str, str]], start: str, end: str) -> int:
    """Simple paths along many-to-one steps, counted by hand."""
    count = 0

    def walk(at: str, seen: set[str]) -> None:
        nonlocal count
        if at == end:
            count += 1
            return
        for a, b in edges:
            if a == at and b not in seen:
                walk(b, seen | {b})

    walk(start, {start})
    return count


@settings(max_examples=30, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(0, 3), st.integers(0, 3)).filter(lambda e: e[0] != e[1]), max_size=6
    ),
    st.integers(1, 3),
)
def test_a_member_is_accepted_exactly_when_one_many_to_one_path_exists(
    edges: list[tuple[int, int]], target: int
) -> None:
    tables = {
        f"d{i}": ["id", *[f"fk{n}" for n, (a, _) in enumerate(edges) if a == i], "name"]
        for i in range(4)
    }
    tables["d0"].insert(1, "amount")
    rels = [(f"r{n}", f"d{a}", f"d{b}", f"fk{n}", "id") for n, (a, b) in enumerate(edges)]
    compiled, _ = model(tables, rels, "SUM(d0.amount)")
    expected = forward_paths([(f"d{a}", f"d{b}") for a, b in edges], "d0", f"d{target}")
    policy = read_policies("version: 1\n", compiled, groups=frozenset(), managed_tables={})
    bound = bind_query(
        compiled,
        json.dumps(query({"member": f"d{target}.name"})),
        instant=datetime(2026, 11, 1, tzinfo=UTC),
        limits=Limits(1000),
        hidden=lambda m: False,
    )
    try:
        analyze(bound, compiled, policy, ce.viewer_context())
        accepted = True
    except SemanticError:
        accepted = False
    assert accepted == (expected == 1)


def test_a_row_policy_on_an_intermediate_dataset_restricts_through_any_role() -> None:
    compiled, catalog = model(LONG, LONG_RELATIONSHIPS, "SUM(f.amount)")
    policy = read_policies(
        yaml.safe_dump(
            {
                "version": 1,
                "row_policies": [
                    {"name": "own_z", "dataset": "z", "kind": "predicate", "predicate": "w_id = $w"}
                ],
            }
        ),
        compiled,
        groups=frozenset(),
        managed_tables={},
    )
    with lake_of(catalog, LONG_DATA) as lake:
        for role in ("a1", "a2"):  # both reach w through z, where the policy is
            for w, expected in (("2", []), ("1", [["via-a", "15.00"]])):
                caller = ce.viewer_context(attributes={"w": {"type": "integer", "value": w}})
                found = ask(
                    query({"member": "w.name", "role": role}),
                    compiled,
                    lake,
                    policy=policy,
                    context=caller,
                )
                assert ce.rows_of(found) == expected
