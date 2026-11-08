"""Which DuckDB functions give the same value in every run, and which are aggregates."""

from __future__ import annotations

import functools

import duckdb

# Functions DuckDB marks `CONSISTENT` whose value still changes between runs (the session's
# clock, the installed version) or depends on the order rows arrive in (#135, Reviewer pass 2).
UNSTABLE_FUNCTIONS = frozenset(
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
SPECIAL_NAMES = frozenset(
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


def is_consistent(name: str) -> bool:
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


def is_aggregate(name: str) -> bool:
    return name.lower() in _aggregate_functions()
