"""#441 AC8 (#417, Reviewer note on #413): a read's CTE names are resolved by scope, in the
managed lake's guard (`read_tree.pin_tables`) and the existing lake's (`existing_lake_guard`).
A name matching a CTE outside that CTE's scope is never taken for it: it is a lake table, and
pinned, or it is refused with the guard's own code. Before, it was admitted unpinned and failed
only because DuckDB then found no such table."""

from __future__ import annotations

import json
from typing import Any

import pytest
from orvex.store.errors import StoreError
from orvex.store.existing_lake_guard import guarded
from orvex.store.read_tree import _read_tree, _walk, pin_tables

TABLES = {("main", "readings"), ("main", "meters")}


def pinned(sql: str) -> list[tuple[str, bool]]:
    """Each base-table reference of `sql` once pinned by the managed guard: (name, pinned)."""
    tree = _read_tree(sql)
    pin_tables(tree, 7, set(TABLES))
    return _references(tree)


def _references(tree: dict[str, Any]) -> list[tuple[str, bool]]:
    return [
        (str(n["table_name"]), n.get("at_clause") is not None)
        for n in _walk(tree)
        if n.get("type") == "BASE_TABLE"
    ]


def existing(sql: str) -> list[tuple[str, bool]]:
    tree, _ = guarded(sql, [], tables=TABLES, snapshot=7, table_functions=frozenset())
    return _references(tree)


GUARDS = [pinned, existing]


@pytest.mark.parametrize("guard", GUARDS, ids=["managed", "existing"])
def test_a_cte_in_scope_is_the_cte(guard: Any) -> None:  # noqa: ANN401
    found = guard("WITH r AS (SELECT * FROM readings) SELECT * FROM r")
    assert ("readings", True) in found


@pytest.mark.parametrize("guard", GUARDS, ids=["managed", "existing"])
def test_a_name_outside_its_ctes_scope_is_refused_when_no_table_has_it(guard: Any) -> None:  # noqa: ANN401
    # `r` is defined only inside the subquery: the outer `FROM r` is not that CTE.
    sql = "SELECT * FROM (WITH r AS (SELECT * FROM readings) SELECT * FROM r) AS a, r"
    with pytest.raises(StoreError) as caught:
        guard(sql)
    assert caught.value.code == "STORE_STATEMENT_INVALID"
    assert caught.value.details == {"table": "r"}


@pytest.mark.parametrize("guard", GUARDS, ids=["managed", "existing"])
def test_a_name_outside_its_ctes_scope_that_is_a_table_is_the_table_pinned(guard: Any) -> None:  # noqa: ANN401
    sql = (
        "SELECT * FROM (WITH meters AS (SELECT * FROM readings) SELECT * FROM meters) AS a, meters"
    )
    found = guard(sql)
    # The outer `meters` is the table, pinned (the managed guard pins the CTE's use too).
    assert ("meters", True) in found
    assert ("readings", True) in found


@pytest.mark.parametrize("guard", GUARDS, ids=["managed", "existing"])
def test_a_cte_never_sees_a_later_one_of_its_with(guard: Any) -> None:  # noqa: ANN401
    sql = "WITH a AS (SELECT * FROM b), b AS (SELECT * FROM readings) SELECT * FROM a"
    with pytest.raises(StoreError) as caught:
        guard(sql)
    assert caught.value.details == {"table": "b"}


@pytest.mark.parametrize("guard", GUARDS, ids=["managed", "existing"])
def test_a_recursive_cte_sees_itself_and_a_plain_one_does_not(guard: Any) -> None:  # noqa: ANN401
    recursive = (
        "WITH RECURSIVE n AS (SELECT 1 AS x UNION ALL SELECT x + 1 FROM n WHERE x < 3) "
        "SELECT * FROM n"
    )
    assert guard(recursive)  # admitted
    with pytest.raises(StoreError) as caught:
        guard("WITH n AS (SELECT * FROM n) SELECT * FROM n")
    assert caught.value.details == {"table": "n"}


def test_the_tree_is_still_json() -> None:
    tree = _read_tree("WITH r AS (SELECT * FROM readings) SELECT * FROM r")
    pin_tables(tree, 7, set(TABLES))
    json.dumps(tree)
