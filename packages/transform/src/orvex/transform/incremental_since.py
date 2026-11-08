"""Where an incremental model's `$since` may appear: the lower-bound rule and its depth limit.

A `partition_overwrite` or manual `append` model's `$since` must be a lower bound of its
`time_column` (#135, #155); queries nested deeper than `MAX_DEPTH` are refused (#183).
"""

from __future__ import annotations

import json
from collections.abc import Callable

import duckdb
from orvex.contracts.model_config.v1 import ModelError
from orvex.transform.incremental_functions import (
    SPECIAL_NAMES,
    UNSTABLE_FUNCTIONS,
    is_aggregate,
    is_consistent,
)
from orvex.transform.incremental_types import SINCE
from orvex.transform.models import Model, problem
from orvex.transform.sql_parser import parser


def uses_since(sql: str) -> bool:
    """Whether the SQL references the `$since` parameter (DuckDB's parser; text in strings and
    comments does not count)."""
    connection = parser()
    try:
        statements = connection.extract_statements(sql)
    except duckdb.Error:
        return False
    finally:
        connection.close()
    # DuckDB binds `$SINCE` and `$Since` to the same parameter: the name is matched without case.
    return any(
        name.lower() == SINCE for statement in statements for name in statement.named_parameters
    )


# `time_column > $since` (or `$since < time_column`): the only bound an `append` model may use
# (#135): it reads each row once.
_STRICTLY_FROM_BELOW = frozenset({"COMPARE_GREATERTHAN", "COMPARE_LESSTHAN"})
# `time_column > …`, `time_column >= …` (and the same written the other way round): the only
# comparisons a `$since` may be in, in a partition_overwrite model (#155).
_FROM_BELOW = {
    "COMPARE_GREATERTHAN": "left",
    "COMPARE_GREATERTHANOREQUALTO": "left",
    "COMPARE_LESSTHAN": "right",
    "COMPARE_LESSTHANOREQUALTO": "right",
}


# Functions whose value changes from row to row: a bound using one is not one bound.
_PER_ROW_FUNCTIONS = frozenset({"random", "gen_random_uuid", "uuid", "uuidv4", "uuidv7"})


# The lowercase column names of a table, from its parts as written (`feed.data` → ("feed",
# "data")); None when unknown.
type ColumnsOf = Callable[[tuple[str, ...]], frozenset[str] | None]


def since_is_a_lower_bound(
    sql: str, time_column: str, columns_of: ColumnsOf | None = None, strict: bool = False
) -> bool:
    """Whether every `$since` of a DuckDB-SQL model is in a lower bound of the time column of
    the rows the model returns (#155):

    - the bound is `time_column > x` or `time_column >= x` (or `x < time_column`, `x <=
      time_column`), where `x` uses `$since` and constants but no column; `OR time_column IS
      NULL` may follow it, to reload undated rows too;
    - it is a condition of the `WHERE` clause of a query whose rows are the model's rows: the
      model's own query, or a query it reads whole as its only `FROM` (a subquery or a CTE read
      nowhere else), never under a `UNION`; that query reads one table or subquery, not a join;
    - every query on that path passes the time column through unchanged and keeps its rows as
      they are: the column is in its select list as itself (`read_at`, `t.read_at`, `read_at AS
      read_at`, or a plain `*`/`t.*`), nothing else is named like it, and the query has no
      `GROUP BY`/`GROUP BY ALL`, `HAVING`, window function, `QUALIFY`, `LIMIT`/`OFFSET`,
      `DISTINCT ON` or sample (plain `DISTINCT` and `ORDER BY` are allowed);
    - the column is matched by name (any letter case); a qualifier, if any, must name that
      query's table (its alias or name); nothing on the path renames columns by position (a
      column list `AS t(a, b)` or `WITH c(a, b)`);
    - one exception to "as itself": the query that holds the bound may compute the time column
      (`CAST(read_at AS DATE) AS day`) when the bound names it without a qualifier and the table
      it reads has no column of that name. DuckDB then reads the name as that alias, so the
      bound bounds the output column. That needs the table's columns: `columns_of` gives them
      (the server, from the bound tables); without it (`config_problems`, the sidecar rules) the
      exception is assumed, and the server checks it again with the columns.

    Such a filter loads every row of a partition from some time on, and the output's time
    column is that same column, so the oldest-row check sees any partition it loads in part.
    Anything else (an upper bound, `BETWEEN`, `OR`/`NOT` around the bound, a bound that reads a
    column or a random value, a `$since` in an `IN`/`EXISTS`/scalar subquery, in
    `EXCEPT`/`INTERSECT`, in a query that joins, an output time column computed from it (a
    cast, an aggregate), or rows computed over the filtered rows) could drop rows the check
    cannot see.
    Read from DuckDB's parse tree; False when the SQL cannot be read."""
    connection = parser()
    try:
        row = connection.execute("SELECT json_serialize_sql($sql)", {"sql": sql}).fetchone()
    except duckdb.Error:
        return False
    finally:
        connection.close()
    try:
        tree = json.loads(str(row[0])) if row else {"error": True}
    except RecursionError:
        return False  # too deep to read: refused (#183)
    statements = tree.get("statements")
    if tree.get("error") or not isinstance(statements, list) or len(statements) != 1:
        return False
    if tree_depth(statements) > MAX_DEPTH:
        return False  # too deep to follow: refused before any recursive walk (#183)
    wanted = time_column.lower()
    type Node = dict[str, object]

    def is_since(node: object) -> bool:
        return (
            isinstance(node, dict)
            and node.get("class") == "PARAMETER"
            and str(node.get("identifier", "")).lower() == SINCE
        )

    def nodes(node: object) -> list[Node]:
        found: list[Node] = []
        if isinstance(node, list):
            for item in node:
                found += nodes(item)
        elif isinstance(node, dict):
            found.append(node)
            for value in node.values():
                found += nodes(value)
        return found

    def ctes(node: Node) -> dict[str, object]:
        found: dict[str, object] = {}
        cte_map = node.get("cte_map")
        entries = cte_map.get("map") if isinstance(cte_map, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            value = entry.get("value") if isinstance(entry, dict) else None
            if isinstance(value, dict) and isinstance(value.get("query_node"), dict):
                # `WITH c(a, b) AS (…)` renames by position: not followed (refused).
                found[str(entry.get("key", "")).lower()] = (
                    None if value.get("aliases") else value["query_node"]
                )
        return found

    def column_parts(node: Node) -> list[str]:
        """A column reference's name parts in lowercase; [] for anything else."""
        parts = node.get("column_names")
        if node.get("class") != "COLUMN_REF" or not isinstance(parts, list):
            return []
        return [str(part).lower() for part in parts]

    # The queries whose rows are the model's rows, each with the names of its one table.
    row_queries: dict[int, set[str]] = {}
    # Row queries whose time column is an alias of an expression, with that expression.
    computed: dict[int, Node] = {}
    everything = nodes(statements)
    every_cte = {name for n in everything for name in ctes(n)}
    reads = [
        str(n.get("table_name", "")).lower()
        for n in everything
        if n.get("type") == "BASE_TABLE" and not n.get("schema_name")
    ]

    # `query('…')` and `query_table('…')` read relations by name at run time: their reads of a
    # CTE cannot be counted, so no CTE is followed in a statement with them.
    dynamic = any(
        str(n.get("function_name", "")).lower() in {"query", "query_table"} for n in everything
    )

    def named(item: Node) -> str:
        """The name a select-list item gives its column ("" when it is not a plain one)."""
        alias = str(item.get("alias") or "")
        if alias:
            return alias.lower()
        parts = item.get("column_names")
        if item.get("class") == "COLUMN_REF" and isinstance(parts, list) and parts:
            return str(parts[-1]).lower()
        return ""

    def passes_through(node: Node, names: set[str]) -> bool:
        """The query returns the time column unchanged, one output row per input row."""
        modifiers = node.get("modifiers")
        for modifier in modifiers if isinstance(modifiers, list) else []:
            kind = modifier.get("type") if isinstance(modifier, dict) else None
            if kind == "ORDER_MODIFIER":
                continue
            if kind == "DISTINCT_MODIFIER" and not modifier.get("distinct_on_targets"):
                continue  # identical rows, time included: all of them or none are reloaded
            return False  # LIMIT, OFFSET, DISTINCT ON
        source = node.get("from_table")
        if (
            node.get("group_expressions")
            or node.get("aggregate_handling") != "STANDARD_HANDLING"
            or node.get("having") is not None
            or node.get("qualify") is not None
            or node.get("sample") is not None
            or (isinstance(source, dict) and source.get("sample") is not None)
        ):
            return False
        items = node.get("select_list")
        if not isinstance(items, list) or any(n.get("class") == "WINDOW" for n in nodes(items)):
            return False
        kept = 0
        for item in items:
            if not isinstance(item, dict):
                return False
            if item.get("class") == "STAR":
                relation = str(item.get("relation_name") or "").lower()
                replaced = item.get("replace_list")
                if (
                    item.get("columns")
                    or item.get("expr") is not None
                    or item.get("rename_list")
                    or (relation and relation not in names)
                    or wanted in {str(x).lower() for x in item.get("exclude_list") or []}
                    or item.get("qualified_exclude_list")
                    or any(
                        str(entry.get("key", "")).lower() == wanted
                        for entry in (replaced if isinstance(replaced, list) else [])
                        if isinstance(entry, dict)
                    )
                ):
                    return False
                kept += 1
            elif named(item) == wanted:
                if not is_time_column(item, names):
                    computed[id(node)] = item  # an alias: the bound must name it (see check)
                kept += 1
        stars = sum(1 for item in items if isinstance(item, dict) and item.get("class") == "STAR")
        # `*, CAST(read_at AS DATE) AS day`: one column only if the table has no `day`, which
        # the check of a computed time column requires.
        return kept == 1 or (kept == 2 and stars == 1 and id(node) in computed)

    def reads_no_such_column(node: Node) -> bool:
        """The query's one table has no column named like the time column, so DuckDB reads an
        unqualified name in its WHERE as the select list's alias."""
        source = node.get("from_table")
        if not isinstance(source, dict) or source.get("type") != "BASE_TABLE":
            return False  # a subquery's columns are not looked at: refused
        expression = computed.get(id(node))
        if any(column_parts(n)[-1:] == [wanted] for n in nodes(expression)):
            return False  # it reads a column of that name: the table has one
        parts = tuple(
            str(source.get(key) or "")
            for key in ("catalog_name", "schema_name", "table_name")
            if source.get(key)
        )
        if len(parts) == 1 and parts[0].lower() in every_cte:
            return False  # a CTE's columns are not looked at: refused
        if columns_of is None:
            return True  # checked again with the columns (the server)
        found = columns_of(parts)
        return found is not None and wanted not in found

    def is_time_column(node: object, names: set[str]) -> bool:
        if not isinstance(node, dict) or node.get("class") != "COLUMN_REF":
            return False
        parts = node.get("column_names")
        if not isinstance(parts, list) or not parts or str(parts[-1]).lower() != wanted:
            return False
        return len(parts) == 1 or str(parts[-2]).lower() in names

    def follow(node: object, scope: dict[str, object]) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "SET_OPERATION_NODE":
            # Not followed: another branch can load an older row of a partition (another table,
            # an older bound, no bound), which hides the cut from the oldest-row check.
            return
        if node.get("type") != "SELECT_NODE":
            return
        inner = scope | ctes(node)
        source = node.get("from_table")
        if not isinstance(source, dict) or source.get("type") not in {"BASE_TABLE", "SUBQUERY"}:
            return  # a join, a table function, no FROM: not a query the rule follows
        if source.get("column_name_alias"):
            return  # `AS t(a, b)` renames by position: not followed (refused)
        names = {str(source.get("alias") or "").lower()} - {""}
        if source.get("type") == "BASE_TABLE":
            names.add(str(source.get("table_name", "")).lower())
        if not passes_through(node, names):
            return
        row_queries[id(node)] = names
        if id(node) in computed:
            # Its time column is computed: a bound below it would bound another column.
            return
        if source.get("type") == "SUBQUERY":
            subquery = source.get("subquery")
            follow(subquery.get("node") if isinstance(subquery, dict) else None, inner)
        else:
            name = str(source.get("table_name", "")).lower()
            if (
                not source.get("schema_name")
                and inner.get(name) is not None
                and reads.count(name) == 1
                and not dynamic
            ):
                # A CTE read again elsewhere (a subquery, a join) computes over the loaded rows.
                follow(inner[name], {key: value for key, value in inner.items() if key != name})

    follow(statements[0].get("node") if isinstance(statements[0], dict) else None, {})

    def bound(node: object) -> bool:
        """`$since` and constants only: no column and no random value, so the bound is the same
        for every row."""
        inside = nodes(node)
        return any(is_since(n) for n in inside) and not any(
            n.get("class") in {"COLUMN_REF", "SUBQUERY"}
            or str(n.get("function_name", "")).lower() in _PER_ROW_FUNCTIONS
            for n in inside
        )

    def lower_bound(node: object, names: set[str]) -> bool:
        if not isinstance(node, dict) or node.get("class") != "COMPARISON":
            return False
        kind = str(node.get("type"))
        side = _FROM_BELOW.get(kind)
        if side is None:
            return False
        other = node.get("right" if side == "left" else "left")
        if strict:
            # `append` (#135): `time_column > $since` exactly, so no row is read twice.
            return (
                kind in _STRICTLY_FROM_BELOW
                and is_time_column(node.get(side), names)
                and is_since(other)
            )
        return is_time_column(node.get(side), names) and bound(other)

    def is_time_null(node: object, names: set[str]) -> bool:
        if not isinstance(node, dict) or node.get("type") != "OPERATOR_IS_NULL":
            return False
        children = node.get("children")
        return (
            isinstance(children, list) and len(children) == 1 and is_time_column(children[0], names)
        )

    def unqualified(node: object) -> bool:
        return any(column_parts(n) == [wanted] for n in nodes(node))

    def check(node: object, names: set[str] | None, alias: bool = False) -> bool:
        """`names`: the node is a condition of the WHERE clause of a row query, joined to it by
        AND only, and these are the names of that query's table; None otherwise. `alias`: that
        query computes its time column (a bound must then name it without a qualifier)."""
        if isinstance(node, list):
            return all(check(item, names, alias) for item in node)
        if not isinstance(node, dict):
            return True
        if is_since(node):
            return False  # reached outside an accepted lower bound
        if names is not None and lower_bound(node, names):
            return not alias or unqualified(node)
        if names is not None and node.get("class") == "CONJUNCTION":
            children = node.get("children")
            if node.get("type") == "CONJUNCTION_OR":
                # `time_column > $since OR time_column IS NULL`: the undated rows too (not for
                # `append`: it would insert them again in every run).
                return (
                    not strict
                    and isinstance(children, list)
                    and all(
                        (lower_bound(child, names) or is_time_null(child, names))
                        and (not alias or unqualified(child))
                        for child in children
                    )
                )
            if node.get("type") == "CONJUNCTION_AND":
                return check(children, names, alias)
        where = row_queries.get(id(node))
        aliased = id(node) in computed
        if aliased and where is not None and not reads_no_such_column(node):
            where = None  # the name in WHERE is the table's column, not the computed one
        return all(
            check(value, where if key == "where_clause" else None, aliased)
            for key, value in node.items()
        )

    # How a name was traced: to a table's own column (each row's value is fixed), not produced
    # by this query, or refused (computed in a way the run cannot hold fixed, or not traceable).
    accepted, absent, refused = "accepted", "absent", "refused"

    def scope_of(node: Node, outer: dict[str, object]) -> dict[str, object] | None:
        found = dict(outer)
        cte_map = node.get("cte_map")
        entries = cte_map.get("map") if isinstance(cte_map, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            value = entry.get("value") if isinstance(entry, dict) else None
            if not isinstance(value, dict) or not isinstance(value.get("query_node"), dict):
                return None
            if value.get("aliases"):
                return None  # `WITH c(a, b) AS …` renames by position: not traced
            found[str(entry.get("key", "")).lower()] = value["query_node"]
        return found

    def expression_state(expression: object, source: object, scope: dict[str, object]) -> str:
        """A select item's expression: stable functions only, each column it reads traced."""
        result = accepted
        for node in nodes(expression):
            kind = node.get("class")
            if kind is not None and kind not in _TRACED_EXPRESSIONS:
                return refused  # a window, a subquery, `#2`, a lambda, …: not traced
            if kind == "FUNCTION":
                name = str(node.get("function_name")).lower()
                if not is_consistent(name) or name in UNSTABLE_FUNCTIONS or is_aggregate(name):
                    # An aggregate's value can change when rows join its group, or with the
                    # order rows arrive in (`list(t)[1]`, `arg_max`): refused (pass 3).
                    return refused
            if kind == "COLUMN_REF":
                parts = column_parts(node)
                if not parts or parts[-1] in SPECIAL_NAMES:
                    return refused
                if len(parts) == 1:
                    state = from_state(source, parts[0], None, scope)
                elif len(parts) == 2:
                    state = from_state(source, parts[1], parts[0], scope)
                else:
                    return refused
                if state != accepted:
                    return refused  # absent here: a struct field, or an outer column
        return result

    def from_state(
        source: object, name: str, relation: str | None, scope: dict[str, object]
    ) -> str:
        """Where a FROM clause gets the column `name` (of `relation`, when qualified)."""
        if not isinstance(source, dict):
            return absent
        kind = source.get("type")
        alias = str(source.get("alias") or "").lower()
        if kind == "EMPTY":
            return absent
        if source.get("column_name_alias"):
            return refused  # renamed by position: not traced
        if kind == "JOIN":
            # No join of any kind (#194, decision #217 A): an outer, semi, anti, ASOF or
            # positional join can give a row another time when other rows arrive (#189), and
            # any join leaves out for good a row whose match arrives late.
            return refused
        if kind == "BASE_TABLE":
            table = str(source.get("table_name") or "").lower()
            if relation is not None and relation not in {alias or table}:
                return absent
            if not source.get("schema_name") and not source.get("catalog_name") and table in scope:
                # The CTE's body does not see itself (`WITH m AS (SELECT * FROM m)` reads the
                # table m), nor any CTE already being followed.
                inner_scope = {key: value for key, value in scope.items() if key != table}
                return node_state(scope[table], name, inner_scope)
            return accepted  # a source or model table: its column holds each row's value
        if kind == "SUBQUERY":
            if relation is not None and relation != alias:
                return absent
            subquery = source.get("subquery")
            inner = subquery.get("node") if isinstance(subquery, dict) else None
            return node_state(inner, name, scope)
        return refused  # table functions, PIVOT, VALUES, …: not traced

    def node_state(node: object, name: str, outer: dict[str, object]) -> str:
        """How a query produces the output column `name`."""
        if not isinstance(node, dict) or node.get("type") != "SELECT_NODE":
            return refused  # set operations name columns by position: not traced
        if not keeps_rows(node):
            return refused  # rows it leaves out are never read again (#183)
        scope = scope_of(node, outer)
        if scope is None:
            return refused
        source = node.get("from_table")
        items = node.get("select_list")
        if not isinstance(items, list):
            return refused
        found: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                return refused
            if item.get("class") == "STAR":
                if item.get("rename_list") or item.get("columns") or item.get("expr") is not None:
                    return refused
                replaced = item.get("replace_list")
                replacements = {
                    str(entry.get("key", "")).lower(): entry.get("value")
                    for entry in (replaced if isinstance(replaced, list) else [])
                    if isinstance(entry, dict)
                }
                excluded = {str(x).lower() for x in item.get("exclude_list") or []}
                if item.get("qualified_exclude_list"):
                    return refused
                if name in replacements:
                    found.append(expression_state(replacements[name], source, scope))
                elif name not in excluded:
                    relation = str(item.get("relation_name") or "").lower() or None
                    state = from_state(source, name, relation, scope)
                    if state != absent:
                        found.append(state)
                continue
            parts = column_parts(item)
            label = str(item.get("alias") or "").lower() or (parts[-1] if parts else "")
            if label == name:
                found.append(expression_state(item, source, scope))
        if len(found) != 1:
            return refused if found else absent
        return found[0]

    def keeps_rows(node: Node) -> bool:
        """Whether a query on the time column's trace keeps every row it reads (#183): no
        `LIMIT`/`OFFSET`, sample (on any table it reads, joined ones included), `DISTINCT ON`,
        `QUALIFY`, window function, nor a subquery in its select list, `WHERE`, `HAVING` or a
        join's `ON`: each lets this query, or one above it, choose rows by reading others. An
        `append` model never reads again a row it left out at or before the watermark."""
        modifiers = node.get("modifiers")
        for modifier in modifiers if isinstance(modifiers, list) else []:
            if not isinstance(modifier, dict):
                return False
            kind = modifier.get("type")
            if kind in {"LIMIT_MODIFIER", "LIMIT_PERCENT_MODIFIER"}:
                return False
            if kind == "DISTINCT_MODIFIER" and modifier.get("distinct_on_targets"):
                return False
        source = node.get("from_table")
        if node.get("sample") is not None or node.get("qualify") is not None:
            return False
        # A window function lets a query above choose rows per key, as `QUALIFY` does
        # (`row_number() OVER (…) AS rn` … `WHERE rn = 1`; Reviewer pass 1 on #188).
        # A subquery in the select list does the same: `(SELECT max(read_at) … WHERE s2.id =
        # s.id) AS m` … `WHERE read_at = m` (Reviewer pass 3 on #188).
        if any(
            item.get("class") in {"WINDOW", "SUBQUERY"} for item in nodes(node.get("select_list"))
        ):
            return False
        # Conditions that read other rows: in `WHERE`, `HAVING`, or a join's `ON`.
        conditions: list[object] = [node.get("where_clause"), node.get("having")]
        joined: list[object] = [source]
        while joined:
            table = joined.pop()
            if not isinstance(table, dict):
                continue
            # A sample on the table read, or on any table it joins (Reviewer pass 2 on #188).
            if table.get("sample") is not None:
                return False
            if table.get("type") == "JOIN":
                conditions.append(table.get("condition"))
                joined += [table.get("left"), table.get("right")]
        return not any(
            item.get("class") == "SUBQUERY" for found in conditions for item in nodes(found)
        )

    def stable(tree: object) -> bool:
        """Whether the time column of the model's rows gets the same value for a row in every
        run (#135, Reviewer passes 1 and 2): it is traced from the model's output down to table
        columns, through aliases, `REPLACE`, stars, subqueries and CTEs (never a join, #194), and
        computed only by functions DuckDB marks `CONSISTENT` (less a fixed list it marks wrongly).
        Anything not traced is refused: column lists that rename by position, struct fields,
        `RENAME`, `COLUMNS(…)`, table functions, `VALUES`, set operations below the model's
        query."""
        if not isinstance(tree, list) or len(tree) != 1 or not isinstance(tree[0], dict):
            return False
        try:
            return node_state(tree[0].get("node"), wanted, {}) == accepted
        except RecursionError:
            return False  # too deep to follow: refused

    try:
        return check(statements, None) and (not strict or stable(statements))
    except RecursionError:
        return False  # too deep to follow: refused (#183)


# How deeply queries and expressions may nest in a model whose `$since` is checked: deeper is
# refused, rather than followed until Python's recursion limit (#183).
MAX_DEPTH = 64


def tree_depth(tree: object) -> int:
    """How deeply queries and expressions nest in a parse tree: each query (`SELECT`, set
    operation) and each expression (a node with a `class`) counts one level. Walked without
    recursion, so any depth can be measured."""
    deepest = 0
    stack: list[tuple[object, int]] = [(tree, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            if "class" in node or str(node.get("type")) in {"SELECT_NODE", "SET_OPERATION_NODE"}:
                depth += 1
                deepest = max(deepest, depth)
            stack.extend((value, depth) for value in node.values())
        elif isinstance(node, list):
            stack.extend((value, depth) for value in node)
    return deepest


def sql_depth(sql: str) -> int:
    """`tree_depth` of a DuckDB-SQL statement; 0 when it does not parse, more than `MAX_DEPTH`
    when its parse tree is too deep for Python to read."""
    connection = parser()
    try:
        row = connection.execute("SELECT json_serialize_sql($sql)", {"sql": sql}).fetchone()
    except duckdb.Error:
        return 0
    finally:
        connection.close()
    try:
        tree = json.loads(str(row[0])) if row else {}
    except RecursionError:
        return MAX_DEPTH + 1
    return tree_depth(tree.get("statements"))


# The expression kinds the time column's trace understands: anything else is refused (#135,
# Reviewer pass 3: a positional reference `#2` was not).
_TRACED_EXPRESSIONS = frozenset(
    {
        "COLUMN_REF",
        "CONSTANT",
        "FUNCTION",
        "CAST",
        "TYPE",  # the target type of a CAST
        "OPERATOR",
        "CASE",
        "COMPARISON",
        "CONJUNCTION",
        "BETWEEN",
    }
)


def since_bound_problem(model: Model, columns_of: ColumnsOf | None = None) -> ModelError | None:
    """`INVALID_MODEL_SQL` when a manual `partition_overwrite` DuckDB-SQL model's `$since` is
    not a lower bound of its time column (`since_is_a_lower_bound`); None otherwise, and for any
    other model. `columns_of`: the tables' columns, when known (the server)."""
    config = model.config
    incremental = config.incremental
    if (
        config.materialized != "incremental"
        or incremental is None
        or incremental.strategy not in {"partition_overwrite", "append"}
        or (incremental.filter or "auto") == "auto"
        or not uses_since(model.sql)
    ):
        return None
    append = incremental.strategy == "append"
    column = incremental.time_column.root
    if sql_depth(model.sql) > MAX_DEPTH:
        return problem(
            "INVALID_MODEL_SQL",
            f"{model.sql_file}: queries and expressions nested more than {MAX_DEPTH} levels "
            "deep: too deeply nested to check where $since bounds the time column.",
            model.sql_file,
            model=model.name,
        )
    if since_is_a_lower_bound(model.sql, column, columns_of, strict=append):
        return None
    if append:
        return problem(
            "INVALID_MODEL_SQL",
            f"{model.sql_file}: an append model inserts every row it reads, so its filter must "
            f"read each row once: write WHERE {column} > $since (not >=, no expression around "
            f"$since, no OR {column} IS NULL), in queries that return {column} unchanged and "
            "keep every row they read (no LIMIT, OFFSET, USING SAMPLE, DISTINCT ON, QUALIFY, "
            "window function or row-choosing subquery below the filter). Use strategy merge when "
            "rows can be read again, or for the latest row per key.",
            model.sql_file,
            model=model.name,
        )
    return problem(
        "INVALID_MODEL_SQL",
        f"{model.sql_file}: in a partition_overwrite model, $since may only set a lower bound of "
        f"the time_column {column}, as a WHERE condition joined by AND, in queries that return "
        f"{column} unchanged and keep their rows (for example WHERE {column} >= "
        "date_trunc('month', $since)), so the run can see any partition it loads in part.",
        model.sql_file,
        model=model.name,
    )
