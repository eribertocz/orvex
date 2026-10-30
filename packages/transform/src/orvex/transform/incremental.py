"""Incremental models: checks and run steps for every strategy (P1-11, P1-12).

The loaded rows (what the model's SELECT returns in this run) are first written to a staging
table inside the run's transaction; checks run on it; the target's columns are changed when the
model's `on_schema_change` allows it; then the target is created or written, and the staging
table is dropped — all in the same transaction, so a failure anywhere leaves the target (data and
columns) as it was.

Every strategy is "delete the target rows the loaded rows replace, insert the loaded rows":
- `append` deletes nothing;
- `merge` deletes the rows whose key is loaded (keys must be unique in the loaded rows), which
  equals updating every column of matched keys and inserting the rest, without relying on MERGE
  support in the lake format;
- `delete_insert` deletes the rows whose key is loaded (a key's rows are replaced as a set);
- `partition_overwrite` deletes the rows whose partition is loaded (`NULL` is a partition).
Rows are inserted by column name, so a target whose columns were added at the end still matches.

A `partition_overwrite` run filtered by `$since` can load only part of a partition, and the
delete would then lose the rest. When the table holds rows of a loaded partition older than its
oldest loaded row, or undated rows the run does not reload, the run fails with
`PARTITION_INCOMPLETE` (#155, decision #128 option B). Its `$since` filter must compare
`time_column` itself, as a lower bound, which validation checks (`since_is_a_lower_bound`).

Which rows are loaded (P1-14): the model's SQL uses `$since`, bound as a typed parameter to the
previous watermark minus `lookback` (`-infinity` on a first run or a full refresh). The new
watermark is the largest `time_column` of the loaded rows (never less than the previous one),
computed from the staging table and stored in `_meta.watermarks` inside the run transaction, so
it commits or rolls back with the rows — never the clock, and never a separate write.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import duckdb
from orvex.contracts.model_config.v1 import ModelError
from orvex.contracts.store.v1 import Parameter, ParameterType, ReadRequest, StatementRequest
from orvex.transform.models import Model, problem

STAGE_SCHEMA = "_stage"
TEMPORAL_TYPES = frozenset({"DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE"})
SINCE = "since"
WATERMARKS = "_meta.watermarks"
LOWEST = "-infinity"
# The store's parameter type for each temporal output type.
PARAMETER_TYPES = {
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ",
}
_DURATION = re.compile(r"([0-9]+)([smhd])")
_UNIT_MICROSECONDS = {"s": 1_000_000, "m": 60_000_000, "h": 3_600_000_000, "d": 86_400_000_000}
KEYED_STRATEGIES = frozenset({"merge", "delete_insert"})

# Type changes that keep every existing value exactly (sync_all_columns); nothing else is applied.
_SIGNED_INTEGERS = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT")
_UNSIGNED_INTEGERS = ("UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT")
_DECIMAL = re.compile(r"DECIMAL\((\d+),(\d+)\)")

type Columns = Sequence[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Check:
    """A value that must be zero; otherwise the run fails with `code`.

    With `detail`, the SQL returns `'0'` or a text describing what failed, which replaces
    `{detail}` in `message`. `parameters` are bound to the SQL (never formatted into it).
    """

    sql: str
    code: str
    message: str
    parameters: tuple[Parameter, ...] = ()
    detail: bool = False

    def failure(self, value: str) -> str:
        """The message for a failing value."""
        return self.message.replace("{detail}", value) if self.detail else self.message


@dataclass(frozen=True, slots=True)
class Count:
    """The number of loaded rows, recorded as rows read (and written)."""

    sql: str


@dataclass(frozen=True, slots=True)
class Refuse:
    """The model cannot run as declared; the run fails with `code` before writing anything."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Watermark:
    """The model's new watermark, as exact text: the larger of the previous one and the loaded
    rows' largest `time_column` (None when there is neither). The runner stores it with
    `watermark_statements`, in the same transaction."""

    request: StatementRequest
    model: str
    reset: bool  # a full refresh: the previous watermark is dropped even if nothing loads


type Step = StatementRequest | Check | Count | Refuse | Watermark


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _statement(sql: str, parameters: Sequence[Parameter] = ()) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=list(parameters))


def uses_since(sql: str) -> bool:
    """Whether the SQL references the `$since` parameter (DuckDB's parser; text in strings and
    comments does not count)."""
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error:
        return False
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
    connection = duckdb.connect()  # parses only: no files, no extensions
    try:
        connection.execute("SET enable_external_access = false")
        connection.execute("SET autoload_known_extensions = false")
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
                if not _consistent(name) or name in _UNSTABLE_FUNCTIONS or _aggregate(name):
                    # An aggregate's value can change when rows join its group, or with the
                    # order rows arrive in (`list(t)[1]`, `arg_max`): refused (pass 3).
                    return refused
            if kind == "COLUMN_REF":
                parts = column_parts(node)
                if not parts or parts[-1] in _SPECIAL_NAMES:
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
            states = [
                from_state(source.get("left"), name, relation, scope),
                from_state(source.get("right"), name, relation, scope),
            ]
            if refused in states:
                return refused
            return accepted if accepted in states else absent
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
        `LIMIT`/`OFFSET`, sample, `DISTINCT ON`, `QUALIFY`, nor a subquery in `WHERE` or `HAVING`
        that chooses rows by reading others. An `append` model never reads again a row it left
        out at or before the watermark."""
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
        if any(item.get("class") == "WINDOW" for item in nodes(node.get("select_list"))):
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
        columns, through aliases, `REPLACE`, stars, subqueries, CTEs and joins, and computed only
        by functions DuckDB marks `CONSISTENT` (less a fixed list it marks wrongly). Anything
        not traced is refused: column lists that rename by position, struct fields, `RENAME`,
        `COLUMNS(…)`, table functions, `VALUES`, set operations below the model's query."""
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
    connection = duckdb.connect()
    try:
        connection.execute("SET enable_external_access = false")
        connection.execute("SET autoload_known_extensions = false")
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


# Functions DuckDB marks `CONSISTENT` whose value still changes between runs (the session's
# clock, the installed version) or depends on the order rows arrive in (#135, Reviewer pass 2).
_UNSTABLE_FUNCTIONS = frozenset(
    {
        "current_localtime",
        "current_localtimestamp",
        "version",
        "any_value",
        "arbitrary",
        "first",
        "last",
        "first_value",
        "last_value",
        "nth_value",
    }
)
# Names DuckDB reads as functions of the moment although they parse as column references.
_SPECIAL_NAMES = frozenset(
    {"current_timestamp", "current_date", "current_time", "localtimestamp", "localtime"}
)


@functools.cache
def _consistent_functions() -> frozenset[str]:
    """The functions whose every overload DuckDB marks `CONSISTENT`: the same arguments give the
    same value in every query (not `now()`, `random()`, `nextval()`, …)."""
    connection = duckdb.connect()
    try:
        row = connection.execute(
            "SELECT string_agg(name, chr(10)) FROM (SELECT lower(function_name) AS name "
            "FROM duckdb_functions() GROUP BY 1 "
            "HAVING bool_and(coalesce(stability = 'CONSISTENT', false)))"
        ).fetchone()
    finally:
        connection.close()
    return frozenset(str(row[0]).split("\n")) if row and row[0] is not None else frozenset()


def _consistent(name: str) -> bool:
    return name.lower() in _consistent_functions()


@functools.cache
def _aggregate_functions() -> frozenset[str]:
    """The names DuckDB lists as aggregate functions (some are window functions too)."""
    connection = duckdb.connect()
    try:
        row = connection.execute(
            "SELECT string_agg(DISTINCT lower(function_name), chr(10)) FROM duckdb_functions() "
            "WHERE function_type = 'aggregate'"
        ).fetchone()
    finally:
        connection.close()
    return frozenset(str(row[0]).split("\n")) if row and row[0] is not None else frozenset()


def _aggregate(name: str) -> bool:
    return name.lower() in _aggregate_functions()


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
        or model.extraction
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
            "keep every row they read (no LIMIT, DISTINCT ON, QUALIFY, window function or "
            "row-choosing subquery below the filter). Use strategy merge when rows can be read "
            "again, or for the latest row per key.",
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


def config_problems(model: Model) -> list[ModelError]:
    """Rules that need only the sidecar."""
    config = model.config
    incremental = config.incremental
    if config.materialized != "incremental" or incremental is None:
        return []
    file = model.config_file or model.sql_file
    found: list[ModelError] = []
    strategy = incremental.strategy
    if strategy in KEYED_STRATEGIES and not incremental.unique_key:
        found.append(
            problem(
                "MISSING_UNIQUE_KEY",
                f"{file}: strategy {strategy} needs unique_key.",
                file,
                model=model.name,
                path="/incremental",
            )
        )
    if strategy == "partition_overwrite" and not incremental.partition_by:
        found.append(
            problem(
                "MISSING_PARTITION_BY",
                f"{file}: strategy partition_overwrite needs partition_by.",
                file,
                model=model.name,
                path="/incremental",
            )
        )
    automatic = (incremental.filter or "auto") == "auto"
    # Extraction models are ClickHouse SQL: their `{since:Type}` is checked by
    # `clickhouse.since_problems`, never by DuckDB's parser.
    if not automatic and not model.extraction and not uses_since(model.sql):
        found.append(
            problem(
                "MISSING_SINCE_PARAMETER",
                f"{model.sql_file}: filter: manual needs $since in the SQL, where the "
                "incremental filter belongs (for example WHERE <time_column> > $since).",
                model.sql_file,
                model=model.name,
            )
        )
    elif (bounded := since_bound_problem(model)) is not None:
        found.append(bounded)
    elif automatic and model.extraction:
        found.append(
            problem(
                "INCREMENTAL_FILTER_NOT_PROVABLE",
                f"{file}: the engine does not read ClickHouse SQL, so it cannot place the "
                "filter (reason: extraction); set filter: manual and use {since:Type}.",
                file,
                model=model.name,
                path="/incremental/filter",
            )
        )
    elif automatic and not model.extraction and uses_since(model.sql):
        found.append(
            problem(
                "MODEL_CONFIG_INVALID",
                f"{file}: the SQL already uses $since: with filter: auto the engine places the "
                "filter itself; set filter: manual to keep yours.",
                file,
                model=model.name,
                path="/incremental/filter",
            )
        )
    if strategy not in KEYED_STRATEGIES and lookback_microseconds(incremental.lookback) > 0:
        found.append(
            problem(
                "LOOKBACK_REQUIRES_UNIQUE_KEY",
                f"{file}: {strategy} with lookback would not replace the re-read rows by key; use "
                "merge or delete_insert with a unique_key, or lookback: 0s.",
                file,
                model=model.name,
                path="/incremental/lookback",
            )
        )
    return found


def column_problems(model: Model, columns: Columns) -> list[ModelError]:
    """Rules that need the model's output columns (from the binder, nothing executed)."""
    incremental = model.config.incremental
    if model.config.materialized != "incremental" or incremental is None:
        return []
    file = model.config_file or model.sql_file
    types = {name.lower(): kind for name, kind in columns}
    found: list[ModelError] = []
    time_column = incremental.time_column.root
    kind = types.get(time_column.lower())
    if kind not in TEMPORAL_TYPES:
        what = "is not an output column" if kind is None else f"is {kind}"
        found.append(
            problem(
                "TIME_COLUMN_NOT_TEMPORAL",
                f"{file}: time_column {time_column} {what}; it must be DATE, TIMESTAMP or "
                "TIMESTAMP WITH TIME ZONE.",
                file,
                model=model.name,
                path="/incremental/time_column",
            )
        )
    for field, listed in (
        ("unique_key", incremental.unique_key),
        ("partition_by", incremental.partition_by),
    ):
        for index, column in enumerate(listed or []):
            if column.root.lower() not in types:
                found.append(
                    problem(
                        "MODEL_CONFIG_INVALID",
                        f"{file}: {field} column {column.root} is not an output column.",
                        file,
                        model=model.name,
                        path=f"/incremental/{field}/{index}",
                    )
                )
    return found


def schema_change(model: Model, output: Columns, existing: Columns | None) -> str | None:
    """Why the output no longer matches the existing target, or None."""
    if existing is None:
        return None
    new = [(name.lower(), kind) for name, kind in output]
    old = [(name.lower(), kind) for name, kind in existing]
    if new == old:
        return None
    added = [name for name, _ in new if name not in dict(old)]
    removed = [name for name, _ in old if name not in dict(new)]
    retyped = [
        f"{name} {dict(old)[name]} → {kind}"
        for name, kind in new
        if name in dict(old) and dict(old)[name] != kind
    ]
    parts = []
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    if retyped:
        parts.append("changed " + ", ".join(retyped))
    return "; ".join(parts) or "the column order changed"


def lookback_microseconds(lookback: str | None) -> int:
    match = _DURATION.fullmatch(lookback or "0s")
    assert match is not None  # the contract's Duration pattern
    return int(match.group(1)) * _UNIT_MICROSECONDS[match.group(2)]


def since_value(kind: str, watermark: str | None, lookback: str | None) -> str:
    """`$since` for a time column of DuckDB type `kind`: the previous watermark minus `lookback`
    (whole days for DATE, rounded down), or `-infinity` when there is no watermark.

    Integer arithmetic on dates and microseconds: exact, and no clock is read. A watermark this
    code cannot read (a year outside 1-9999) is returned unchanged; the store then refuses it as
    a parameter, so the run fails instead of loading the wrong rows.
    """
    if watermark is None:
        return LOWEST
    if watermark in {"infinity", "-infinity"}:
        return watermark
    back = lookback_microseconds(lookback)
    try:
        if kind == "DATE":
            day = dt.date.fromisoformat(watermark)
            return (day - dt.timedelta(days=back // _UNIT_MICROSECONDS["d"])).isoformat()
        moment = dt.datetime.fromisoformat(watermark) - dt.timedelta(microseconds=back)
    except OverflowError:
        return LOWEST  # before year 1: every row is newer
    except ValueError:
        return watermark
    if kind == "TIMESTAMP WITH TIME ZONE":
        if moment.tzinfo is None:
            return watermark  # no offset: never read in the machine's zone; the store refuses it
        moment = moment.astimezone(dt.UTC)
    return moment.isoformat(timespec="microseconds")


def _as_text(value: str, kind: str) -> str:
    """A temporal value as the store's parameter text (ISO 8601; TIMESTAMPTZ in UTC with
    `+00:00`; `infinity` and `-infinity` as such)."""
    if kind == "DATE":
        text = f"strftime({value}, '%Y-%m-%d')"
    elif kind == "TIMESTAMP":
        text = f"strftime({value}, '%Y-%m-%dT%H:%M:%S.%f')"
    else:
        text = f"strftime(timezone('UTC', {value}), '%Y-%m-%dT%H:%M:%S.%f') || '+00:00'"
    return f"CASE WHEN isinf({value}) THEN CAST({value} AS VARCHAR) ELSE {text} END"


def _watermark(model: Model, column: str, kind: str, stage: str, previous: str | None) -> Watermark:
    loaded = f"SELECT max({column}) AS m FROM {stage}"
    if previous is None:
        sql, parameters = f"SELECT {_as_text('m', kind)} FROM ({loaded})", []
    else:
        # Re-read rows (lookback) may all be older than the previous watermark: never go back.
        sql = (
            f"SELECT {_as_text('w', kind)} FROM (SELECT CASE WHEN m IS NULL OR m < $p_previous "
            f"THEN $p_previous ELSE m END AS w FROM ({loaded}))"
        )
        parameters = [
            Parameter(name="p_previous", type=ParameterType(PARAMETER_TYPES[kind]), value=previous)
        ]
    return Watermark(_statement(sql, parameters), model.name, previous is None)


def watermark_statements(step: Watermark, value: str | None) -> list[StatementRequest]:
    """Statements that store a model's new watermark (run in the run transaction)."""
    if value is None and not step.reset:
        return []
    model = Parameter(name="p_model", type=ParameterType("VARCHAR"), value=step.model)
    statements = [
        _statement("CREATE SCHEMA IF NOT EXISTS _meta"),
        _statement(f"CREATE TABLE IF NOT EXISTS {WATERMARKS} (model VARCHAR, watermark VARCHAR)"),
        _statement(f"DELETE FROM {WATERMARKS} WHERE model = $p_model", [model]),
    ]
    if value is not None:
        text = Parameter(name="p_watermark", type=ParameterType("VARCHAR"), value=value)
        statements.append(
            _statement(f"INSERT INTO {WATERMARKS} VALUES ($p_model, $p_watermark)", [model, text])
        )
    return statements


def watermark_request(snapshot: int, model: Model) -> ReadRequest:
    """The model's stored watermark (no row: none stored)."""
    return ReadRequest(
        kind="read",
        sql=f"SELECT watermark FROM {WATERMARKS} WHERE model = $p_model",
        parameters=[Parameter(name="p_model", type=ParameterType("VARCHAR"), value=model.name)],
        snapshot=snapshot,
        batch_size=1,
    )


def table_watermark_request(snapshot: int, model: Model, kind: str) -> ReadRequest:
    """The largest `time_column` already in the model's table: the watermark of a table written
    before watermarks were stored (so it is never reloaded from `-infinity`)."""
    incremental = model.config.incremental
    assert incremental is not None
    column = _identifier(incremental.time_column.root)
    return ReadRequest(
        kind="read",
        sql=(
            f"SELECT {_as_text('m', kind)} AS watermark FROM "
            f"(SELECT max({column}) AS m FROM {_target(model)})"
        ),
        parameters=[],
        snapshot=snapshot,
        batch_size=1,
    )


def widens(old: str, new: str) -> bool:
    """True when every value of type `old` casts to `new` exactly (and back)."""
    for family in (_SIGNED_INTEGERS, _UNSIGNED_INTEGERS):
        if old in family and new in family:
            return family.index(new) > family.index(old)
    old_decimal, new_decimal = _DECIMAL.fullmatch(old), _DECIMAL.fullmatch(new)
    if old_decimal and new_decimal:
        old_precision, old_scale = map(int, old_decimal.groups())
        new_precision, new_scale = map(int, new_decimal.groups())
        return new_scale == old_scale and new_precision > old_precision
    return old == "DATE" and new == "TIMESTAMP"


def schema_statements(model: Model, output: Columns, existing: Columns) -> list[str] | str:
    """The `ALTER TABLE` statements that make the target take the output's columns under the
    model's `on_schema_change`, or why it cannot (the run then fails with SCHEMA_CHANGED)."""
    changed = schema_change(model, output, existing)
    if changed is None:
        return []
    policy = model.config.on_schema_change or "fail"
    if policy == "fail":
        return changed
    target = _target(model)
    old = {name.lower(): (name, kind) for name, kind in existing}
    new = {name.lower(): (name, kind) for name, kind in output}
    added = [new[key] for key in new if key not in old]
    removed = [old[key] for key in old if key not in new]
    retyped = [(old[key], new[key][1]) for key in new if key in old and old[key][1] != new[key][1]]
    if policy == "append_new_columns" and (removed or retyped):
        return changed + "; append_new_columns only adds columns"
    refused = [f"{name} {kind} → {to}" for (name, kind), to in retyped if not widens(kind, to)]
    if refused:
        return "cannot change " + ", ".join(refused) + " without changing values"
    # Added first, dropped last: the table never has zero columns.
    return (
        [f"ALTER TABLE {target} ADD COLUMN {_identifier(name)} {kind}" for name, kind in added]
        + [
            f"ALTER TABLE {target} ALTER COLUMN {_identifier(name)} SET DATA TYPE {to}"
            for (name, _), to in retyped
        ]
        + [f"ALTER TABLE {target} DROP COLUMN {_identifier(name)}" for name, _ in removed]
    )


def _target(model: Model) -> str:
    return f"{_identifier(model.schema)}.{_identifier(model.table)}"


def steps(
    model: Model,
    sql: str,
    output: Columns,
    existing: Columns | None,
    since: str = LOWEST,
    full_refresh: bool = False,
    previous: str | None = None,
    filtered: bool | None = None,
) -> list[Step]:
    """The run steps of one incremental model.

    `existing`: its table's columns (None if absent); `since`: the `$since` value (text of the
    time column's type); `full_refresh`: replace the table with every loaded row; `previous`:
    the stored watermark (ignored on a full refresh); `filtered`: whether the loaded rows were
    selected by `$since` (default: whether `sql` uses it; an extraction model's rows were
    filtered on ClickHouse, so its SQL here does not).
    """
    incremental = model.config.incremental
    assert incremental is not None  # only incremental models get here
    strategy = incremental.strategy
    policy = model.config.on_schema_change or "fail"
    time_column = incremental.time_column.root
    kind = {name.lower(): kind for name, kind in output}.get(time_column.lower(), "")
    if kind not in PARAMETER_TYPES:
        return [
            Refuse(
                "TIME_COLUMN_NOT_TEMPORAL",
                f"{model.name}: time_column {time_column} must be DATE, TIMESTAMP or "
                "TIMESTAMP WITH TIME ZONE.",
            )
        ]
    if full_refresh:
        existing, previous = None, None
    alterations: list[str] = []
    if existing is not None:
        planned = schema_statements(model, output, existing)
        if isinstance(planned, str):
            return [
                Refuse(
                    "SCHEMA_CHANGED",
                    f"{model.name}: the output no longer matches the table ({planned}); with "
                    f"on_schema_change: {policy} nothing is written.",
                )
            ]
        alterations = planned
    target = _target(model)
    stage_table = f"{model.schema}__{model.table}"
    stage = f"{STAGE_SCHEMA}.{_identifier(stage_table)}"
    parameters = (
        [Parameter(name=SINCE, type=ParameterType(PARAMETER_TYPES[kind]), value=since)]
        if uses_since(sql)
        else []
    )
    result: list[Step] = [
        _statement(f"CREATE SCHEMA IF NOT EXISTS {STAGE_SCHEMA}"),
        _statement(f"CREATE OR REPLACE TABLE {stage} AS {sql}", parameters),
        Count(f"SELECT count(*) FROM {stage}"),
        _watermark(model, _identifier(time_column), kind, stage, previous),
    ]
    keys = [_identifier(key.root) for key in incremental.unique_key or []]
    listed = ", ".join(keys)
    if strategy in KEYED_STRATEGIES:
        any_null = " OR ".join(f"{key} IS NULL" for key in keys)
        result.append(
            Check(
                f"SELECT count(*) FROM {stage} WHERE {any_null}",
                "UNIQUE_KEY_NULL",
                f"{model.name}: loaded rows have a NULL in unique_key ({listed}).",
            )
        )
    if strategy == "merge":
        result.append(
            Check(
                f"SELECT count(*) FROM (SELECT {listed} FROM {stage} "
                f"GROUP BY {listed} HAVING count(*) > 1)",
                "UNIQUE_KEY_DUPLICATED",
                f"{model.name}: loaded rows repeat a unique_key ({listed}); the result would "
                "depend on row order.",
            )
        )
    if existing is None:
        result += [
            _statement(f"CREATE SCHEMA IF NOT EXISTS {_identifier(model.schema)}"),
            # A full refresh replaces the table; a first run creates it.
            _statement(f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {stage}"),
        ]
    else:
        result += [_statement(statement) for statement in alterations]
        # The columns were compared at validation; compare again on the loaded rows, inside the
        # transaction, after any column change, so a source that changed since validation cannot
        # be written. Under `fail` the order must match too; otherwise columns are compared by
        # name, since added columns go to the end of the table (rows are inserted by name).
        result.append(
            Check(
                _same_columns_sql(stage_table, model, ordered=policy == "fail"),
                "SCHEMA_CHANGED",
                f"{model.name}: the loaded rows' columns no longer match the table; nothing is "
                "written.",
            )
        )
        # Only on an existing table (this branch): with no watermark yet `$since` is -infinity,
        # no row is older, but undated rows the filter never reloads are still protected.
        by_since = bool(parameters) if filtered is None else filtered
        if strategy == "partition_overwrite" and by_since:
            result.append(_cut_partitions(model, target, stage, output))
        replaced = _replaced(model, target)
        if replaced is not None:
            result.append(
                _statement(
                    f"DELETE FROM {target} WHERE EXISTS "
                    f"(SELECT 1 FROM {stage} AS loaded WHERE {replaced})"
                )
            )
        result.append(_statement(f"INSERT INTO {target} BY NAME SELECT * FROM {stage}"))
    result += [
        _statement(f"DROP TABLE {stage}"),
        _statement(f"DROP SCHEMA IF EXISTS {STAGE_SCHEMA}"),
    ]
    return result


_CUT_SHOWN = 5  # partitions named in the PARTITION_INCOMPLETE message


_LABEL_LENGTH = 60  # characters of one partition value shown in the message
_TEXT_TYPES = frozenset({"VARCHAR"})
_QUOTE = "''''"  # a SQL string literal holding one single quote


def _label(alias: str, kind: str) -> str:
    """A partition value as message text: `NULL` for SQL NULL, text values quoted (so the text
    `NULL` reads `'NULL'`), values longer than `_LABEL_LENGTH` characters shortened (before
    quoting, so a shortened text value keeps its closing quote)."""
    text = f"CAST({alias} AS VARCHAR)"
    shown = (
        f"CASE WHEN length({text}) > {_LABEL_LENGTH} "
        f"THEN left({text}, {_LABEL_LENGTH}) || '…' ELSE {text} END"
    )
    if kind in _TEXT_TYPES or kind.startswith("ENUM("):
        shown = f"{_QUOTE} || replace({shown}, {_QUOTE}, '''''') || {_QUOTE}"
    return f"coalesce({shown}, 'NULL')"


def _cut_partitions(model: Model, target: str, stage: str, output: Columns) -> Check:
    """Fails when the `$since` filter loaded only part of a loaded partition (#155, decision
    #128 option B; #109 before it).

    A loaded partition is cut when the table holds dated rows of it older than the oldest dated
    loaded row of it (or the loaded rows of it have no dated row), or holds rows of it with a
    `NULL` time column while the loaded rows have none (a `$since` filter never reloads those).
    With the filter a lower bound of `time_column` (validation, `since_is_a_lower_bound`), a
    run loads every row of a partition from some time on, so a partition it loads in part has
    older rows left in the table (`> $since`, `>= $since`, a lookback). Partitions compare with
    `IS NOT DISTINCT FROM`, as the delete does, so `NULL` is a partition. Rows with the same
    time as the oldest loaded row but not reloaded are not detected: with the filter a lower
    bound, only another condition of the WHERE clause could leave them out.

    It also fails when the source itself no longer has a partition's oldest rows (then
    `--full-refresh` reloads it).

    One grouped pass over the loaded rows and one over the table's rows of loaded partitions.
    Partition columns are renamed `p0`, `p1`, … inside the query, so no column name can collide
    with the query's own names. The result is `'0'`, or up to five cut partitions, ordered by
    value, and their total.
    """
    incremental = model.config.incremental
    assert incremental is not None
    assert incremental.partition_by
    types = {name.lower(): kind for name, kind in output}
    columns = [_identifier(column.root) for column in incremental.partition_by]
    aliases = [f"p{index}" for index in range(len(columns))]
    pairs = list(zip(columns, aliases, strict=True))
    renamed = ", ".join(f"{column} AS {alias}" for column, alias in pairs)
    time = _identifier(incremental.time_column.root)

    def matches(left: str, left_names: list[str]) -> str:
        return " AND ".join(
            f"{left}.{name} IS NOT DISTINCT FROM loaded.{alias}"
            for name, alias in zip(left_names, aliases, strict=True)
        )

    # `min` skips NULL times: `oldest` is NULL when a partition has no dated row.
    counted = f"min({time}) AS oldest, count(*) FILTER (WHERE {time} IS NULL) AS n_undated"
    labels = [
        _label(f"loaded.{alias}", types.get(column.root.lower(), ""))
        for column, alias in zip(incremental.partition_by, aliases, strict=True)
    ]
    label = labels[0] if len(labels) == 1 else "'(' || " + " || ', ' || ".join(labels) + " || ')'"
    order = ", ".join(f"loaded.{alias} NULLS LAST" for alias in aliases)
    sql = (
        f"WITH loaded AS (SELECT {renamed}, {counted} FROM {stage} GROUP BY ALL), "
        f"held AS (SELECT {renamed}, {counted} FROM {target} AS row_held "
        f"WHERE EXISTS (SELECT 1 FROM loaded WHERE {matches('row_held', columns)}) "
        "GROUP BY ALL), "
        f"cut AS (SELECT {label} AS cut_label, row_number() OVER (ORDER BY {order}) AS cut_rank "
        f"FROM loaded JOIN held ON {matches('held', aliases)} "
        "WHERE (held.oldest IS NOT NULL "
        "AND (loaded.oldest IS NULL OR held.oldest < loaded.oldest)) "
        "OR (held.n_undated > 0 AND loaded.n_undated = 0)) "
        "SELECT CASE WHEN count(*) = 0 THEN '0' ELSE "
        f"string_agg(cut_label, ', ' ORDER BY cut_rank) FILTER (WHERE cut_rank <= {_CUT_SHOWN}) "
        "|| ' (' || count(*) || ' in total)' END FROM cut"
    )
    name = incremental.time_column.root
    partition_by = ", ".join(column.root for column in incremental.partition_by)
    return Check(
        sql,
        "PARTITION_INCOMPLETE",
        f"{model.name}: the $since filter loaded only part of these partitions ({partition_by}): "
        "{detail}. The table holds rows of them older than the oldest row this run loaded of "
        f"them, or with a NULL {name}, and partition_overwrite would delete them; nothing is "
        "written. Load whole partitions, for example "
        f"WHERE {name} >= date_trunc('month', $since) for monthly partitions; if the source "
        "itself no longer has those rows, use --full-refresh.",
        (),
        detail=True,
    )


def _replaced(model: Model, target: str) -> str | None:
    """How a target row matches a loaded row it is replaced by (None: nothing is replaced)."""
    incremental = model.config.incremental
    assert incremental is not None
    if incremental.strategy in KEYED_STRATEGIES:
        # Keys were checked to have no NULL part, so `=` is exact.
        columns, match = incremental.unique_key or [], "="
    elif incremental.strategy == "partition_overwrite":
        columns, match = incremental.partition_by or [], "IS NOT DISTINCT FROM"
    else:
        return None
    return " AND ".join(
        f"{target}.{_identifier(column.root)} {match} loaded.{_identifier(column.root)}"
        for column in columns
    )


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _columns_sql(schema: str, table: str, ordered: bool) -> str:
    order = "ordinal_position" if ordered else "lower(column_name)"
    return (
        "(SELECT string_agg(lower(column_name) || ' ' || data_type, ', ' "
        f"ORDER BY {order}) "
        "FROM information_schema.columns WHERE table_catalog = current_database() "
        f"AND table_schema = {_literal(schema)} AND table_name = {_literal(table)})"
    )


def _same_columns_sql(stage_table: str, model: Model, ordered: bool) -> str:
    """0 only when the stage and the target both exist with the same columns (in order, when
    `ordered`; otherwise by name)."""
    # Both sides must be found: two missing lookups would otherwise compare equal.
    return (
        f"SELECT CASE WHEN {_columns_sql(STAGE_SCHEMA, stage_table, ordered)} = "
        f"{_columns_sql(model.schema, model.table, ordered)} THEN 0 ELSE 1 END"
    )


def problems_for(models: Sequence[Model], columns: Mapping[str, Columns]) -> list[ModelError]:
    """Every incremental rule for the models whose output columns are known."""
    found: list[ModelError] = []
    for model in models:
        if model.name in columns:
            found += column_problems(model, columns[model.name])
    return found
