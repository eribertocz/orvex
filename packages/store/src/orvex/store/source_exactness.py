"""Source columns DuckDB cannot carry exactly (law 11): which a PostgreSQL source has, and whether a
statement reads one.
"""

from __future__ import annotations

import json
import re
from typing import NamedTuple

import duckdb
from orvex.store.read_tree import _walk


class Inexact(NamedTuple):
    """A source column DuckDB cannot carry exactly."""

    source: str
    schema: str  # "" for SQLite
    table: str
    column: str
    label: str  # how errors name it, after the source name


def postgres_inexact_sql(scope: str) -> str:
    """The PostgreSQL query listing a source's inexact columns: numeric without precision or
    above 38 digits (DuckDB reads both as DOUBLE), money, and arrays of either, on every relation
    DuckDB can scan (tables, views, materialized views, foreign and partitioned tables)."""
    return (
        "SELECT n.nspname AS table_schema, c.relname AS table_name, a.attname AS column_name "
        "FROM pg_catalog.pg_attribute a "
        "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'v', 'm', 'f', 'p') AND a.attnum > 0 "
        "AND NOT a.attisdropped "
        "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "AND n.nspname NOT LIKE 'pg_toast%' AND ("
        "(a.atttypid = 'numeric'::regtype "
        # typmod - 4 = precision << 16 | scale (11 bits; PostgreSQL 15+ allows a negative
        # scale or one above the precision, which DuckDB cannot represent either).
        "AND (a.atttypmod < 0 OR ((a.atttypmod - 4) >> 16) > 38 "
        "OR ((a.atttypmod - 4) & 2047) > ((a.atttypmod - 4) >> 16))) "
        "OR a.atttypid IN ('money'::regtype, '_money'::regtype, '_numeric'::regtype))" + scope
    )


def _inexact_column_read(
    cursor: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: dict[str, str | None],
    inexact: frozenset[Inexact],
) -> str | None:
    """`source.table.column` of an inexact source column the statement reads, or None.

    The columns a statement reads are those DuckDB's plan scans (projections and pushed-down
    filters). A scan is matched by its table name and, when the plan names one, its schema or
    source; when two candidates share a name, any of them counts (refusing beats a lost digit).
    A statement that names none of these sources is not planned twice.
    """
    lowered = sql.lower()
    # Every inexact column of a source the statement names: a SQLite view is expanded by
    # DuckDB, so its scan names the underlying table, not the view.
    candidates = [entry for entry in inexact if entry.source in lowered]
    if not candidates:
        return None
    result = cursor.execute(f"EXPLAIN (FORMAT json) {sql}", parameters)
    row = result.fetchone()
    result.fetchone()  # reaches the end of the one-row result, so the cursor is free again
    plan = json.loads(str(row[1])) if row else []
    for node in _walk(plan):
        info = node.get("extra_info")
        table = info.get("Table") if isinstance(info, dict) else None
        if not isinstance(info, dict) or not isinstance(table, str):
            continue
        scanned = table.lower().replace('"', "").split(".")
        for entry in candidates:
            if scanned[-1] != entry.table:
                continue
            qualifiers = scanned[:-1]
            if qualifiers and not ({entry.schema, entry.source} & set(qualifiers)) and entry.schema:
                continue
            if "Projections" not in info:
                return f"{entry.source}.{entry.label}"  # columns unknown: assume all are read
            used = f"{info.get('Projections')} {info.get('Filters', '')}".lower()
            if re.search(rf"(?<![a-z0-9_]){re.escape(entry.column)}(?![a-z0-9_])", used):
                return f"{entry.source}.{entry.label}"
    return None
