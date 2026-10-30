"""Model dependencies, run order and the run plan (P1-09).

References are read with DuckDB's own parser (`json_serialize_sql`); nothing is bound or run.
`view` models are not stored: their SQL is inlined as a subquery wherever another model reads
them, so every model computes them from the same snapshot (the store refuses views, because a
read could not pin the tables inside them, ADR-0025).
"""

from __future__ import annotations

import copy
import heapq
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import duckdb
from orvex.contracts.model_config.v1 import ModelError, Relationship
from orvex.contracts.store.v1 import StatementRequest
from orvex.transform import extraction, incremental, model_tests, reconciliation
from orvex.transform.clickhouse import Column
from orvex.transform.extraction import Extract
from orvex.transform.incremental import Columns, Step
from orvex.transform.model_tests import ModelTest
from orvex.transform.models import Model, problem
from orvex.transform.reconciliation import Comparison, Reconcile

LAKE_CATALOG = "lake"

type Tree = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Graph:
    """Models in run order (dependencies first, ties by name), and their direct dependencies."""

    order: tuple[str, ...]
    dependencies: Mapping[str, tuple[str, ...]]
    sources: Mapping[str, tuple[str, ...]]
    problems: tuple[ModelError, ...]


def _parser() -> duckdb.DuckDBPyConnection:
    """A connection that can only parse: no files, no extensions."""
    connection = duckdb.connect()
    connection.execute("SET enable_external_access = false")
    connection.execute("SET autoload_known_extensions = false")
    return connection


def _tree(connection: duckdb.DuckDBPyConnection, sql: str) -> Tree:
    row = connection.execute("SELECT json_serialize_sql($sql)", {"sql": sql}).fetchone()
    tree: Tree = json.loads(str(row[0])) if row else {}
    return tree


def _walk(node: object) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _cte_names(tree: object) -> set[str]:
    names: set[str] = set()
    for node in _walk(tree):
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map", []):
                if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                    names.add(entry["key"].lower())
    return names


def _path(node: dict[str, Any]) -> list[str]:
    qualified = node.get("qualified_name")
    raw = qualified.get("path") if isinstance(qualified, dict) else None
    if isinstance(raw, list) and raw:
        return [str(part).lower() for part in raw]
    parts = [node.get("catalog_name"), node.get("schema_name"), node.get("table_name")]
    return [str(part).lower() for part in parts if part]


def _line(sql: str, location: object) -> int | None:
    if not isinstance(location, int) or location < 0 or location > len(sql):
        return None
    return sql.count("\n", 0, location) + 1


def build_graph(models: Sequence[Model], sources: set[str]) -> Graph:
    """Dependencies of every DuckDB-SQL model; unknown references and cycles are problems."""
    by_name = {model.name: model for model in models}
    dependencies: dict[str, tuple[str, ...]] = {}
    used_sources: dict[str, tuple[str, ...]] = {}
    problems: list[ModelError] = []
    connection = _parser()
    try:
        for model in models:
            if model.extraction:
                dependencies[model.name] = ()
                used_sources[model.name] = (str(model.config.connection),)
                continue
            tree = _tree(connection, model.sql)
            ctes = _cte_names(tree)
            needs: set[str] = set()
            reads: set[str] = set()
            for node in _walk(tree):
                if node.get("type") != "BASE_TABLE":
                    continue
                parts = _path(node)
                if len(parts) == 3 and parts[0] == LAKE_CATALOG:
                    parts = parts[1:]
                if len(parts) == 1 and parts[0] in ctes:
                    continue
                if len(parts) >= 2 and parts[0] in sources:
                    reads.add(parts[0])
                    continue
                name = ".".join(parts)
                if len(parts) == 2 and name in by_name:
                    needs.add(name)
                    continue
                problems.append(
                    problem(
                        "MODEL_REFERENCE_UNKNOWN",
                        f"{model.sql_file}: '{'.'.join(_path(node))}' is neither a model "
                        "(<folder>.<name>) nor a source (<connection>.<table>).",
                        model.sql_file,
                        model=model.name,
                        line=_line(model.sql, node.get("query_location")),
                    )
                )
            # A relationship test reads the model it names: that model runs first. A test naming
            # its own model (a parent column in the same table) adds no edge.
            for index, test in enumerate(model.config.tests or []):
                related = getattr(test.root, "relationship", None)
                if not isinstance(related, Relationship) or related.to == model.name:
                    continue
                if related.to in by_name:
                    needs.add(related.to)
                    continue
                file = model.config_file or model.sql_file
                problems.append(
                    problem(
                        "MODEL_REFERENCE_UNKNOWN",
                        f"{file}: /tests/{index}/relationship/to: {related.to} is not a model "
                        "of the project.",
                        file,
                        model=model.name,
                        path=f"/tests/{index}/relationship/to",
                    )
                )
            # A model reading itself stays an edge: it is a cycle of one, reported below (each
            # run would otherwise read the table the previous run left).
            dependencies[model.name] = tuple(sorted(needs))
            used_sources[model.name] = tuple(sorted(reads))
    finally:
        connection.close()
    order, cycles = _order(dependencies)
    for cycle in cycles:
        first = by_name[cycle[0]]
        problems.append(
            problem(
                "DEPENDENCY_CYCLE",
                f"Models depend on each other in a cycle: {' → '.join([*cycle, cycle[0]])}.",
                first.sql_file,
                model=first.name,
            )
        )
    return Graph(tuple(order), dependencies, used_sources, tuple(problems))


def _order(
    dependencies: Mapping[str, tuple[str, ...]],
) -> tuple[list[str], list[list[str]]]:
    """Topological order taking, at each step, the first ready model by name; or the cycles."""
    waiting = {name: set(needs) & set(dependencies) for name, needs in dependencies.items()}
    users: dict[str, set[str]] = {name: set() for name in dependencies}
    for name, needs in waiting.items():
        for needed in needs:
            users[needed].add(name)
    ready = [name for name, needs in waiting.items() if not needs]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        name = heapq.heappop(ready)
        order.append(name)
        for user in sorted(users[name]):
            waiting[user].discard(name)
            if not waiting[user]:
                heapq.heappush(ready, user)
    if len(order) == len(dependencies):
        return order, []
    return [], _cycles({name: set(needs) for name, needs in waiting.items() if needs})


def _cycles(remaining: dict[str, set[str]]) -> list[list[str]]:
    """Every independent cycle among models that never became ready (each from its first
    model by name); models that only wait on a cycle are not reported."""
    found: list[list[str]] = []
    while True:
        # Drop models whose remaining needs are all gone: they wait on a cycle, not in one.
        changed = True
        while changed:
            changed = False
            for name in [n for n, needs in remaining.items() if not needs & remaining.keys()]:
                del remaining[name]
                changed = True
        if not remaining:
            return found
        path: list[str] = []
        name = min(remaining)
        while name not in path:
            path.append(name)
            name = min(remaining[name] & remaining.keys())
        cycle = path[path.index(name) :]
        found.append(cycle)
        for member in cycle:
            del remaining[member]


def compiled_sql(model: Model, models: Mapping[str, Model], sql: str | None = None) -> str:
    """The model's SQL (or `sql`, its rewritten form) with every `view` model it reads inlined
    as a subquery (recursively)."""
    connection = _parser()
    try:
        tree = _inline(connection, sql or model.sql, models, frozenset({model.name}))
        row = connection.execute(
            "SELECT json_deserialize_sql($tree)", {"tree": json.dumps(tree)}
        ).fetchone()
        return str(row[0]) if row else model.sql
    finally:
        connection.close()


def _inline(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    models: Mapping[str, Model],
    seen: frozenset[str],
) -> Tree:
    tree = _tree(connection, sql)
    ctes = _cte_names(tree)
    for node in list(_walk(tree)):
        if node.get("type") != "BASE_TABLE":
            continue
        parts = _path(node)
        if len(parts) == 3 and parts[0] == LAKE_CATALOG:
            parts = parts[1:]
        name = ".".join(parts)
        view = models.get(name)
        if len(parts) != 2 or name in ctes or view is None:
            continue
        if view.config.materialized != "view" or name in seen:
            continue
        inner = _inline(connection, view.sql, models, seen | {name})
        replacement = {
            "type": "SUBQUERY",
            "alias": node.get("alias") or parts[1],
            "sample": None,
            "query_location": node.get("query_location", 0),
            "query_location_length": node.get("query_location_length", 0),
            "subquery": {
                "node": copy.deepcopy(inner["statements"][0]["node"]),
                "named_param_map": [],
            },
            "column_name_alias": node.get("column_name_alias", []),
        }
        node.clear()
        node.update(replacement)
    return tree


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def plan_run(
    models: Sequence[Model],
    graph: Graph,
    outputs: Mapping[str, Columns] | None = None,
    existing: Mapping[str, Columns | None] | None = None,
    since: Mapping[str, str] | None = None,
    full_refresh: frozenset[str] = frozenset(),
    previous: Mapping[str, str | None] | None = None,
    differences: Mapping[str, Sequence[Comparison]] | None = None,
    filtered: Mapping[str, str] | None = None,
    extracted: Mapping[str, tuple[Column, ...]] | None = None,
) -> list[tuple[Model, list[Step | Extract | ModelTest | Reconcile]]]:
    """Steps that build every stored model, in run order (views are not stored).

    For incremental models only: `outputs` are the models' output columns, `existing` the
    columns of their tables in the lake (None when absent), `since` the `$since` value of each
    (`-infinity` when missing), `full_refresh` the models whose table is replaced, and
    `previous` their stored watermarks.

    Each model's tests (P1-15), then its reconciliation checks (P1-16), follow its write; a
    view with checks gets an entry with only them, run on its SQL. Tests need `outputs`, and
    reconciliation how each check compares, from the binder (`differences`). `filtered` holds
    the SQL of incremental models whose filter was proven and injected (P1-13), and
    `extracted` the validated ClickHouse output of each extraction model (P1-17, #98).
    """
    by_name = {model.name: model for model in models}
    relations = test_relations(models)
    plan: list[tuple[Model, list[Step | Extract | ModelTest | Reconcile]]] = []
    for name in graph.order:
        model = by_name[name]
        tests: list[ModelTest | Reconcile] = []
        if name in relations:
            tests += model_tests.steps(
                model, relations[name], (outputs or {}).get(name, []), relations
            )
            tests += reconciliation.steps(model, relations[name], (differences or {}).get(name, []))
        if model.config.materialized == "view":
            if tests:
                plan.append((model, list(tests)))
            continue
        if model.extraction:
            plan.append(
                (
                    model,
                    [
                        *extraction.steps(
                            model,
                            (extracted or {}).get(name, ()),
                            (outputs or {}).get(name, []),
                            (existing or {}).get(name),
                            (since or {}).get(name, incremental.LOWEST),
                            name in full_refresh,
                            (previous or {}).get(name),
                        ),
                        *tests,
                    ],
                )
            )
            continue
        sql = compiled_sql(model, by_name, (filtered or {}).get(name))
        if model.config.materialized == "incremental":
            output = (outputs or {}).get(name, [])
            plan.append(
                (
                    model,
                    [
                        *incremental.steps(
                            model,
                            sql,
                            output,
                            (existing or {}).get(name),
                            (since or {}).get(name, incremental.LOWEST),
                            name in full_refresh,
                            (previous or {}).get(name),
                        ),
                        *tests,
                    ],
                )
            )
            continue
        schema, table = _identifier(model.schema), _identifier(model.table)
        plan.append(
            (
                model,
                [
                    StatementRequest(
                        kind="statement", sql=f"CREATE SCHEMA IF NOT EXISTS {schema}", parameters=[]
                    ),
                    StatementRequest(
                        kind="statement",
                        sql=f"CREATE OR REPLACE TABLE {schema}.{table} AS {sql}",
                        parameters=[],
                    ),
                    *tests,
                ],
            )
        )
    return plan


def test_relations(models: Sequence[Model]) -> dict[str, str]:
    """What a test reads for each model: its table (an extraction model's too), or a view's SQL
    in parentheses."""
    by_name = {model.name: model for model in models}
    return {
        model.name: f"({compiled_sql(model, by_name)})"
        if model.config.materialized == "view"
        else f"{_identifier(model.schema)}.{_identifier(model.table)}"
        for model in models
    }


def known_output_names(sql: str) -> list[str] | None:
    """The output column names of a model's SELECT when its text alone decides them (#113), in
    order; None when only binding can (an unnamed expression, a star next to other items or over
    a join, `COLUMNS(…)`, a star that renames). A star over one table alone gives [], since a
    table holds no repeated name. Names are the text's spelling."""
    connection = _parser()
    try:
        tree = _tree(connection, sql)
    except duckdb.Error:
        return None
    finally:
        connection.close()
    statements = tree.get("statements")
    if tree.get("error") or not isinstance(statements, list) or len(statements) != 1:
        return None

    def ctes(node: Tree, known: dict[str, Tree]) -> dict[str, Tree]:
        found = dict(known)
        cte_map = node.get("cte_map")
        for entry in cte_map.get("map", []) if isinstance(cte_map, dict) else []:
            value = entry.get("value") if isinstance(entry, dict) else None
            if isinstance(value, dict) and isinstance(value.get("query_node"), dict):
                if value.get("aliases"):
                    return {}  # renamed by position: not followed
                found[str(entry.get("key", "")).lower()] = value["query_node"]
        return found

    def names(node: object, known: dict[str, Tree]) -> list[str] | None:
        if not isinstance(node, dict):
            return None
        scope = ctes(node, known)
        if node.get("type") == "SET_OPERATION_NODE":
            children = node.get("children")
            first = children[0] if isinstance(children, list) and children else node.get("left")
            return names(first, scope)  # the first branch names the columns
        if node.get("type") != "SELECT_NODE":
            return None
        items = node.get("select_list")
        if not isinstance(items, list):
            return None
        found: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                return None
            if item.get("class") == "STAR":
                if len(items) != 1 or item.get("columns") or item.get("expr") is not None:
                    return None
                if item.get("rename_list") or item.get("relation_name"):
                    return None
                source = node.get("from_table")
                kind = source.get("type") if isinstance(source, dict) else None
                if kind == "SUBQUERY" and isinstance(source, dict):
                    subquery = source.get("subquery")
                    inner = subquery.get("node") if isinstance(subquery, dict) else None
                    if source.get("column_name_alias"):
                        return None
                    return names(inner, scope)
                if kind == "BASE_TABLE" and isinstance(source, dict):
                    table = str(source.get("table_name") or "").lower()
                    if not source.get("schema_name") and table in scope:
                        if source.get("column_name_alias"):
                            return None
                        return names(scope[table], scope)
                    return []  # one table: its names are distinct
                return None
            alias = str(item.get("alias") or "")
            parts = item.get("column_names")
            if alias:
                found.append(alias)
            elif item.get("class") == "COLUMN_REF" and isinstance(parts, list) and parts:
                found.append(str(parts[-1]))
            else:
                return None
        return found

    return names(statements[0].get("node"), {})
