"""The automatic incremental filter: proven, then injected (P1-13).

With `filter: auto`, an incremental model loads only rows newer than its watermark without the
Builder writing the filter — but only when DuckDB's parse tree proves the filter is safe: the
`time_column` comes, unchanged, from exactly one base table, and nothing between that table and
the output can change which rows a filter on it removes (aggregation, windows, DISTINCT, set
operations, LIMIT, sampling, or the null-supplying side of an outer join). Subqueries in `FROM`
and CTEs are followed. When proven, `<table>.<column> > $since` is added to the WHERE of the
query that reads that table, by rewriting the parse tree; the `.sql` file is never changed.
When not proven, the reason is reported and nothing is guessed.
"""

from __future__ import annotations

import copy
import functools
import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

import duckdb

type Tree = dict[str, Any]
type ColumnsOf = Callable[[list[str]], Collection[str] | None]

SINCE = "since"
LAKE_CATALOG = "lake"


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why the filter cannot be proven, and where the time column was found (0-based offset in
    the SQL), when it was."""

    reason: str
    location: int | None = None


@dataclass(frozen=True, slots=True)
class _Found:
    """The query that reads the base table, the table's reference node, and the column."""

    query: Tree
    table: Tree
    column: str


def _parser() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    connection.execute("SET enable_external_access = false")
    connection.execute("SET autoload_known_extensions = false")
    return connection


@functools.cache
def _aggregates() -> frozenset[str]:
    connection = _parser()
    try:
        rows = connection.execute(
            "SELECT DISTINCT function_name FROM duckdb_functions() "
            "WHERE function_type = 'aggregate'"
        ).fetchall()
    finally:
        connection.close()
    return frozenset(str(row[0]).lower() for row in rows)


def _walk(node: object) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    pending = [node]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            found.append(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return found


def _expressions_only(node: Tree) -> list[dict[str, Any]]:
    """Every expression node of a SELECT's own clauses (not of subqueries in its FROM)."""
    parts = [
        node.get("select_list"),
        node.get("where_clause"),
        node.get("having"),
        node.get("qualify"),
    ]
    return _walk(parts)


def _reason_in_query(node: Tree) -> str | None:
    """Why a SELECT changes which rows a filter below it would remove, or None."""
    if node.get("type") != "SELECT_NODE":
        return "set operation"
    if node.get("group_expressions") or node.get("group_sets") or node.get("having"):
        return "aggregation"
    if node.get("aggregate_handling") not in (None, "STANDARD_HANDLING"):
        return "aggregation"
    if node.get("qualify"):
        return "window"
    if node.get("sample"):
        return "limit"
    for modifier in node.get("modifiers") or []:
        kind = modifier.get("type")
        if kind in {"LIMIT_MODIFIER", "LIMIT_PERCENT_MODIFIER"}:
            return "limit"
        if kind == "DISTINCT_MODIFIER":
            return "distinct"
    aggregates = _aggregates()
    for part in _walk([node.get("select_list"), node.get("having"), node.get("qualify")]):
        if part.get("class") == "WINDOW":
            return "window"
        if part.get("class") == "SUBQUERY":
            continue
        if (
            part.get("class") in {"FUNCTION", "AGGREGATE"}
            and str(part.get("function_name", "")).lower() in aggregates
            and not _inside_subquery(node, part)
        ):
            return "aggregation"
    return None


def _inside_subquery(node: Tree, target: dict[str, Any]) -> bool:
    """Whether `target` belongs to a subquery expression of `node`'s own clauses."""
    for part in _walk([node.get("select_list"), node.get("having"), node.get("qualify")]):
        if part.get("class") == "SUBQUERY" and any(sub is target for sub in _walk(part)):
            return True
    return False


def _output_name(item: Tree) -> str | None:
    if item.get("alias"):
        return str(item["alias"])
    if item.get("class") == "COLUMN_REF" and item.get("column_names"):
        return str(item["column_names"][-1])
    return None


def _leaves(table: Tree) -> list[tuple[Tree, list[Tree]]]:
    """Each table reference of a FROM clause with the joins above it (outermost first)."""
    if table.get("type") == "JOIN":
        left = [(leaf, [table, *joins]) for leaf, joins in _leaves(table["left"])]
        right = [(leaf, [table, *joins]) for leaf, joins in _leaves(table["right"])]
        return [*left, *right]
    return [(table, [])]


def _in(table: Tree, leaf: Tree) -> bool:
    return any(found is leaf for found, _ in _leaves(table))


def _join_reason(leaf: Tree, joins: list[Tree]) -> str | None:
    """Why filtering `leaf` before its joins would change the result, or None."""
    for join in joins:
        kind, ref = join.get("join_type"), join.get("ref_type")
        on_left = _in(join["left"], leaf)
        if ref in {"POSITIONAL", "NATURAL"}:
            return "several tables"
        if ref == "ASOF" and not on_left:
            return "outer join"
        if (
            kind in {"OUTER", "FULL"}
            or (kind == "LEFT" and not on_left)
            or (kind == "RIGHT" and on_left)
            or (kind in {"SEMI", "ANTI", "MARK", "SINGLE"} and not on_left)
        ):
            return "outer join"
    return None


def _name(leaf: Tree) -> str:
    return str(leaf.get("alias") or leaf.get("table_name") or "").lower()


def _path(leaf: Tree) -> list[str]:
    qualified = leaf.get("qualified_name")
    raw = qualified.get("path") if isinstance(qualified, dict) else None
    if isinstance(raw, list) and raw:
        parts = [str(part) for part in raw]
    else:
        parts = [
            str(part)
            for part in (leaf.get("catalog_name"), leaf.get("schema_name"), leaf.get("table_name"))
            if part
        ]
    if len(parts) == 3 and parts[0].lower() == LAKE_CATALOG:
        parts = parts[1:]
    return parts


# Expansions whose output names DuckDB takes from the data, not from the SQL text, even under an
# alias: `unnest(<struct>)` and `unlist` name their columns after the struct's fields.
_EXPANDING = frozenset({"unnest", "unlist"})
# DuckDB renames a repeated output name of a subquery or CTE to `<name>_1`, `<name>_2`, …
_RENAMED = re.compile(r".+?(_[0-9]+)+")


def _unnamed(node: Tree) -> Tree | None:
    """A SELECT item whose output name is not known from the SQL text, or None: an expansion,
    a star inside an expression (`COLUMNS(…)`), a positional reference (`#2`, named after the
    column it points to), or an unaliased constant (named after its value)."""
    for item in node.get("select_list") or []:
        parts = _walk(item)
        if any(
            part.get("class") in {"FUNCTION", "AGGREGATE"}
            and str(part.get("function_name", "")).lower() in _EXPANDING
            for part in parts
        ):
            return item
        if item.get("class") != "STAR" and any(part.get("class") == "STAR" for part in parts):
            return item
        if not item.get("alias") and item.get("class") in {"POSITIONAL_REFERENCE", "CONSTANT"}:
            return item
    return None


def _names_reason(node: Tree, column: str) -> Refusal | None:
    """Why the output name `column` of a SELECT may not be the item the SQL text names it."""
    unnamed = _unnamed(node)
    if unnamed is not None:
        return Refusal("expression", unnamed.get("query_location"))
    items = node.get("select_list") or []
    names = [
        name.lower()
        for item in items
        if item.get("class") != "STAR" and (name := _output_name(item)) is not None
    ]
    if len(names) != len(set(names)):
        return Refusal(
            "several tables"
        )  # a repeated name is renamed: which one is read is not proven
    if _RENAMED.fullmatch(column.lower()) and any(item.get("class") == "STAR" for item in items):
        return Refusal("several tables")  # the name may be a renamed copy from the star
    return None


def _query_outputs(node: Tree) -> list[str] | None:
    """The output names of a SELECT, when they are known without the binder."""
    if node.get("type") != "SELECT_NODE" or _unnamed(node) is not None:
        return None
    names = [_output_name(item) for item in node.get("select_list") or []]
    if any(name is None for name in names):
        return None
    known_names = [str(name) for name in names]
    if len({name.lower() for name in known_names}) != len(known_names):
        return None
    return known_names


class _Proof:
    def __init__(self, tree: Tree, columns_of: ColumnsOf) -> None:
        statement = tree["statements"][0]
        self.columns_of = columns_of
        self.ctes: dict[str, Tree] = {}
        self.cte_uses: dict[str, int] = {}
        self.shadowed: set[str] = set()  # CTE names defined in more than one scope
        for node in _walk(statement["node"]):
            cte_map = node.get("cte_map")
            if isinstance(cte_map, dict):
                for entry in cte_map.get("map", []):
                    key = str(entry["key"]).lower()
                    if key in self.ctes:
                        self.shadowed.add(key)
                    self.ctes[key] = entry["value"]
        # `query('…')` and `query_table('…')` read relations by name at run time: their uses of a
        # CTE cannot be counted, so a statement with them is not followed.
        self.dynamic = any(
            str(node.get("function_name", "")).lower() in {"query", "query_table"}
            for node in _walk(statement["node"])
        )
        for node in _walk(statement["node"]):
            if node.get("type") == "BASE_TABLE" and not node.get("schema_name"):
                name = str(node.get("table_name", "")).lower()
                if name in self.ctes:
                    self.cte_uses[name] = self.cte_uses.get(name, 0) + 1

    def trace(self, node: Tree, column: str, depth: int = 0) -> _Found | Refusal:
        if depth > 64:
            return Refusal("expression")
        reason = _reason_in_query(node)
        if reason is not None:
            return Refusal(reason)
        names = _names_reason(node, column)
        if names is not None:
            return names
        item = self._item(node, column)
        if isinstance(item, Refusal):
            return item
        leaves = _leaves(node.get("from_table") or {})
        if item.get("class") == "STAR":
            return self._through_star(node, item, column, leaves, depth)
        if item.get("class") != "COLUMN_REF":
            return Refusal("expression", item.get("query_location"))
        names = [str(name) for name in item.get("column_names", [])]
        if len(names) > 2:
            # `t.struct.field` or `schema.table.column`: not followed (never guessed).
            return Refusal("expression", item.get("query_location"))
        if len(names) == 1:
            return self._unqualified(node, names[0], leaves, depth, item.get("query_location"))
        qualifier = names[-2].lower()
        matching = [(leaf, joins) for leaf, joins in leaves if _name(leaf) == qualifier]
        if len(matching) != 1:
            return Refusal("several tables", item.get("query_location"))
        return self._leaf(node, *matching[0], names[-1], depth)

    def _item(self, node: Tree, column: str) -> Tree | Refusal:
        """The SELECT item that outputs `column` (a star when only a star can)."""
        wanted = column.lower()
        named: list[Tree] = [
            item
            for item in node.get("select_list") or []
            if (_output_name(item) or "").lower() == wanted and item.get("class") != "STAR"
        ]
        stars: list[Tree] = [
            item for item in node.get("select_list") or [] if item.get("class") == "STAR"
        ]
        if len(named) > 1 or (named and stars) or len(stars) > 1:
            # The name could be output more than once: which one a reader gets is not proven.
            located = (named or stars)[0]
            return Refusal("several tables", located.get("query_location"))
        if len(named) == 1:
            return named[0]
        for star in stars:
            if star.get("columns") or star.get("expr") or star.get("rename_list"):
                return Refusal("expression", star.get("query_location"))
            excluded = {str(name).lower() for name in star.get("exclude_list") or []}
            # A qualified EXCLUDE may name any relation: conservatively, the name is excluded.
            excluded |= {
                str(entry.get("column", "")).lower()
                for entry in star.get("qualified_exclude_list") or []
                if isinstance(entry, dict)
            }
            replaced = {
                str(entry.get("key", "")).lower()
                for entry in star.get("replace_list") or []
                if isinstance(entry, dict)
            }
            if wanted in replaced:
                return Refusal("expression", star.get("query_location"))
            if wanted not in excluded:
                return star
        return Refusal("not found")

    def _through_star(
        self,
        node: Tree,
        star: Tree,
        column: str,
        leaves: list[tuple[Tree, list[Tree]]],
        depth: int,
    ) -> _Found | Refusal:
        relation = str(star.get("relation_name") or "").lower()
        if relation:
            matching = [(leaf, joins) for leaf, joins in leaves if _name(leaf) == relation]
            if len(matching) != 1:
                return Refusal("several tables", star.get("query_location"))
            return self._leaf(node, *matching[0], column, depth)
        return self._unqualified(node, column, leaves, depth, star.get("query_location"))

    def _has(self, leaf: Tree, column: str) -> bool | None:
        """Whether a table reference outputs `column` (None: unknown)."""
        wanted = column.lower()
        if leaf.get("column_name_alias"):
            return None  # renamed by position: not followed
        if leaf.get("type") == "SUBQUERY":
            outputs = _query_outputs(leaf["subquery"]["node"])
        elif leaf.get("type") != "BASE_TABLE":
            return None
        elif not leaf.get("schema_name") and str(leaf.get("table_name", "")).lower() in self.ctes:
            cte = self.ctes[str(leaf.get("table_name", "")).lower()]
            if cte.get("aliases"):
                return None  # renamed by position: not followed
            outputs = _query_outputs(cte["query_node"])
        else:
            columns = self.columns_of(_path(leaf))
            outputs = None if columns is None else list(columns)
        if outputs is None:
            return None
        return wanted in (name.lower() for name in outputs)

    def _unqualified(
        self,
        node: Tree,
        column: str,
        leaves: list[tuple[Tree, list[Tree]]],
        depth: int,
        location: int | None,
    ) -> _Found | Refusal:
        if len(leaves) == 1:
            leaf, joins = leaves[0]
            if leaf.get("type") == "EMPTY":
                return Refusal("not found", location)
            return self._leaf(node, leaf, joins, column, depth)
        having = [(leaf, joins) for leaf, joins in leaves if self._has(leaf, column) is not False]
        if len(having) != 1 or self._has(having[0][0], column) is not True:
            return Refusal("several tables", location)
        return self._leaf(node, *having[0], column, depth)

    def _leaf(
        self, node: Tree, leaf: Tree, joins: list[Tree], column: str, depth: int
    ) -> _Found | Refusal:
        reason = _join_reason(leaf, joins)
        if reason is not None:
            return Refusal(reason)
        same = [other for other, _ in _leaves(node.get("from_table") or {})]
        if sum(1 for other in same if _name(other) == _name(leaf)) != 1:
            return Refusal("several tables")  # two references with one name in this FROM
        if leaf.get("sample"):
            return Refusal("limit")
        if leaf.get("column_name_alias"):
            # `t(a, b)` renames by position: not followed (never guessed).
            return Refusal("expression")
        kind = leaf.get("type")
        if kind == "SUBQUERY":
            return self.trace(leaf["subquery"]["node"], column, depth + 1)
        if kind != "BASE_TABLE":
            return Refusal("several tables")
        name = str(leaf.get("table_name", "")).lower()
        if not leaf.get("schema_name") and name in self.ctes:
            cte = self.ctes[name]
            if self.cte_uses.get(name, 0) != 1 or name in self.shadowed:
                return Refusal("several tables")  # a filter in it would reach every use
            if cte.get("materialized") not in (None, "CTE_MATERIALIZE_DEFAULT"):
                return Refusal("several tables")
            if cte.get("aliases"):
                return Refusal("expression")  # `c(a, b)` renames by position
            return self.trace(cte["query_node"], column, depth + 1)
        columns = self.columns_of(_path(leaf))
        if columns is None:
            return Refusal("several tables")  # the table's columns are not known
        matches = [n for n in columns if n.lower() == column.lower()]
        if len(matches) != 1:
            return Refusal("expression")  # not a column of the table (a lateral alias, say)
        return _Found(node, leaf, matches[0])


def _tree(connection: duckdb.DuckDBPyConnection, sql: str) -> Tree:
    row = connection.execute("SELECT json_serialize_sql($sql)", {"sql": sql}).fetchone()
    tree: Tree = json.loads(str(row[0])) if row else {}
    return tree


def prove(sql: str, time_column: str, columns_of: ColumnsOf) -> Tree | Refusal:
    """The rewritten parse tree with `<table>.<time_column> > $since`, or why not."""
    connection = _parser()
    try:
        tree = _tree(connection, sql)
    finally:
        connection.close()
    if tree.get("error") or len(tree.get("statements", [])) != 1:
        return Refusal("expression")
    rewritten = copy.deepcopy(tree)
    proof = _Proof(rewritten, columns_of)
    if proof.dynamic:
        return Refusal("several tables")
    found = proof.trace(rewritten["statements"][0]["node"], time_column)
    if isinstance(found, Refusal):
        if found.location is None:
            item = _first_item(rewritten["statements"][0]["node"], time_column)
            return Refusal(found.reason, item)
        return found
    _inject(found)
    return rewritten


def _first_item(node: Tree, column: str) -> int | None:
    for item in node.get("select_list") or []:
        if (_output_name(item) or "").lower() == column.lower():
            location = item.get("query_location")
            return int(location) if isinstance(location, int) else None
    return None


def _inject(found: _Found) -> None:
    # The reference's own name in its FROM (its alias, or its table name, which is unique there):
    # it still binds when a view model is inlined as `(…) AS <name>`.
    qualifier = [str(found.table.get("alias") or found.table.get("table_name"))]
    comparison: Tree = {
        "class": "COMPARISON",
        "type": "COMPARE_GREATERTHAN",
        "alias": "",
        "query_location": 18446744073709551615,
        "query_location_length": 0,
        "left": {
            "class": "COLUMN_REF",
            "type": "COLUMN_REF",
            "alias": "",
            "query_location": 18446744073709551615,
            "query_location_length": 0,
            "column_names": [*qualifier, found.column],
        },
        "right": {
            "class": "PARAMETER",
            "type": "VALUE_PARAMETER",
            "alias": "",
            "query_location": 18446744073709551615,
            "query_location_length": 0,
            "identifier": SINCE,
        },
    }
    existing = found.query.get("where_clause")
    if existing:
        found.query["where_clause"] = {
            "class": "CONJUNCTION",
            "type": "CONJUNCTION_AND",
            "alias": "",
            "query_location": 18446744073709551615,
            "query_location_length": 0,
            "children": [existing, comparison],
        }
    else:
        found.query["where_clause"] = comparison


def render(tree: Tree) -> str:
    """The SQL text of a parse tree (DuckDB's own rendering: deterministic)."""
    connection = _parser()
    try:
        row = connection.execute(
            "SELECT json_deserialize_sql($tree)", {"tree": json.dumps(tree)}
        ).fetchone()
    finally:
        connection.close()
    return str(row[0]) if row else ""


def filtered_sql(sql: str, time_column: str, columns_of: ColumnsOf) -> str | Refusal:
    """The model's SQL with the proven filter, or why it cannot be proven."""
    result = prove(sql, time_column, columns_of)
    return result if isinstance(result, Refusal) else render(result)


def line_of(sql: str, location: int | None) -> int | None:
    if location is None or location < 0 or location > len(sql):
        return None
    return sql.count("\n", 0, location) + 1


def known(columns: Mapping[str, Collection[str]]) -> ColumnsOf:
    """A `columns_of` for tests: relation path joined with dots → its columns."""
    return lambda path: columns.get(".".join(path))
