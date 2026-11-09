"""Cost (law 7; ADR-0027 §7): ClickHouse's own estimate of the rows a statement reads, recorded
in the receipt; it never refuses a read (no rows-scanned budget, Owner, 2026-11-08)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from orvex.semantic.adapters.duckdb import PhysicalQuery


def estimate_statement(query: PhysicalQuery) -> str:
    """`EXPLAIN ESTIMATE` of the statement, with the same parameters: one row per MergeTree-
    family table it reads (`database`, `table`, `parts`, `rows`, `marks`); a table of any other
    engine has no row."""
    return f"EXPLAIN ESTIMATE {query.sql}"


def rows_scanned(
    query: PhysicalQuery, rows: Sequence[Mapping[str, object]], database: str
) -> int | None:
    """The rows the statement reads, from its `EXPLAIN ESTIMATE` rows: each table read counts
    its estimated rows once per read. A table with no estimate (not MergeTree) means there are
    no statistics, so no estimate at all (`semantic-engine.md` §8)."""
    found: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row["database"]), str(row["table"]))
        found[key] = found.get(key, 0) + int(str(row["rows"]))
    total = 0
    for source, _ in query.reads:
        parts = source.split(".")
        key = (parts[0], parts[1]) if len(parts) == 2 else (database, parts[0])
        if key not in found:
            return None
        total += found[key]
    return total
