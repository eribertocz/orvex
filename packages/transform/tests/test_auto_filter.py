"""P1-13: the automatic incremental filter is injected only when proven safe."""

from __future__ import annotations

import datetime as dt

import duckdb
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from orvex.transform.auto_filter import Refusal, filtered_sql, known

COLUMNS = known(
    {
        "archive.data": ["reading_id", "consumer_id", "read_at", "status", "kwh"],
        "staging.readings": ["reading_id", "consumer_id", "read_at", "status", "kwh"],
        "staging.consumers": ["consumer_id", "tariff", "created_at"],
        "staging.other": ["reading_id", "consumer_id", "read_at", "status", "kwh"],
        "staging.u": ["consumer_id", "read_at", "data"],
    }
)
SINCE = dt.datetime(2024, 1, 2, 12, 0)  # plain TIMESTAMP columns here


def lake() -> duckdb.DuckDBPyConnection:
    """Tables for every relation above, with readings on both sides of `SINCE`."""
    connection = duckdb.connect()
    for schema in ("archive", "staging"):
        connection.execute(f"CREATE SCHEMA {schema}")
    readings = (
        "SELECT i AS reading_id, 'C' || (i % 3) AS consumer_id, "
        "TIMESTAMP '2024-01-01 00:00:00' + INTERVAL (i * 7) HOUR AS read_at, "
        "CASE WHEN i % 4 = 0 THEN 'voided' ELSE 'valid' END AS status, "
        "CAST(i AS DECIMAL(18,4)) AS kwh FROM range(1, 13) AS t(i)"
    )
    for table in ("archive.data", "staging.readings", "staging.other"):
        connection.execute(f"CREATE TABLE {table} AS {readings}")
    connection.execute(
        "CREATE TABLE staging.consumers AS SELECT * FROM (VALUES ('C0', 'a', "
        "TIMESTAMP '2023-01-01 00:00:00'), ('C1', 'b', TIMESTAMP '2023-01-01 00:00:00'))"
        " AS t(consumer_id, tariff, created_at)"
    )
    return connection


def rows(connection: duckdb.DuckDBPyConnection, sql: str, since: bool = False) -> list[str]:
    parameters = {"since": SINCE} if since else {}
    return sorted(map(repr, connection.execute(sql, parameters).fetchall()))


PROVABLE = [
    "SELECT * FROM archive.data",
    "SELECT reading_id, read_at AS ts FROM staging.readings WHERE status = 'valid'",
    "SELECT r.reading_id, r.read_at, c.tariff FROM staging.readings AS r "
    "JOIN staging.consumers AS c ON r.consumer_id = c.consumer_id",
    "WITH valid AS (SELECT * FROM staging.readings WHERE status = 'valid') "
    "SELECT reading_id, read_at FROM valid",
    "SELECT s.reading_id, s.read_at FROM (SELECT * FROM staging.readings) AS s",
    "SELECT r.read_at, c.tariff FROM staging.readings AS r "
    "LEFT JOIN staging.consumers AS c USING (consumer_id)",
    "SELECT a.read_at FROM staging.readings AS a JOIN staging.readings AS b USING (reading_id)",
]


@pytest.mark.parametrize("sql", PROVABLE)
def test_proven_filters_keep_exactly_the_newer_rows(sql: str) -> None:
    column = "ts" if " ts" in sql else "read_at"
    result = filtered_sql(sql, column, COLUMNS)
    assert isinstance(result, str), result
    assert "$since" in result
    connection = lake()
    expected = rows(connection, f"SELECT * FROM ({sql}) AS q WHERE q.{column} > $since", since=True)
    assert rows(connection, result, since=True) == expected
    assert expected  # the cases keep some rows and drop others
    assert len(expected) < len(rows(connection, sql))


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (
            "SELECT max(read_at) AS read_at FROM staging.readings GROUP BY consumer_id",
            "aggregation",
        ),
        (
            "SELECT read_at, max(kwh) OVER (PARTITION BY consumer_id) AS m FROM staging.readings",
            "window",
        ),
        (
            "SELECT read_at FROM staging.readings UNION ALL SELECT read_at FROM staging.other",
            "set operation",
        ),
        ("SELECT date_trunc('day', read_at) AS read_at FROM staging.readings", "expression"),
        (
            "SELECT r.read_at FROM staging.consumers AS c "
            "LEFT JOIN staging.readings AS r USING (consumer_id)",
            "outer join",
        ),
        ("SELECT read_at FROM staging.readings LIMIT 100", "limit"),
        ("SELECT DISTINCT read_at FROM staging.readings", "distinct"),
        ("SELECT reading_id FROM staging.readings", "not found"),
        ("SELECT read_at FROM staging.readings AS a, staging.other AS b", "several tables"),
        ("SELECT read_at FROM staging.readings QUALIFY row_number() OVER () = 1", "window"),
        ("SELECT read_at FROM staging.readings USING SAMPLE 10%", "limit"),
        ("SELECT read_at, count(*) OVER () AS n FROM staging.readings", "window"),
        ("SELECT read_at, kwh FROM staging.readings GROUP BY ALL", "aggregation"),
        (
            "WITH c AS (SELECT * FROM staging.readings) "
            "SELECT a.read_at FROM c AS a JOIN c AS b USING (reading_id)",
            "several tables",
        ),
        (
            "SELECT * REPLACE (read_at + INTERVAL 1 HOUR AS read_at) FROM staging.readings",
            "expression",
        ),
        ("SELECT kwh AS read_at2, read_at2 AS read_at FROM staging.readings", "expression"),
        (
            "SELECT max(read_at) OVER (PARTITION BY consumer_id) AS read_at FROM staging.readings",
            "window",
        ),
        (
            "SELECT r.read_at FROM staging.readings AS r "
            "FULL JOIN staging.consumers AS c USING (consumer_id)",
            "outer join",
        ),
        # Renamed by position: the name may belong to another column of the table.
        ("SELECT read_at FROM staging.readings AS r(read_at)", "expression"),
        ("SELECT read_at FROM (SELECT read_at FROM staging.readings) AS s(read_at)", "expression"),
        (
            "SELECT read_at FROM (SELECT reading_id, consumer_id, read_at FROM staging.readings)"
            " AS s(read_at)",
            "expression",
        ),
        (
            "WITH c(read_at) AS (SELECT reading_id, read_at AS x FROM staging.readings) "
            "SELECT read_at FROM c",
            "expression",
        ),
        ("SELECT *, kwh AS read_at FROM staging.readings", "several tables"),
        (
            "SELECT u.data.ts AS read_at FROM "
            "(SELECT {'ts': read_at} AS data FROM staging.readings) AS u",
            "expression",
        ),
        (
            "WITH c AS (SELECT * FROM staging.readings) SELECT read_at FROM "
            "(WITH c AS (SELECT * FROM staging.other) SELECT * FROM c) AS x",
            "several tables",
        ),
        (
            "WITH c AS (SELECT * FROM staging.readings) SELECT read_at FROM c "
            "WHERE reading_id IN (SELECT reading_id FROM query_table('c'))",
            "several tables",
        ),
        # Output names DuckDB takes from the data or renames (second review).
        (
            "SELECT read_at FROM (SELECT #3, kwh AS read_at FROM staging.readings) AS s",
            "expression",
        ),
        ("SELECT read_at FROM (SELECT unnest(data), read_at FROM staging.u) AS s", "expression"),
        (
            "SELECT read_at FROM (SELECT CAST(COLUMNS('^read_at$') AS TIMESTAMP), "
            "kwh AS read_at FROM staging.readings) AS s",
            "expression",
        ),
        (
            "SELECT unnest(u.data), r.read_at FROM staging.readings AS r "
            "JOIN staging.u AS u USING (consumer_id)",
            "expression",
        ),
        (
            "SELECT read_at_1 AS read_at FROM "
            "(SELECT kwh AS read_at, read_at, kwh AS read_at_1 FROM staging.readings) AS s",
            "several tables",
        ),
        (
            "SELECT read_at_1 AS read_at FROM "
            "(SELECT *, kwh AS read_at FROM staging.readings) AS s",
            "several tables",
        ),
        (
            "SELECT read_at FROM (SELECT r.read_at FROM staging.readings AS r "
            "FULL JOIN staging.consumers AS c USING (consumer_id)) AS s",
            "outer join",
        ),
        (
            "WITH f AS (SELECT r.read_at FROM staging.readings AS r "
            "FULL JOIN staging.consumers AS c USING (consumer_id)) SELECT read_at FROM f",
            "outer join",
        ),
        (
            "WITH c AS (SELECT * FROM staging.readings) SELECT read_at FROM c "
            "WHERE reading_id IN (SELECT reading_id FROM query('SELECT * FROM c'))",
            "several tables",
        ),
        (
            "SELECT readings.read_at FROM staging.readings "
            "JOIN archive.readings USING (reading_id)",
            "several tables",
        ),
    ],
)
def test_unprovable_filters_are_refused_with_a_reason(sql: str, reason: str) -> None:
    result = filtered_sql(sql, "read_at", COLUMNS)
    assert isinstance(result, Refusal)
    assert result.reason == reason


def test_the_rewrite_is_deterministic_and_strict() -> None:
    sql = "SELECT reading_id, read_at FROM staging.readings WHERE status = 'valid'"
    first = filtered_sql(sql, "read_at", COLUMNS)
    assert first == filtered_sql(sql, "read_at", COLUMNS)
    assert isinstance(first, str)
    assert "(readings.read_at > $since)" in first


def test_the_location_of_the_column_is_reported() -> None:
    sql = "SELECT reading_id,\n  date_trunc('day', read_at) AS read_at\nFROM staging.readings"
    result = filtered_sql(sql, "read_at", COLUMNS)
    assert isinstance(result, Refusal)
    assert result.location is not None
    assert sql.count("\n", 0, result.location) + 1 == 2


# --- property: every generated provable shape keeps exactly the hand-filtered rows -----------

WHERE = st.sampled_from(["", "status = 'valid'", "kwh > 3", "consumer_id <> 'C1'"])
SHAPE = st.sampled_from(["plain", "renamed", "cte", "subquery", "join", "left join", "star"])


def generated(shape: str, where: str) -> tuple[str, str]:
    """A provable SELECT of the given shape and the output name of the time column."""
    condition = f" WHERE {where}" if where else ""
    if shape == "plain":
        return f"SELECT reading_id, read_at, kwh FROM staging.readings{condition}", "read_at"
    if shape == "renamed":
        return f"SELECT reading_id, read_at AS ts FROM staging.readings{condition}", "ts"
    if shape == "cte":
        return (
            f"WITH r AS (SELECT * FROM staging.readings{condition}) SELECT reading_id, read_at "
            "FROM r",
            "read_at",
        )
    if shape == "subquery":
        return (
            f"SELECT q.read_at, q.kwh FROM (SELECT * FROM staging.readings{condition}) AS q",
            "read_at",
        )
    if shape == "join":
        prefixed = where.replace("status", "r.status").replace("kwh", "r.kwh")
        prefixed = prefixed.replace("consumer_id", "r.consumer_id")
        joined = f" WHERE {prefixed}" if where else ""
        return (
            "SELECT r.read_at, c.tariff FROM staging.readings AS r "
            f"JOIN staging.consumers AS c ON r.consumer_id = c.consumer_id{joined}",
            "read_at",
        )
    if shape == "left join":
        return (
            "SELECT r.read_at, c.tariff FROM staging.readings AS r "
            "LEFT JOIN staging.consumers AS c ON r.consumer_id = c.consumer_id",
            "read_at",
        )
    return f"SELECT * FROM staging.readings{condition}", "read_at"


@settings(max_examples=60, deadline=None)
@given(SHAPE, WHERE, st.integers(min_value=0, max_value=90))
def test_property_rewritten_equals_hand_filtered(shape: str, where: str, hours: int) -> None:
    sql, column = generated(shape, where)
    result = filtered_sql(sql, column, COLUMNS)
    assert isinstance(result, str), result
    connection = lake()
    since = dt.datetime(2024, 1, 1) + dt.timedelta(hours=hours)
    expected = connection.execute(
        f"SELECT * FROM ({sql}) AS q WHERE q.{column} > $since", {"since": since}
    ).fetchall()
    actual = connection.execute(result, {"since": since}).fetchall()
    assert sorted(map(repr, actual)) == sorted(map(repr, expected))
