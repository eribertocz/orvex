"""The tables of a DuckLake catalog at one snapshot, read from the catalog itself: the managed
lake's (`lake`) and an existing lake's (`existing_lake_reads`) alike. Nothing of any other
version is read, and a view is never listed as a table.
"""

from __future__ import annotations

import json
from typing import Any

import duckdb
from orvex.store.sql_text import identifier as _identifier


def one_row(
    cursor: duckdb.DuckDBPyConnection, sql: str, parameters: dict[str, object]
) -> tuple[object, ...] | None:
    """One catalog row on `cursor`; no unfinished result is left behind."""
    result = cursor.execute(sql, parameters)
    found: tuple[object, ...] | None = result.fetchone()
    result.fetchone()
    return found


def tables_at(cursor: duckdb.DuckDBPyConnection, meta: str, snapshot: int) -> set[tuple[str, str]]:
    """(schema, table) of every table alive at `snapshot` in the DuckLake catalog `meta` (the
    managed lake's or an existing lake's), lower-cased. A view is not a table."""
    row = one_row(
        cursor,
        f"SELECT string_agg(lower(s.schema_name) || '.' || lower(t.table_name), '|') "
        f"FROM {meta}.ducklake_table AS t "
        f"JOIN {meta}.ducklake_schema AS s USING (schema_id) "
        "WHERE t.begin_snapshot <= $n AND (t.end_snapshot IS NULL OR t.end_snapshot > $n) "
        "AND s.begin_snapshot <= $n AND (s.end_snapshot IS NULL OR s.end_snapshot > $n)",
        {"n": snapshot},
    )
    text = str(row[0]) if row and row[0] is not None else ""
    found: set[tuple[str, str]] = set()
    for item in text.split("|"):
        schema, _, table = item.partition(".")
        if table:
            found.add((schema, table))
    return found


def tables_with_columns(
    cursor: duckdb.DuckDBPyConnection, meta: str, attached: str, snapshot: int
) -> list[dict[str, Any]]:
    """Every table alive at `snapshot` in the DuckLake catalog `meta`, attached as `attached`, with
    its columns: names and nullability from the catalog, DuckDB types as DuckDB binds the table
    at that version (`TablesResult`'s tables)."""
    alive = "{0}.begin_snapshot <= $n AND ({0}.end_snapshot IS NULL OR {0}.end_snapshot > $n)"
    row = one_row(
        cursor,
        "SELECT CAST(to_json(list({'schema': s.schema_name, 'table': t.table_name, "
        "'nullable': (SELECT list({'name': c.column_name, 'nullable': c.nulls_allowed} "
        f"ORDER BY c.column_order) FROM {meta}.ducklake_column AS c "
        f"WHERE c.table_id = t.table_id AND c.parent_column IS NULL AND {alive.format('c')})}} "
        "ORDER BY s.schema_name, t.table_name)) AS VARCHAR) "
        f"FROM {meta}.ducklake_table AS t JOIN {meta}.ducklake_schema AS s USING (schema_id) "
        f"WHERE {alive.format('t')} AND {alive.format('s')}",
        {"n": snapshot},
    )
    found = json.loads(str(row[0])) if row is not None and row[0] is not None else []
    tables = []
    for entry in found:
        # The table bound at that version: its types as DuckDB reads it there. Names come
        # from the catalog and are quoted as identifiers; the version is an integer.
        relation = cursor.sql(
            f"SELECT * FROM {_identifier(attached)}.{_identifier(entry['schema'])}."
            f"{_identifier(entry['table'])} AT (VERSION => {int(snapshot)}) LIMIT 0"
        )
        nullable = {str(c["name"]).casefold(): bool(c["nullable"]) for c in entry["nullable"] or ()}
        tables.append(
            {
                "schema": entry["schema"],
                "table": entry["table"],
                "columns": [
                    {
                        "name": str(name),
                        "type": str(kind),
                        "nullable": nullable.get(str(name).casefold(), True),
                    }
                    for name, kind in zip(relation.columns, relation.types, strict=True)
                ],
            }
        )
    return tables
