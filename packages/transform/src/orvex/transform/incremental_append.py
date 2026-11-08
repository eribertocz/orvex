"""Which rows an `append` model reads once: no join, no run-dependent condition (#194)."""

from __future__ import annotations

import json
from collections.abc import Mapping

import duckdb
from orvex.contracts.model_config.v1 import ModelError
from orvex.transform.incremental_functions import SPECIAL_NAMES, UNSTABLE_FUNCTIONS, is_consistent
from orvex.transform.incremental_since import MAX_DEPTH, tree_depth
from orvex.transform.incremental_types import SINCE
from orvex.transform.models import Model, problem
from orvex.transform.sql_parser import parser


def _walk(node: object) -> list[dict[str, object]]:
    """Every dict of a parse tree (without recursion)."""
    found: list[dict[str, object]] = []
    pending = [node]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            found.append(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


def _changing_call(expression: object, changing: Mapping[str, str]) -> str | None:
    """The first function of an expression that can give another value in another run (not
    `CONSISTENT`, or in `UNSTABLE_FUNCTIONS`), or the one computing a column it reads
    (`changing`: output names → that function, read by name, through a struct field, by
    position or with `COLUMNS`); None when there is none. A comparison with
    `$since` is left to the bound's own rule (`since_is_a_lower_bound`), which accepts only
    `time_column > $since` in an `append` model and names what to write instead."""
    nodes = _walk(expression)
    bounds = {
        id(inner)
        for node in nodes
        if node.get("class") == "COMPARISON"
        and any(
            inner.get("class") == "PARAMETER" and str(inner.get("identifier", "")).lower() == SINCE
            for inner in _walk(node)
        )
        for inner in _walk(node)
    }
    for node in nodes:
        if id(node) in bounds:
            continue
        kind = node.get("class")
        if kind == "FUNCTION":
            name = str(node.get("function_name")).lower()
            if not is_consistent(name) or name in UNSTABLE_FUNCTIONS:
                return name
        parts = node.get("column_names")
        if kind == "COLUMN_REF" and isinstance(parts, list) and parts:
            last = str(parts[-1]).lower()
            if last in SPECIAL_NAMES:
                return last
            # Any part: `r.a` reads field `a` of a changing column `r` (#230, Reviewer pass 1).
            for part in parts:
                if str(part).lower() in changing:
                    return changing[str(part).lower()]
        if kind in {"POSITIONAL_REFERENCE", "STAR"} and changing:
            # `#4` or `COLUMNS(…)` may read a changing column; which one cannot be told here.
            return next(iter(changing.values()))
    return None


def _append_row_path(statement: object) -> tuple[str, str] | None:
    """The first join, or condition that can give another result in another run, on the row
    path of an `append` model (#194): ("join", "") or ("condition", <function>); None when
    there is none.

    The row path is the model's query and every query whose rows it reads in `FROM`: subqueries,
    CTEs read there, set-operation branches. Any join there is refused (decision #217 A): a row
    left out because its match is not there yet is never read again. A condition (`WHERE`,
    `HAVING`, `QUALIFY`) may not call a function whose value changes between runs, nor read a
    column one computed below or in the same select list. Table functions (`range`,
    `generate_series`, `unnest`, `read_parquet`) read no other table: not followed here."""
    found: list[tuple[str, str]] = []
    memo: dict[tuple[int, tuple[str, ...]], dict[str, str]] = {}

    def ctes(node: dict[str, object]) -> dict[str, object]:
        result: dict[str, object] = {}
        cte_map = node.get("cte_map")
        entries = cte_map.get("map") if isinstance(cte_map, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            value = entry.get("value") if isinstance(entry, dict) else None
            if isinstance(value, dict):
                result[str(entry.get("key", "")).lower()] = value.get("query_node")
        return result

    def from_changing(source: object, scope: dict[str, object]) -> dict[str, str]:
        """The output names of a FROM clause computed by a function that changes between runs."""
        if not isinstance(source, dict):
            return {}
        kind = source.get("type")
        if kind == "JOIN":
            found.append(("join", ""))
            return {}
        inner: dict[str, str] = {}
        if kind == "SUBQUERY":
            subquery = source.get("subquery")
            inner = node_changing(
                subquery.get("node") if isinstance(subquery, dict) else None, scope
            )
        elif kind == "BASE_TABLE":
            name = str(source.get("table_name") or "").lower()
            if not source.get("schema_name") and not source.get("catalog_name") and name in scope:
                # The CTE's body does not see itself (`WITH m AS (SELECT * FROM m)`).
                rest = {key: value for key, value in scope.items() if key != name}
                inner = node_changing(scope[name], rest)
        renamed = source.get("column_name_alias")
        if inner and isinstance(renamed, list):
            # `AS t(a, b)` renames by position: every new name is taken as changing.
            first = next(iter(inner.values()))
            return {str(alias).lower(): first for alias in renamed} | inner
        return inner

    def node_changing(node: object, outer: dict[str, object]) -> dict[str, str]:
        """The output names of a query computed by a function that changes between runs."""
        if found or not isinstance(node, dict):
            return {}
        key = (id(node), tuple(sorted(outer)))
        if key in memo:
            return memo[key]
        result: dict[str, str] = {}
        if node.get("type") == "SELECT_NODE":
            scope = outer | ctes(node)
            below = from_changing(node.get("from_table"), scope)
            result = select_changing(node, below)
            for clause in ("where_clause", "having", "qualify"):
                # A select-list alias can be read in these clauses too.
                function = _changing_call(node.get(clause), below | result)
                if function is not None:
                    found.append(("condition", function))
                    break
        elif node.get("type") in {"SET_OPERATION_NODE", "RECURSIVE_CTE_NODE"}:
            scope = outer | ctes(node)
            children = node.get("children")
            branches: list[object] = [
                *(children if isinstance(children, list) else []),
                node.get("left"),
                node.get("right"),
            ]
            for branch in branches:
                result = node_changing(branch, scope) | result
        memo[key] = result
        return result

    def select_changing(node: dict[str, object], below: dict[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        items = node.get("select_list")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("class") == "STAR":
                excluded = {str(x).lower() for x in item.get("exclude_list") or []}
                result |= {
                    name: function for name, function in below.items() if name not in excluded
                }
                replaced = item.get("replace_list")
                for entry in replaced if isinstance(replaced, list) else []:
                    if isinstance(entry, dict):
                        function = _changing_call(entry.get("value"), below)
                        if function is not None:
                            result[str(entry.get("key", "")).lower()] = function
                continue
            function = _changing_call(item, below)
            parts = item.get("column_names")
            label = str(item.get("alias") or "").lower()
            if not label and item.get("class") == "COLUMN_REF" and isinstance(parts, list):
                label = str(parts[-1]).lower() if parts else ""
            if function is not None and label:
                result[label] = function
        return result

    node_changing(statement, {})
    return found[0] if found else None


def append_rows_problem(model: Model) -> ModelError | None:
    """`INVALID_MODEL_SQL` when the rows of a DuckDB-SQL `append` model pass through a join, or
    a condition whose result can change between runs (`_append_row_path`), whatever its
    `filter`; None otherwise, and for any other model. An `append` model reads each row once,
    so a row such a query leaves out at or before the watermark is never loaded (#194)."""
    config = model.config
    incremental = config.incremental
    if (
        config.materialized != "incremental"
        or incremental is None
        or incremental.strategy != "append"
    ):
        return None
    connection = parser()
    try:
        row = connection.execute("SELECT json_serialize_sql($sql)", {"sql": model.sql}).fetchone()
    except duckdb.Error:
        return None  # not readable: reported where the SQL is bound
    finally:
        connection.close()
    deep = problem(
        "INVALID_MODEL_SQL",
        f"{model.sql_file}: queries and expressions nested more than {MAX_DEPTH} levels "
        "deep: too deeply nested to check which rows an append model reads.",
        model.sql_file,
        model=model.name,
    )
    try:
        tree = json.loads(str(row[0])) if row else {}
    except RecursionError:
        return deep
    statements = tree.get("statements")
    if tree.get("error") or not isinstance(statements, list) or len(statements) != 1:
        return None  # not one statement: reported where the SQL is bound
    if tree_depth(statements) > MAX_DEPTH:
        return deep
    try:
        found = _append_row_path(statements[0].get("node"))
    except RecursionError:
        return deep
    if found is None:
        return None
    kind, function = found
    if kind == "join":
        message = (
            f"{model.sql_file}: an append model reads each row once, so a row a join leaves "
            "out (its match arrives later, or a subquery picks other rows) is never loaded: it "
            "reads one table, or a subquery over one table, with no join of any kind (JOIN, "
            "CROSS, NATURAL, SEMI, ANTI, ASOF, POSITIONAL, LATERAL or a comma between tables). "
            "To join, declare the model merge with a lookback, or partition_overwrite with a "
            "lookback in its $since bound (date_trunc('month', $since)); or load each table "
            "with its own append model and join them downstream."
        )
    else:
        message = (
            f"{model.sql_file}: an append model reads each row once, so a condition whose "
            "result can change between runs leaves rows out for good: its WHERE, HAVING and "
            f"QUALIFY may not use {function} (only functions that give the same value in every "
            "run, such as WHERE id % 2 = 0). Use strategy merge with a lookback to read rows "
            "again."
        )
    return problem("INVALID_MODEL_SQL", message, model.sql_file, model=model.name)
