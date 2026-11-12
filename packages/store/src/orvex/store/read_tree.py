"""A read's parse tree: parsed and checked before the read is admitted, walked, and its table
names resolved against the lake's tables and the read's own CTEs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from orvex.store.errors import StoreError
from orvex.store.lake_names import LAKE
from orvex.store.sql_guard import _EXTERNAL_SQL_FUNCTIONS, READ_TABLE_FUNCTIONS
from orvex.store.sql_parser import _parser

_SUSPICIOUS = re.compile(r"[./\\*?:]")


def _read_tree(sql: str) -> dict[str, Any]:
    """A read's parse tree, checked for what needs no catalog: SQL that does not parse,
    functions that run SQL given as text, `SUMMARIZE`/`DESCRIBE`/`SHOW`, and table functions
    other than generators (which could read the lake or its files without a snapshot). Parsed
    on a connection of its own, with no lake attached, so it runs before the read is admitted:
    an invalid read gets its own error on a busy lake (#160)."""
    parser = _parser()
    try:
        serialized = parser.execute("SELECT json_serialize_sql($sql)", {"sql": sql}).fetchone()
    finally:
        parser.close()
    tree: dict[str, Any] = json.loads(str(serialized[0])) if serialized else {"error": True}
    if tree.get("error"):
        raise StoreError(
            "STORE_QUERY_FAILED",
            "The SQL could not be parsed.",
            {"duckdb": str(tree.get("error_message", ""))},
        )
    for node in _walk(tree):
        if (
            node.get("class") == "FUNCTION"
            and str(node.get("function_name", "")).lower() in _EXTERNAL_SQL_FUNCTIONS
        ):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "This function is not allowed in a read.",
                {"function": str(node.get("function_name"))},
            )
        if node.get("type") == "SHOW_REF":
            # SUMMARIZE / DESCRIBE / SHOW: unpinned, and a quoted target is read as a file.
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "SUMMARIZE, DESCRIBE and SHOW are not allowed in statements.",
                {},
            )
        if node.get("type") == "TABLE_FUNCTION":
            function = node.get("function")
            name = function.get("function_name") if isinstance(function, dict) else None
            if str(name).lower() not in READ_TABLE_FUNCTIONS:
                raise StoreError(
                    "STORE_STATEMENT_INVALID",
                    "A read may use only the table functions range, generate_series and unnest.",
                    {"function": str(name)},
                )
    return tree


type JsonTree = dict[str, object]


def _walk(node: object) -> Iterator[dict[str, object]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _scoped(
    node: object, scope: frozenset[str] = frozenset()
) -> Iterator[tuple[dict[str, Any], frozenset[str]]]:
    """Every node of `node`, with the CTE names in scope at it (#441, #417).

    A query node's CTEs are in scope in its body; each CTE's own query sees the CTEs listed
    before it, and a recursive CTE sees itself in its recursive term only: DuckDB binds the
    name in the anchor term to the table of that name. A name outside that scope is not the
    CTE, whatever DuckDB would make of it: the guards read it as a lake table, or refuse it.
    """
    if isinstance(node, list):
        for item in node:
            yield from _scoped(item, scope)
        return
    if not isinstance(node, dict):
        return
    yield node, scope
    cte_map = node.get("cte_map")
    entries = cte_map.get("map", []) if isinstance(cte_map, dict) else []
    named = [
        str(e["key"]).lower()
        for e in entries
        if isinstance(e, dict) and isinstance(e.get("key"), str)
    ]
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        definition = entry.get("value")
        query = definition.get("query_node") if isinstance(definition, dict) else None
        earlier = scope | set(named[:position])
        if not (isinstance(query, dict) and query.get("type") == "RECURSIVE_CTE_NODE"):
            yield from _scoped(definition, earlier)
            continue
        yield definition, earlier  # type: ignore[misc]
        for key, value in definition.items():  # type: ignore[union-attr]
            if key != "query_node":
                yield from _scoped(value, earlier)
        yield query, earlier
        for key, value in query.items():  # the recursive term alone sees the CTE itself
            yield from _scoped(value, earlier | {named[position]} if key == "right" else earlier)
    body = scope | set(named)
    for key, value in node.items():
        if key != "cte_map":
            yield from _scoped(value, body)


def _is_lake_table(
    path: list[str], tables: set[tuple[str, str]], schemas: set[str], ctes: set[str]
) -> bool:
    """Whether a FROM name is a lake table alive at the snapshot, or one of the query's CTEs.

    Names that look like files or URLs are refused outright: DuckDB would otherwise fall back to
    reading them as files when no table matches.
    """
    parts = [part.lower() for part in path]
    if any(_SUSPICIOUS.search(part) for part in parts):
        return False
    if len(parts) == 1:
        return parts[0] in ctes or ("main", parts[0]) in tables
    if len(parts) == 2:
        if parts[0] in schemas:
            return (parts[0], parts[1]) in tables
        return parts[0] == LAKE and ("main", parts[1]) in tables
    return len(parts) == 3 and parts[0] == LAKE and (parts[1], parts[2]) in tables


def pin_tables(tree: dict[str, Any], snapshot: int, tables: set[tuple[str, str]]) -> None:
    """Add `AT (VERSION => snapshot)` to every table `tree` references, in place; `tables` are
    the lake's tables alive at `snapshot`.

    Every base table is pinned, including CTE references (DuckDB binds a CTE with an AT clause
    unchanged). A name that is not a lake table (or the read's own CTE) is refused.
    """
    schemas = {schema for schema, _ in tables}
    for node, ctes in _scoped(tree):
        if node.get("type") != "BASE_TABLE":
            continue
        qualified = node.get("qualified_name")
        raw = qualified.get("path") if isinstance(qualified, dict) else None
        path = [str(part) for part in raw] if isinstance(raw, list) else [str(node["table_name"])]
        if not _is_lake_table(path, tables, schemas, set(ctes)):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A read may reference only tables of the lake (and its own CTEs).",
                {"table": ".".join(path)},
            )
        node["at_clause"] = {
            "unit": "VERSION",
            "expr": {
                "class": "CONSTANT",
                "type": "VALUE_CONSTANT",
                "alias": "",
                "query_location": 0,
                "query_location_length": 0,
                "literal": {"kind": "INTEGER", "text": str(int(snapshot))},
            },
        }
