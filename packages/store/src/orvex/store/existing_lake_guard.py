"""The statement guard of an existing DuckLake's instance (P2-30 PR 3, #399; ADR-0027 §3).

The sandbox of `existing_lake` bounds what the instance's files can reach, yet a statement could
still use what the instance holds: `postgres_query` or `postgres_execute` on the catalog's own
connection, `COPY … TO` into the data folder (which the allow-list must keep readable), an
`ATTACH ':memory:'`, the lake's views and macros. So only the shape of SQL the semantic adapter
generates is run there:

- exactly one `SELECT`, every value bound as a `$name` of its declared type;
- the checks of a managed-lake read (`sql_guard._single_statement`, `read_tree._read_tree`): no
  statement of another kind, no file read, no SQL given as text, no `SHOW`/`DESCRIBE`, no table
  function but the generators, nothing of the catalog database or the engine's own settings;
- every table it reads is a table of the lake alive at the snapshot read (never a view), named
  through the lake's own catalog and pinned there with `AT (VERSION => n)`; the lake is never on
  the instance's search path;
- no function call names a catalog or a schema, so none can resolve to one of the lake's macros.

Anything else is `STORE_STATEMENT_INVALID`, and nothing reaches the instance. The guard only
parses (on the store's parser, which holds no lake); it never runs the statement.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

import duckdb
from orvex.contracts.store.v1 import Parameter
from orvex.store.errors import StoreError
from orvex.store.lake_names import LAKE
from orvex.store.parameters import _bound, _typed
from orvex.store.read_tree import _cte_names, _is_lake_table, _read_tree, _walk
from orvex.store.sql_guard import SHARED_HARMLESS_FUNCTIONS, TABLE_FUNCTIONS_SQL, _single_statement
from orvex.store.sql_parser import _plain


def guarded(
    sql: str,
    parameters: Sequence[Parameter],
    *,
    tables: Collection[tuple[str, str]],
    snapshot: int,
    table_functions: frozenset[str],
) -> tuple[dict[str, Any], dict[str, str | None]]:
    """The parse tree of `sql`, every table it reads pinned to `snapshot` of the lake, and its
    bound values; `tables` are the lake's tables alive at `snapshot`, (schema, table) in lower
    case. Refused (`STORE_STATEMENT_INVALID`) unless it is the adapter's shape."""
    text = _plain(sql)
    statement = _single_statement(text, table_functions)
    if statement.type != duckdb.StatementType.SELECT:
        raise StoreError(
            "STORE_STATEMENT_INVALID",
            "A read must be one SELECT statement.",
            {"statement": statement.type.name},
        )
    values = _bound(statement, list(parameters))
    tree = _read_tree(_typed(text, list(parameters)))
    found = set(tables)
    schemas = {schema for schema, _ in found}
    ctes = _cte_names(tree)
    for node in _walk(tree):
        if node.get("class") == "FUNCTION" and (node.get("catalog") or node.get("schema")):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A read on an existing lake may call no function of a named catalog or schema.",
                {"function": str(node.get("function_name"))},
            )
        if node.get("type") != "BASE_TABLE":
            continue
        path = _path(node)
        if len(path) == 1 and path[0].lower() in ctes:
            continue  # the read's own CTE: DuckDB binds it first
        if not _is_lake_table(path, found, schemas, set()):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A read may reference only tables of the lake (and its own CTEs).",
                {"table": ".".join(path)},
            )
        # As `_is_lake_table` read it: `table`, `schema.table` (a lake schema) or `lake.table`,
        # and `lake.schema.table`.
        if len(path) == 3:
            parts = path[1:]
        elif len(path) == 2 and path[0].lower() in schemas:
            parts = path
        else:
            parts = ["main", path[-1]]
        node["catalog_name"], node["schema_name"], node["table_name"] = LAKE, parts[0], parts[1]
        node["qualified_name"] = {"path": [LAKE, parts[0], parts[1]]}
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
    return tree, values


def table_functions(connection: duckdb.DuckDBPyConnection) -> frozenset[str]:
    """The instance's table functions, which the guard refuses by name; read before the lock."""
    row = connection.execute(TABLE_FUNCTIONS_SQL).fetchone()
    text = str(row[0]) if row and row[0] is not None else ""
    return frozenset(text.split("|")) - {""} - SHARED_HARMLESS_FUNCTIONS


def _path(node: dict[str, Any]) -> list[str]:
    """A FROM name as written: catalog, schema and table, whichever are given."""
    qualified = node.get("qualified_name")
    raw = qualified.get("path") if isinstance(qualified, dict) else None
    if isinstance(raw, list) and raw:
        return [str(part) for part in raw]
    return [
        str(node[key]) for key in ("catalog_name", "schema_name", "table_name") if node.get(key)
    ]
