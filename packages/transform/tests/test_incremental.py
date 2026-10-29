"""P1-11, P1-12: incremental checks and run steps, without a lake (plain DuckDB)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import Parameter, StatementRequest
from orvex.transform.incremental import (
    Check,
    Count,
    Refuse,
    Watermark,
    column_problems,
    config_problems,
    schema_statements,
    since_value,
    steps,
    watermark_statements,
    widens,
)
from orvex.transform.models import Model, discover_models

COLUMNS = [("id", "BIGINT"), ("kwh", "DECIMAL(18,4)"), ("read_at", "TIMESTAMP WITH TIME ZONE")]


def model(tmp_path: Path, sidecar: str) -> Model:
    folder = tmp_path / "models" / "s"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "m.sql").write_text("SELECT 1 AS id", encoding="utf-8")
    (folder / "m.yaml").write_text(sidecar, encoding="utf-8")
    found = discover_models(tmp_path, {})
    assert found.problems == ()
    return found.models[0]


MERGE = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: merge, time_column: read_at, unique_key: [id]}\n"
)
APPEND = (
    "version: 1\nmaterialized: incremental\nincremental: {strategy: append, time_column: read_at}\n"
)
DELETE_INSERT = MERGE.replace("merge", "delete_insert")
PARTITION = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: partition_overwrite, time_column: read_at, partition_by: [month]}\n"
)


def lake() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    connection.execute("ATTACH ':memory:' AS lake")  # the store names its catalog `lake`
    connection.execute("USE lake")
    return connection


def table_columns(connection: duckdb.DuckDBPyConnection) -> list[tuple[str, str]] | None:
    found = connection.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 's' AND table_name = 'm' ORDER BY ordinal_position"
    ).fetchall()
    return [(name, kind) for name, kind in found] or None


def execute(
    connection: duckdb.DuckDBPyConnection, chosen: Model, sql: str
) -> tuple[list[int], str | None]:
    """Run a model's steps like the runner does, in one transaction: counts, then the failing
    check's code (None when every check passed and the transaction committed)."""
    output = connection.execute(f"DESCRIBE {sql}").fetchall()
    columns = [(row[0], row[1]) for row in output]
    counts: list[int] = []
    connection.execute("BEGIN")
    for step in steps(chosen, sql, columns, table_columns(connection)):
        if isinstance(step, Refuse):
            connection.execute("ROLLBACK")
            return counts, step.code
        if isinstance(step, StatementRequest):
            connection.execute(step.sql, {p.name: p.value for p in step.parameters})
            continue
        if isinstance(step, Watermark):
            continue  # tested on its own below
        value = connection.execute(step.sql).fetchall()[0][0]
        counts.append(value)
        if isinstance(step, Check) and value != 0:
            connection.execute("ROLLBACK")
            return counts, step.code
    connection.execute("COMMIT")
    return counts, None


def contents(connection: duckdb.DuckDBPyConnection, columns: str = "*") -> list[tuple[object, ...]]:
    return connection.execute(f"SELECT {columns} FROM s.m ORDER BY ALL").fetchall()


def test_config_rules(tmp_path: Path) -> None:
    merge = model(tmp_path / "a", MERGE.replace(", unique_key: [id]", ""))
    assert [p.code.root for p in config_problems(merge)] == ["MISSING_UNIQUE_KEY"]
    append = model(tmp_path / "b", APPEND.replace("read_at}", "read_at, lookback: 2h}"))
    assert [p.code.root for p in config_problems(append)] == ["LOOKBACK_REQUIRES_UNIQUE_KEY"]
    assert config_problems(model(tmp_path / "c", MERGE)) == []
    keyless = model(
        tmp_path / "d", MERGE.replace("merge", "delete_insert").replace(", unique_key: [id]", "")
    )
    assert [p.code.root for p in config_problems(keyless)] == ["MISSING_UNIQUE_KEY"]
    looking_back = model(tmp_path / "f", PARTITION.replace("[month]}", "[month], lookback: 1h}"))
    assert [p.code.root for p in config_problems(looking_back)] == ["LOOKBACK_REQUIRES_UNIQUE_KEY"]
    unpartitioned = model(tmp_path / "e", APPEND.replace("append", "partition_overwrite"))
    assert [p.code.root for p in config_problems(unpartitioned)] == ["MISSING_PARTITION_BY"]


def test_column_rules(tmp_path: Path) -> None:
    merge = model(tmp_path, MERGE)
    assert column_problems(merge, COLUMNS) == []
    text = [("id", "BIGINT"), ("read_at", "VARCHAR")]
    assert [p.code.root for p in column_problems(merge, text)] == ["TIME_COLUMN_NOT_TEMPORAL"]
    no_key = [("kwh", "DOUBLE"), ("read_at", "DATE")]
    (item,) = column_problems(merge, no_key)
    assert item.code.root == "MODEL_CONFIG_INVALID"
    assert item.details.path == "/incremental/unique_key/0"
    partitioned = model(tmp_path / "p", PARTITION.replace("[month]", "[nothing]"))
    (item,) = column_problems(partitioned, COLUMNS)
    assert (item.code.root, item.details.path) == (
        "MODEL_CONFIG_INVALID",
        "/incremental/partition_by/0",
    )


def test_merge_steps_run_in_duckdb(tmp_path: Path) -> None:
    """The planned SQL itself: first run creates, second merges, checks count offending rows."""
    merge = model(tmp_path, MERGE)
    connection = duckdb.connect()
    connection.execute("ATTACH ':memory:' AS lake")  # the store names its catalog `lake`
    connection.execute("USE lake")
    connection.execute("CREATE TABLE src (id BIGINT, kwh DECIMAL(18,4), read_at TIMESTAMPTZ)")

    def run(existing: list[tuple[str, str]] | None) -> list[int]:
        counts = []
        for step in steps(merge, "SELECT * FROM src", COLUMNS, existing):
            if isinstance(step, Watermark):
                continue  # TIMESTAMPTZ watermarks need the lake's time zone support
            if isinstance(step, StatementRequest):
                connection.execute(step.sql)
            elif isinstance(step, Count | Check):
                counts.append(connection.execute(step.sql).fetchall()[0][0])
            else:
                raise AssertionError(step)
        return counts

    connection.execute("INSERT INTO src VALUES (1, 1.0, '2024-01-01'), (2, 2.0, '2024-01-01')")
    assert run(None) == [2, 0, 0]
    connection.execute("DELETE FROM src")
    connection.execute("INSERT INTO src VALUES (2, 2.5001, '2024-01-02'), (3, 3.0, '2024-01-02')")
    assert run(COLUMNS) == [2, 0, 0, 0]  # count, NULL keys, repeated keys, column check
    rows = connection.execute("SELECT id, CAST(kwh AS VARCHAR) FROM s.m ORDER BY id").fetchall()
    assert rows == [(1, "1.0000"), (2, "2.5001"), (3, "3.0000")]
    assert connection.execute(
        "SELECT count(*) FROM information_schema.schemata WHERE schema_name = '_stage'"
    ).fetchall() == [(0,)]
    connection.execute("INSERT INTO src VALUES (NULL, 1, '2024-01-03'), (3, 1, '2024-01-03')")
    assert run(COLUMNS)[1:3] == [1, 1]  # a NULL key and a duplicated key are both counted


def test_schema_change_refuses_by_default(tmp_path: Path) -> None:
    merge = model(tmp_path / "a", MERGE)
    (refuse,) = steps(merge, "SELECT 1", COLUMNS, [("id", "BIGINT")])
    assert isinstance(refuse, Refuse)
    assert refuse.code == "SCHEMA_CHANGED"
    assert "added kwh, read_at" in refuse.message
    assert "on_schema_change: fail" in refuse.message


def test_delete_insert_replaces_a_key_as_a_set(tmp_path: Path) -> None:
    chosen = model(tmp_path, DELETE_INSERT)
    connection = lake()
    rows = "SELECT * FROM (VALUES {}) AS t(id, n, read_at)"
    first = "(1, 1, DATE '2024-01-01'), (1, 2, DATE '2024-01-01'), (2, 9, DATE '2024-01-01')"
    assert execute(connection, chosen, rows.format(first)) == ([3, 0], None)
    again = "(1, 3, DATE '2024-01-02'), (1, 4, DATE '2024-01-02'), (1, 5, DATE '2024-01-02')"
    # Count, NULL keys, column check; repeated keys are allowed.
    assert execute(connection, chosen, rows.format(again)) == ([3, 0, 0], None)
    assert contents(connection, "id, n") == [(1, 3), (1, 4), (1, 5), (2, 9)]
    null = "(CAST(NULL AS INTEGER), 7, DATE '2024-01-03')"
    assert execute(connection, chosen, rows.format(null)) == ([1, 1], "UNIQUE_KEY_NULL")
    assert contents(connection, "id, n") == [(1, 3), (1, 4), (1, 5), (2, 9)]


def test_partition_overwrite_replaces_loaded_partitions_only(tmp_path: Path) -> None:
    chosen = model(tmp_path, PARTITION)
    connection = lake()
    rows = "SELECT * FROM (VALUES {}) AS t(month, n, read_at)"
    first = (
        "('2024-01', 1, DATE '2024-01-01'), ('2024-02', 2, DATE '2024-02-01'), "
        "('2024-02', 3, DATE '2024-02-02'), (NULL, 4, DATE '2024-02-03')"
    )
    assert execute(connection, chosen, rows.format(first)) == ([4], None)
    again = "('2024-02', 5, DATE '2024-02-05'), (NULL, 6, DATE '2024-02-06')"
    assert execute(connection, chosen, rows.format(again)) == ([2, 0], None)
    assert contents(connection, "month, n") == [("2024-01", 1), ("2024-02", 5), (None, 6)]


SOURCE = "SELECT * FROM (VALUES {}) AS t({})"


def test_append_new_columns_adds_columns_and_refuses_removals(tmp_path: Path) -> None:
    chosen = model(tmp_path, APPEND + "on_schema_change: append_new_columns\n")
    connection = lake()
    first = SOURCE.format("(1, DATE '2024-01-01')", "id, read_at")
    assert execute(connection, chosen, first)[1] is None
    # A new column in the middle: added at the end of the table, old rows get NULL.
    wider = (
        "SELECT id, CAST(2.5 AS DECIMAL(10,2)) AS kwh, read_at "
        "FROM (VALUES (2, DATE '2024-01-02')) AS t(id, read_at)"
    )
    assert execute(connection, chosen, wider)[1] is None
    assert table_columns(connection) == [
        ("id", "INTEGER"),
        ("read_at", "DATE"),
        ("kwh", "DECIMAL(10,2)"),
    ]
    assert contents(connection, "id, CAST(kwh AS VARCHAR)") == [(1, None), (2, "2.50")]
    # The next run sees the table's order (columns added at the end) and still writes by name.
    assert execute(connection, chosen, wider.replace("(2,", "(3,"))[1] is None
    narrower = SOURCE.format("(4, DATE '2024-01-04')", "id, read_at")
    assert execute(connection, chosen, narrower)[1] == "SCHEMA_CHANGED"
    assert contents(connection, "id") == [(1,), (2,), (3,)]
    assert table_columns(connection) == [
        ("id", "INTEGER"),
        ("read_at", "DATE"),
        ("kwh", "DECIMAL(10,2)"),
    ]


def test_sync_all_columns_drops_widens_and_refuses_lossy_changes(tmp_path: Path) -> None:
    chosen = model(tmp_path, APPEND + "on_schema_change: sync_all_columns\n")
    connection = lake()
    first = (
        "SELECT id, CAST(kwh AS DECIMAL(10,2)) AS kwh, code, read_at FROM (VALUES "
        "(1, 12345678.99, '7', DATE '2024-01-01'), (2, -0.01, '8', DATE '2024-01-01')) "
        "AS t(id, kwh, code, read_at)"
    )
    assert execute(connection, chosen, first)[1] is None
    widened = (
        "SELECT id, CAST(kwh AS DECIMAL(18,2)) AS kwh, read_at FROM (VALUES "
        "(3, 9999999999999999.99, DATE '2024-01-02')) AS t(id, kwh, read_at)"
    )
    assert execute(connection, chosen, widened)[1] is None
    assert table_columns(connection) == [
        ("id", "INTEGER"),
        ("kwh", "DECIMAL(18,2)"),
        ("read_at", "DATE"),
    ]
    assert contents(connection, "id, CAST(kwh AS VARCHAR)") == [
        (1, "12345678.99"),
        (2, "-0.01"),
        (3, "9999999999999999.99"),
    ]
    lossy = widened.replace("CAST(kwh AS DECIMAL(18,2)) AS kwh", "CAST(id AS VARCHAR) AS kwh")
    assert execute(connection, chosen, lossy) == ([], "SCHEMA_CHANGED")
    assert table_columns(connection) == [
        ("id", "INTEGER"),
        ("kwh", "DECIMAL(18,2)"),
        ("read_at", "DATE"),
    ]
    assert len(contents(connection)) == 3


def test_only_exact_type_changes_widen() -> None:
    assert widens("INTEGER", "BIGINT")
    assert widens("TINYINT", "HUGEINT")
    assert widens("UINTEGER", "UBIGINT")
    assert widens("DECIMAL(10,2)", "DECIMAL(18,2)")
    assert widens("DATE", "TIMESTAMP")
    assert not widens("BIGINT", "INTEGER")
    assert not widens("UINTEGER", "BIGINT")  # mixed families are not applied
    assert not widens("BIGINT", "DOUBLE")
    assert not widens("INTEGER", "DECIMAL(18,2)")
    assert not widens("DECIMAL(10,2)", "DECIMAL(18,4)")  # the scale changes
    assert not widens("DECIMAL(18,2)", "DECIMAL(10,2)")
    assert not widens("DATE", "TIMESTAMP WITH TIME ZONE")  # depends on a time zone
    assert not widens("VARCHAR", "INTEGER")


def test_schema_statements_follow_the_policy(tmp_path: Path) -> None:
    old = [("id", "INTEGER"), ("gone", "VARCHAR")]
    new = [("ID", "BIGINT"), ("added", "DATE")]
    sync = model(tmp_path / "a", APPEND + "on_schema_change: sync_all_columns\n")
    assert schema_statements(sync, new, old) == [
        'ALTER TABLE "s"."m" ADD COLUMN "added" DATE',
        'ALTER TABLE "s"."m" ALTER COLUMN "id" SET DATA TYPE BIGINT',
        'ALTER TABLE "s"."m" DROP COLUMN "gone"',
    ]
    adding = model(tmp_path / "b", APPEND + "on_schema_change: append_new_columns\n")
    assert isinstance(schema_statements(adding, new, old), str)
    assert schema_statements(adding, old, old) == []


def test_columns_changed_after_validation_are_caught(tmp_path: Path) -> None:
    append = model(tmp_path, APPEND)
    connection = duckdb.connect()
    connection.execute("ATTACH ':memory:' AS lake")
    connection.execute("USE lake")
    connection.execute("CREATE SCHEMA s")
    connection.execute("CREATE TABLE s.m (id BIGINT, kwh DECIMAL(18,4), read_at TIMESTAMPTZ)")
    # The source now returns the columns in another order (validation saw the old order).
    source = "SELECT kwh, id, read_at FROM (SELECT 1::BIGINT AS id, 1.0::DECIMAL(18,4) AS kwh, "
    source += "now() AS read_at)"
    checks = []
    for step in steps(append, source, COLUMNS, COLUMNS):
        if isinstance(step, Check):
            checks.append((step.code, connection.execute(step.sql).fetchall()[0][0]))
            break
        if isinstance(step, StatementRequest):
            connection.execute(step.sql)
    assert checks == [("SCHEMA_CHANGED", 1)]


def test_the_column_check_fails_when_a_table_is_missing(tmp_path: Path) -> None:
    append = model(tmp_path, APPEND)
    connection = duckdb.connect()
    connection.execute("ATTACH ':memory:' AS lake")
    connection.execute("USE lake")
    check = [s for s in steps(append, "SELECT 1 AS id", COLUMNS, COLUMNS) if isinstance(s, Check)]
    # Neither the stage nor the target exists: the lookups are both NULL, which must not pass.
    assert connection.execute(check[-1].sql).fetchall() == [(1,)]


# --- P1-14: $since, watermark, lookback, full refresh --------------------------------------------


def with_sql(tmp_path: Path, sidecar: str, sql: str) -> Model:
    folder = tmp_path / "models" / "s"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "m.sql").write_text(sql, encoding="utf-8")
    (folder / "m.yaml").write_text(sidecar, encoding="utf-8")
    return discover_models(tmp_path, {}).models[0]


def test_since_must_be_in_the_sql(tmp_path: Path) -> None:
    manual = MERGE.replace("unique_key: [id]", "unique_key: [id], filter: manual")
    # Text in a comment or a string is not the parameter.
    unused = "SELECT 1 AS id, '$since' AS t -- $since"
    (item,) = config_problems(with_sql(tmp_path / "a", manual, unused))
    assert (item.code.root, item.details.file) == ("MISSING_SINCE_PARAMETER", "models/s/m.sql")
    # filter: auto places the filter itself (proven later, with the binder).
    assert config_problems(with_sql(tmp_path / "b", MERGE, unused)) == []
    used = "SELECT 1 AS id FROM t WHERE read_at > $since"
    assert config_problems(with_sql(tmp_path / "c", manual, used)) == []
    (item,) = config_problems(with_sql(tmp_path / "d", MERGE, used))
    assert (item.code.root, item.details.path) == ("MODEL_CONFIG_INVALID", "/incremental/filter")
    # DuckDB binds `$SINCE` to the same parameter: it counts as a use.
    (item,) = config_problems(with_sql(tmp_path / "e", MERGE, used.replace("$since", "$SINCE")))
    assert (item.code.root, item.details.path) == ("MODEL_CONFIG_INVALID", "/incremental/filter")


def test_since_is_the_watermark_minus_the_lookback() -> None:
    tz = "TIMESTAMP WITH TIME ZONE"
    assert since_value("DATE", None, "1h") == "-infinity"
    assert since_value(tz, None, None) == "-infinity"
    assert since_value("DATE", "2024-03-01", None) == "2024-03-01"
    assert since_value("DATE", "2024-03-01", "47h") == "2024-02-29"  # whole days, rounded down
    assert since_value("DATE", "2024-03-01", "2d") == "2024-02-28"
    stamp = "2024-01-02T10:00:00.000001"
    assert since_value("TIMESTAMP", stamp, "1h") == "2024-01-02T09:00:00.000001"
    assert since_value("TIMESTAMP", stamp, "90s") == "2024-01-02T09:58:30.000001"
    assert since_value(tz, "2024-01-02T00:10:00.000000+00:00", "30m") == (
        "2024-01-01T23:40:00.000000+00:00"
    )
    assert since_value("TIMESTAMP", "infinity", "1h") == "infinity"
    assert since_value("TIMESTAMP", "0001-01-01T00:00:00.000000", "1d") == "-infinity"


def test_since_reaches_the_store_only_as_a_bound_parameter(tmp_path: Path) -> None:
    sql = "SELECT * FROM src WHERE read_at > $since"
    chosen = with_sql(tmp_path, MERGE, sql)
    value = "2024-01-02T10:00:00.000000+00:00"
    planned = steps(chosen, sql, COLUMNS, COLUMNS, value)
    with_since = [s for s in planned if isinstance(s, StatementRequest) and s.parameters]
    assert len(with_since) == 1
    (parameter,) = with_since[0].parameters
    assert (parameter.name, parameter.type.root, parameter.value) == ("since", "TIMESTAMPTZ", value)
    assert "$since" in with_since[0].sql
    texts = [
        s.request.sql if isinstance(s, Watermark) else s.sql
        for s in planned
        if not isinstance(s, Refuse)
    ]
    assert all("2024" not in text for text in texts)


def test_full_refresh_replaces_the_table(tmp_path: Path) -> None:
    chosen = model(tmp_path, MERGE)
    planned = steps(chosen, "SELECT 1", COLUMNS, [("other", "VARCHAR")], full_refresh=True)
    sqls = [s.sql for s in planned if isinstance(s, StatementRequest)]
    assert 'CREATE OR REPLACE TABLE "s"."m" AS SELECT * FROM _stage."s__m"' in sqls
    assert not any(sql.startswith(("DELETE", "INSERT", "ALTER")) for sql in sqls)
    assert not any(isinstance(s, Refuse) for s in planned)  # the old columns do not matter


def test_watermark_is_the_largest_loaded_value(tmp_path: Path) -> None:
    connection = lake()
    for kind, rows, expected in [
        ("DATE", "(DATE '2024-01-03'), (DATE '2024-05-01'), (NULL)", "2024-05-01"),
        (
            "TIMESTAMP",
            "(TIMESTAMP '2099-12-31 23:59:59.999999'), (TIMESTAMP '2024-01-01 00:00:00')",
            "2099-12-31T23:59:59.999999",
        ),
        ("TIMESTAMP", "(TIMESTAMP 'infinity')", "infinity"),
        ("DATE", "(CAST(NULL AS DATE))", None),  # no value loaded: the previous one stays
    ]:
        chosen = model(tmp_path / kind, APPEND)
        sql = f"SELECT * FROM (VALUES {rows}) AS t(read_at)"
        planned = steps(chosen, sql, [("read_at", kind)], None)
        (mark,) = [s for s in planned if isinstance(s, Watermark)]
        for step in planned:
            if step is mark:
                assert connection.execute(step.request.sql).fetchall()[0][0] == expected
                break
            if isinstance(step, StatementRequest):
                connection.execute(step.sql)


def test_the_watermark_never_goes_back_and_is_stored_with_the_rows(tmp_path: Path) -> None:
    connection = lake()
    chosen = model(tmp_path, MERGE)
    sql = "SELECT * FROM (VALUES (1, DATE '2024-01-03')) AS t(id, read_at)"
    columns = [("id", "INTEGER"), ("read_at", "DATE")]

    def new_watermark(previous: str | None, full_refresh: bool = False) -> str | None:
        planned = steps(chosen, sql, columns, None, full_refresh=full_refresh, previous=previous)
        for step in planned:
            if isinstance(step, Watermark):
                parameters = {p.name: p.value for p in step.request.parameters}
                typed = step.request.sql.replace("$p_previous", "CAST($p_previous AS DATE)")
                value: str | None = connection.execute(typed, parameters).fetchall()[0][0]
                for statement in watermark_statements(step, value):
                    connection.execute(
                        statement.sql, {p.name: p.value for p in statement.parameters}
                    )
                return value
            if isinstance(step, StatementRequest):
                connection.execute(step.sql)
        raise AssertionError("no watermark step")

    stored = "SELECT model, watermark FROM _meta.watermarks"
    assert new_watermark(None) == "2024-01-03"
    assert connection.execute(stored).fetchall() == [("s.m", "2024-01-03")]
    # Re-read rows older than the previous watermark (lookback): it stays where it was.
    assert new_watermark("2024-02-01") == "2024-02-01"
    assert new_watermark("2023-12-01") == "2024-01-03"
    assert connection.execute(stored).fetchall() == [("s.m", "2024-01-03")]
    # A full refresh starts again from the loaded rows, whatever was stored.
    assert new_watermark("2099-01-01", full_refresh=True) == "2024-01-03"
    assert connection.execute(stored).fetchall() == [("s.m", "2024-01-03")]


def test_a_full_refresh_loading_nothing_drops_the_watermark() -> None:
    step = Watermark(StatementRequest(kind="statement", sql="SELECT 1", parameters=[]), "s.m", True)
    sqls = [s.sql for s in watermark_statements(step, None)]
    assert any(sql.startswith("DELETE FROM _meta.watermarks") for sql in sqls)
    assert not any(sql.startswith("INSERT") for sql in sqls)
    kept = Watermark(step.request, "s.m", False)
    assert watermark_statements(kept, None) == []


# --- #109: a $since filter that cuts a partition fails partition_overwrite -----------------------

FILTERED = PARTITION.replace("[month]}", "[month], filter: manual}")
LOADED = "SELECT * FROM src WHERE read_at > $since"
WHOLE = "SELECT * FROM src WHERE read_at >= date_trunc('month', $since)"


def run_since(
    connection: duckdb.DuckDBPyConnection,
    chosen: Model,
    sql: str,
    since: str,
    full_refresh: bool = False,
) -> tuple[str | None, str | None]:
    """Run the steps with `$since` bound, like the runner: (failing code, its message), or
    (None, None) when the transaction committed."""
    # Every model here selects the source's columns.
    described = connection.execute("DESCRIBE SELECT * FROM src").fetchall()
    output = [(row[0], row[1]) for row in described]
    connection.execute("BEGIN")
    for step in steps(chosen, sql, output, table_columns(connection), since, full_refresh):
        if isinstance(step, Refuse):
            connection.execute("ROLLBACK")
            return step.code, step.message
        if isinstance(step, Watermark):
            continue
        parameters = {} if isinstance(step, Count) else typed(step.parameters)
        if isinstance(step, StatementRequest):
            connection.execute(step.sql, parameters)
            continue
        value = connection.execute(step.sql, parameters).fetchall()[0][0]
        if isinstance(step, Check) and value not in {0, "0", None}:
            connection.execute("ROLLBACK")
            return step.code, step.failure(str(value))
    connection.execute("COMMIT")
    return None, None


def typed(parameters: Sequence[Parameter]) -> dict[str, object]:
    """Parameters as typed Python values, as the store binds them (never as text)."""
    values: dict[str, object] = {}
    for parameter in parameters:
        kind, value = parameter.type.root, parameter.value
        if value in {"infinity", "-infinity"}:
            values[parameter.name] = value  # DuckDB casts the text, as the store does
        elif value is not None and kind == "DATE":
            values[parameter.name] = dt.date.fromisoformat(value)
        elif value is not None and kind in {"TIMESTAMP", "TIMESTAMPTZ"}:
            values[parameter.name] = dt.datetime.fromisoformat(value)
        else:
            values[parameter.name] = value
    return values


def days(first: str, last: str, kind: str = "DATE") -> str:
    """Source rows (month, n, read_at), one per day; `n` marks the run that loaded them."""
    return (
        f"SELECT strftime(d, '%Y-%m') AS month, 0 AS n, CAST(d AS {kind}) AS read_at "
        f"FROM range(TIMESTAMP '{first}', TIMESTAMP '{last}' + INTERVAL 1 DAY, INTERVAL 1 DAY) "
        "AS r(d)"
    )


def partition_lake(kind: str = "DATE") -> duckdb.DuckDBPyConnection:
    """October 1 to 20 already in the table (n = 0); October 21 to 25 new in the source (n = 1)."""
    connection = lake()
    connection.execute("CREATE SCHEMA s")
    connection.execute(f"CREATE TABLE s.m AS {days('2026-10-01', '2026-10-20', kind)}")
    connection.execute(
        f"CREATE TABLE src AS {days('2026-10-01', '2026-10-25', kind)} "
        "UNION ALL BY NAME SELECT '2026-11' AS month, 0 AS n, "
        f"CAST(TIMESTAMP '2026-11-02' AS {kind}) AS read_at"
    )
    connection.execute("UPDATE src SET n = 1")
    return connection


def test_a_since_filter_that_cuts_a_partition_fails_and_writes_nothing(tmp_path: Path) -> None:
    chosen = model(tmp_path, FILTERED)
    connection = partition_lake()
    code, message = run_since(connection, chosen, LOADED, "2026-10-20")
    assert code == "PARTITION_INCOMPLETE"
    assert message is not None
    assert "s.m: the $since filter loaded only part of these partitions (month): " in message
    assert "'2026-10' (1 in total)" in message
    assert "date_trunc('month', $since)" in message
    # Rolled back: October 1 to 20 are untouched, nothing of the new rows was written.
    assert connection.execute("SELECT count(*), max(n) FROM s.m").fetchall() == [(20, 0)]


def test_a_whole_partition_filter_passes_and_keeps_every_row(tmp_path: Path) -> None:
    chosen = model(tmp_path, FILTERED)
    connection = partition_lake()
    assert run_since(connection, chosen, WHOLE, "2026-10-20") == (None, None)
    assert connection.execute(
        "SELECT month, count(*), min(n), max(n) FROM s.m GROUP BY month ORDER BY month"
    ).fetchall() == [("2026-10", 25, 1, 1), ("2026-11", 1, 1, 1)]


def test_a_new_partition_only_passes(tmp_path: Path) -> None:
    chosen = model(tmp_path, FILTERED)
    connection = partition_lake()
    assert run_since(connection, chosen, LOADED, "2026-10-31") == (None, None)
    assert connection.execute(
        "SELECT month, count(*) FROM s.m GROUP BY month ORDER BY month"
    ).fetchall() == [("2026-10", 20), ("2026-11", 1)]


def test_every_temporal_type(tmp_path: Path) -> None:
    for number, (kind, since) in enumerate(
        [
            ("DATE", "2026-10-20"),
            ("TIMESTAMP", "2026-10-20T00:00:00.000000"),
            ("TIMESTAMPTZ", "2026-10-20T00:00:00.000000+00:00"),
        ]
    ):
        chosen = model(tmp_path / str(number), FILTERED)
        connection = partition_lake(kind)
        connection.execute("SET TimeZone = 'UTC'")
        code, message = run_since(connection, chosen, LOADED, since)
        assert (code, kind) == ("PARTITION_INCOMPLETE", kind)
        assert message is not None
        assert "'2026-10' (1 in total)" in message
        assert run_since(connection, chosen, WHOLE, since) == (None, None), kind


def test_a_null_partition_is_caught_and_named(tmp_path: Path) -> None:
    by_region = FILTERED.replace("[month]", "[region]")
    chosen = model(tmp_path, by_region)
    connection = lake()
    connection.execute("CREATE SCHEMA s")
    connection.execute(
        "CREATE TABLE s.m AS SELECT * FROM (VALUES (NULL, 1, DATE '2026-10-01'), "
        "('n', 2, DATE '2026-10-01')) AS t(region, n, read_at)"
    )
    connection.execute(
        "CREATE TABLE src AS SELECT * FROM (VALUES (CAST(NULL AS VARCHAR), 3, DATE '2026-10-05'), "
        "(NULL, 4, DATE '2026-10-01')) AS t(region, n, read_at)"
    )
    code, message = run_since(connection, chosen, LOADED, "2026-10-02")
    assert code == "PARTITION_INCOMPLETE"
    assert message is not None
    assert ": NULL (1 in total)." in message
    assert connection.execute("SELECT count(*) FROM s.m").fetchall() == [(2,)]


def test_undated_rows_the_filter_never_reloads_are_protected(tmp_path: Path) -> None:
    by_region = FILTERED.replace("[month]", "[region]")
    chosen = model(tmp_path, by_region)
    connection = lake()
    connection.execute("CREATE SCHEMA s")
    connection.execute(
        "CREATE TABLE s.m AS SELECT * FROM (VALUES ('n', 1, CAST(NULL AS DATE))) "
        "AS t(region, n, read_at)"
    )
    connection.execute(
        "CREATE TABLE src AS SELECT * FROM (VALUES ('n', 2, DATE '2026-10-05')) "
        "AS t(region, n, read_at)"
    )
    code, message = run_since(connection, chosen, LOADED, "2026-10-02")
    assert code == "PARTITION_INCOMPLETE"
    assert message is not None
    assert ": 'n' (1 in total)." in message
    # A filter that also reloads undated rows passes.
    reloads = "SELECT * FROM src WHERE read_at > $since OR read_at IS NULL"
    connection.execute("INSERT INTO src VALUES ('n', 3, NULL)")
    assert run_since(connection, chosen, reloads, "2026-10-02") == (None, None)
    assert connection.execute("SELECT n FROM s.m ORDER BY n").fetchall() == [(2,), (3,)]


def test_the_message_names_five_partitions_and_the_total(tmp_path: Path) -> None:
    by_two = FILTERED.replace("[month]", "[region, month]")
    chosen = model(tmp_path, by_two)
    connection = lake()
    connection.execute("CREATE SCHEMA s")
    regions = "SELECT chr(CAST(97 + i AS INTEGER)) AS region FROM range(7) AS r(i)"
    connection.execute(
        f"CREATE TABLE s.m AS SELECT region, '2026-10' AS month, 0 AS n, DATE '2026-10-01' "
        f"AS read_at FROM ({regions})"
    )
    connection.execute(
        f"CREATE TABLE src AS SELECT region, '2026-10' AS month, 1 AS n, DATE '2026-10-09' "
        f"AS read_at FROM ({regions})"
    )
    code, message = run_since(connection, chosen, LOADED, "2026-10-05")
    assert code == "PARTITION_INCOMPLETE"
    assert message is not None
    assert (
        "('a', '2026-10'), ('b', '2026-10'), ('c', '2026-10'), ('d', '2026-10'), "
        "('e', '2026-10') (7 in total)." in message
    )


def test_first_runs_full_refreshes_and_unfiltered_models_skip_the_check(tmp_path: Path) -> None:
    chosen = model(tmp_path, FILTERED)
    existing = [("month", "VARCHAR"), ("n", "INTEGER"), ("read_at", "DATE")]

    def codes(sql: str, since: str, full_refresh: bool = False, first: bool = False) -> list[str]:
        planned = steps(chosen, sql, existing, None if first else existing, since, full_refresh)
        return [step.code for step in planned if isinstance(step, Check)]

    # A first run: no table yet, so nothing can be cut.
    assert "PARTITION_INCOMPLETE" not in codes(LOADED, "-infinity", first=True)
    assert "PARTITION_INCOMPLETE" in codes(LOADED, "2026-10-20")
    # An existing table without a watermark still gets the check (its undated rows).
    assert "PARTITION_INCOMPLETE" in codes(LOADED, "-infinity")
    assert "PARTITION_INCOMPLETE" not in codes(LOADED, "2026-10-20", full_refresh=True)
    assert "PARTITION_INCOMPLETE" not in codes("SELECT * FROM src", "2026-10-20")
    # Other strategies replace by key or delete nothing.
    for other in (MERGE, DELETE_INSERT, APPEND):
        chosen_other = model(tmp_path / other[60:70].strip(), other)
        planned = steps(chosen_other, LOADED, existing, existing, "2026-10-20")
        assert all(
            step.code != "PARTITION_INCOMPLETE" for step in planned if isinstance(step, Check)
        )


def test_the_check_needs_no_since_value(tmp_path: Path) -> None:
    # #155: the oldest-row rule compares loaded and held rows only; `$since` is not bound into
    # the check, and its value never appears in the SQL.
    chosen = model(tmp_path, FILTERED)
    existing = [("month", "VARCHAR"), ("n", "INTEGER"), ("read_at", "DATE")]
    (check,) = [
        step
        for step in steps(chosen, LOADED, existing, existing, "2026-10-20")
        if isinstance(step, Check) and step.code == "PARTITION_INCOMPLETE"
    ]
    assert "2026-10-20" not in check.sql
    assert list(check.parameters) == []


def test_partition_columns_cannot_collide_with_the_check_s_own_names(tmp_path: Path) -> None:
    """Partition columns named like the query's internal names still compare as partitions."""
    for number, name in enumerate(["older", "undated", "n_older", "p0", "cut_label", "loaded"]):
        chosen = model(tmp_path / str(number), FILTERED.replace("[month]", f"[{name}]"))
        connection = lake()
        connection.execute("CREATE SCHEMA s")
        connection.execute(
            f"CREATE TABLE s.m AS SELECT 7 AS \"{name}\", 0 AS n, DATE '2026-10-01' AS read_at"
        )
        connection.execute(
            f"CREATE TABLE src AS SELECT 7 AS \"{name}\", 1 AS n, DATE '2026-10-09' AS read_at"
        )
        code, message = run_since(connection, chosen, LOADED, "2026-10-05")
        assert code == "PARTITION_INCOMPLETE", name
        assert message is not None
        assert ": 7 (1 in total)." in message, name
        assert connection.execute("SELECT n FROM s.m").fetchall() == [(0,)], name


def test_text_values_are_quoted_and_long_ones_shortened(tmp_path: Path) -> None:
    by_region = FILTERED.replace("[month]", "[region]")
    chosen = model(tmp_path, by_region)
    connection = lake()
    connection.execute("CREATE SCHEMA s")
    long = "x" * 100
    connection.execute(
        "CREATE TABLE s.m AS SELECT * FROM (VALUES ('NULL', 1, DATE '2026-10-01'), "
        f"(CAST(NULL AS VARCHAR), 2, DATE '2026-10-01'), ('{long}', 3, DATE '2026-10-01'), "
        "('it''s', 4, DATE '2026-10-01')) AS t(region, n, read_at)"
    )
    connection.execute(
        "CREATE TABLE src AS SELECT region, 9 AS n, DATE '2026-10-09' AS read_at FROM s.m"
    )
    code, message = run_since(connection, chosen, LOADED, "2026-10-05")
    assert code == "PARTITION_INCOMPLETE"
    assert message is not None
    shown = "'" + "x" * 60 + "…'"  # shortened inside its quotes
    assert f": 'NULL', '{'it' + chr(39) * 2 + 's'}', {shown}, NULL (4 in total)." in message


def test_undated_rows_are_protected_before_any_watermark(tmp_path: Path) -> None:
    """An existing table holding only undated rows has no watermark: $since is -infinity."""
    by_region = FILTERED.replace("[month]", "[region]")
    chosen = model(tmp_path, by_region)
    connection = lake()
    connection.execute("CREATE SCHEMA s")
    connection.execute(
        "CREATE TABLE s.m AS SELECT * FROM (VALUES ('n', 1, CAST(NULL AS DATE))) "
        "AS t(region, n, read_at)"
    )
    connection.execute(
        "CREATE TABLE src AS SELECT * FROM (VALUES ('n', 2, DATE '2026-10-05')) "
        "AS t(region, n, read_at)"
    )
    code, _ = run_since(connection, chosen, LOADED, "-infinity")
    assert code == "PARTITION_INCOMPLETE"


def test_enum_values_are_quoted(tmp_path: Path) -> None:
    by_kind = FILTERED.replace("[month]", "[k]")
    chosen = model(tmp_path, by_kind)
    connection = lake()
    connection.execute("CREATE TYPE kinds AS ENUM ('NULL', 'b')")
    connection.execute("CREATE SCHEMA s")
    connection.execute(
        "CREATE TABLE s.m AS SELECT CAST('NULL' AS kinds) AS k, 0 AS n, "
        "DATE '2026-10-01' AS read_at"
    )
    connection.execute(
        "CREATE TABLE src AS SELECT CAST('NULL' AS kinds) AS k, 1 AS n, "
        "DATE '2026-10-09' AS read_at"
    )
    code, message = run_since(connection, chosen, LOADED, "2026-10-05")
    assert code == "PARTITION_INCOMPLETE"
    assert message is not None
    assert ": 'NULL' (1 in total)." in message


# --- #155: the oldest-row rule (decision #128, option B) ---------------------------------------


@pytest.mark.parametrize(
    ("sql", "loaded"),
    [
        ("SELECT * FROM src WHERE read_at >= $since", 6),
        ("SELECT * FROM src WHERE read_at > $since - INTERVAL 3 DAY", 8),
    ],
    ids=["at-or-after-since", "hand-written-lookback"],
)
def test_a_partial_reload_fails_and_keeps_the_table(tmp_path: Path, sql: str, loaded: int) -> None:
    # The #128 measurements: 20 rows became 6 and 8 under the #95 rule.
    chosen = model(tmp_path, FILTERED)
    connection = partition_lake()
    reloaded = connection.execute(
        f"SELECT count(*) FROM ({sql.replace('$since', "DATE '2026-10-20'")}) "
        "WHERE month = '2026-10'"
    ).fetchall()
    assert reloaded == [(loaded,)]
    code, message = run_since(connection, chosen, sql, "2026-10-20")
    assert code == "PARTITION_INCOMPLETE"
    assert message is not None
    assert "older than the oldest row this run loaded" in message
    assert connection.execute("SELECT count(*), max(n) FROM s.m").fetchall() == [(20, 0)]


def test_a_source_that_dropped_its_oldest_row_fails_until_a_full_refresh(tmp_path: Path) -> None:
    chosen = model(tmp_path, FILTERED)
    connection = partition_lake()
    connection.execute("DELETE FROM src WHERE read_at = DATE '2026-10-01'")
    code, message = run_since(connection, chosen, WHOLE, "2026-10-20")
    assert code == "PARTITION_INCOMPLETE"
    assert message is not None
    assert "--full-refresh" in message
    assert connection.execute("SELECT count(*) FROM s.m").fetchall() == [(20,)]
    assert run_since(connection, chosen, WHOLE, "2026-10-20", full_refresh=True) == (None, None)
    assert connection.execute(
        "SELECT month, count(*) FROM s.m GROUP BY month ORDER BY month"
    ).fetchall() == [("2026-10", 24), ("2026-11", 1)]


def test_the_same_oldest_time_passes(tmp_path: Path) -> None:
    # The documented limit: a row with the oldest loaded time, not reloaded, is not detected.
    chosen = model(tmp_path, FILTERED)
    connection = partition_lake()
    connection.execute("INSERT INTO s.m VALUES ('2026-10', 7, DATE '2026-10-01')")
    assert run_since(connection, chosen, WHOLE, "2026-10-20") == (None, None)


def test_only_undated_loaded_rows_of_a_dated_partition_fail(tmp_path: Path) -> None:
    by_region = FILTERED.replace("[month]", "[region]")
    chosen = model(tmp_path, by_region)
    connection = lake()
    connection.execute("CREATE SCHEMA s")
    connection.execute(
        "CREATE TABLE s.m AS SELECT * FROM (VALUES ('n', 1, DATE '2026-10-01')) "
        "AS t(region, n, read_at)"
    )
    connection.execute(
        "CREATE TABLE src AS SELECT * FROM (VALUES ('n', 2, CAST(NULL AS DATE))) "
        "AS t(region, n, read_at)"
    )
    reloads = "SELECT * FROM src WHERE read_at > $since OR read_at IS NULL"
    code, _ = run_since(connection, chosen, reloads, "2026-10-02")
    assert code == "PARTITION_INCOMPLETE"
    assert connection.execute("SELECT n FROM s.m").fetchall() == [(1,)]


def sql_model(tmp_path: Path, sidecar: str, sql: str) -> Model:
    folder = tmp_path / "models" / "s"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "m.sql").write_text(sql, encoding="utf-8")
    (folder / "m.yaml").write_text(sidecar, encoding="utf-8")
    found = discover_models(tmp_path, {})
    return found.models[0]


BY_DAY = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: partition_overwrite, time_column: day, partition_by: [month], "
    "filter: manual}\n"
)


@pytest.mark.parametrize(
    "where",
    [
        "read_at > $since",
        "coalesce(read_at, day) > $since",
        "day > $since AND $since IS NOT NULL",
        # #162 Reviewer pass 1: each of these lost rows the source still had.
        "day BETWEEN $since AND $since + INTERVAL 1 MONTH",
        "day >= date_trunc('month', $since) AND day < $since + INTERVAL 3 DAY",
        "day = date_trunc('month', $since) OR day >= $since",
        "day NOT BETWEEN date_trunc('month', $since) + INTERVAL 1 DAY AND $since",
        "day >= greatest($since, read_at::DATE)",
        "day < $since",
        "day > $since OR x = 1",
        # #162 Reviewer pass 2: a bound outside the query that returns the rows.
        "day >= date_trunc('month', $since) AND day < "
        "(SELECT min(day) + INTERVAL 3 DAY FROM src WHERE day >= $since)",
        "day >= date_trunc('month', $since) AND day NOT IN "
        "(SELECT day FROM src WHERE day >= $since)",
        "day >= date_trunc('month', $since) AND id IN (SELECT id FROM src WHERE day >= $since)",
        "EXISTS (SELECT 1 FROM src AS s WHERE s.day > $since)",
        "c.day >= $since",
    ],
    ids=[
        "finer-column",
        "expression",
        "outside-a-comparison",
        "between",
        "upper-bound",
        "or",
        "not-between",
        "column-in-the-bound",
        "only-an-upper-bound",
        "or-another-condition",
        "scalar-subquery",
        "not-in",
        "in-semi-join",
        "exists",
        "another-table",
    ],
)
def test_a_since_filter_on_another_column_is_refused(tmp_path: Path, where: str) -> None:
    chosen = sql_model(tmp_path, BY_DAY, f"SELECT * FROM src WHERE {where}")
    found = config_problems(chosen)
    assert [item.code.root for item in found] == ["INVALID_MODEL_SQL"]
    assert "time_column day" in found[0].message


def test_since_outside_a_comparison_in_the_select_list_is_refused(tmp_path: Path) -> None:
    chosen = sql_model(tmp_path, BY_DAY, "SELECT $since AS s, * FROM src WHERE day > $since")
    assert [item.code.root for item in config_problems(chosen)] == ["INVALID_MODEL_SQL"]


@pytest.mark.parametrize(
    "where",
    [
        "day > $since",
        "m.day >= date_trunc('month', $since)",
        "$since < day",
        "DAY > $since - INTERVAL 3 DAY",
        "day > $since OR day IS NULL",
        "(day >= $since OR day IS NULL) AND status = 'valid'",
        "m.day > $since AND id > 0",
    ],
)
def test_a_since_filter_on_the_time_column_is_accepted(tmp_path: Path, where: str) -> None:
    chosen = sql_model(tmp_path, BY_DAY, f"SELECT * FROM src AS m WHERE {where}")
    assert config_problems(chosen) == []


def test_the_same_column_rule_is_for_partition_overwrite_only(tmp_path: Path) -> None:
    append = BY_DAY.replace("partition_overwrite", "append").replace(", partition_by: [month]", "")
    chosen = sql_model(tmp_path, append, "SELECT * FROM src WHERE read_at > $since")
    assert config_problems(chosen) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM (SELECT * FROM src WHERE day >= $since) AS s",
        "WITH w AS (SELECT * FROM src WHERE day >= $since) SELECT * FROM w",
        "SELECT * FROM src WHERE day > $since UNION ALL SELECT * FROM src2 WHERE day > $since",
    ],
    ids=["from-subquery", "cte", "union-all"],
)
def test_a_bound_in_a_query_whose_rows_the_model_returns_is_accepted(
    tmp_path: Path, sql: str
) -> None:
    assert config_problems(sql_model(tmp_path, BY_DAY, sql)) == []


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM src EXCEPT ALL SELECT * FROM src WHERE day >= $since",
        "SELECT * FROM src WHERE day > $since INTERSECT SELECT * FROM src",
        "SELECT r.* FROM src AS r JOIN changes AS c ON r.id = c.id WHERE c.day >= $since",
        "SELECT r.* FROM src AS r JOIN changes AS c ON r.id = c.id WHERE r.day >= $since",
        "WITH w AS (SELECT * FROM src WHERE day >= $since) SELECT * FROM w JOIN x USING (id)",
    ],
    ids=["except", "intersect", "join-other-table", "join", "cte-in-a-join"],
)
def test_a_bound_outside_the_row_queries_is_refused(tmp_path: Path, sql: str) -> None:
    # #162 Reviewer pass 2: rows a join, EXCEPT or INTERSECT drops depend on other data, which
    # the oldest-row check cannot see.
    found = config_problems(sql_model(tmp_path, BY_DAY, sql))
    assert [item.code.root for item in found] == ["INVALID_MODEL_SQL"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT month, id, CAST(day AS DATE) AS day FROM src WHERE day > $since",
        "SELECT month, id, max(day) AS day, sum(kwh) AS kwh FROM src WHERE day >= $since "
        "GROUP BY ALL",
        "SELECT month, id, max(day) AS day FROM src WHERE day >= $since GROUP BY month, id",
        "SELECT * FROM src WHERE day >= $since GROUP BY ALL HAVING count(*) > 1",
        "SELECT *, sum(kwh) OVER (PARTITION BY month) AS total FROM src WHERE day >= $since",
        "SELECT * FROM src WHERE day >= $since QUALIFY row_number() OVER () > 1",
        "SELECT * FROM src WHERE day >= $since ORDER BY day LIMIT 10",
        "SELECT * FROM src WHERE day >= $since OFFSET 2",
        "SELECT DISTINCT ON (id) * FROM src WHERE day >= $since",
        "SELECT * FROM src TABLESAMPLE 10 WHERE day >= $since",
        "SELECT * REPLACE (CAST(day AS DATE) AS day) FROM src WHERE day >= $since",
        "SELECT * RENAME (other AS day) FROM src WHERE day >= $since",
        "SELECT COLUMNS(*) FROM src WHERE day >= $since",
        "SELECT id, other AS day FROM src WHERE day >= $since",
        "SELECT *, day FROM src WHERE day >= $since",
        "SELECT id, day FROM (SELECT id, max(day) AS day FROM src WHERE day >= $since "
        "GROUP BY id) AS s",
        "WITH c AS (SELECT * FROM src WHERE day >= date_trunc('month', $since)) SELECT * FROM c "
        "WHERE id IN (SELECT id FROM c WHERE day >= (SELECT min(day) FROM c) + INTERVAL 14 DAY)",
        "SELECT * FROM src WHERE day >= $since - CAST(random() * 30 AS INTEGER)",
    ],
    ids=[
        "cast-alias",
        "aggregate-group-by-all",
        "aggregate-group-by",
        "having",
        "window",
        "qualify",
        "limit",
        "offset",
        "distinct-on",
        "sample",
        "star-replace",
        "star-rename",
        "columns",
        "another-column-named-like-it",
        "twice",
        "aggregate-below",
        "cte-read-twice",
        "random-bound",
    ],
)
def test_a_bound_whose_query_changes_the_time_column_or_the_rows_is_refused(
    tmp_path: Path, sql: str
) -> None:
    # #162 Reviewer pass 3: the run's check sees the output time column; a query that computes
    # it, or computes over the filtered rows, could lose rows the check cannot see.
    found = config_problems(sql_model(tmp_path, BY_DAY, sql))
    assert [item.code.root for item in found] == ["INVALID_MODEL_SQL"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, kwh, day, month FROM src WHERE day > $since",
        "SELECT id, s.day AS day, month FROM src AS s WHERE s.day > $since ORDER BY id",
        "SELECT DISTINCT * FROM src WHERE day > $since",
        "SELECT s.* FROM src AS s WHERE s.day > $since",
        "SELECT * EXCLUDE (kwh) FROM src WHERE day > $since",
        "SELECT * FROM (SELECT id, day, month FROM src WHERE day > $since) AS s ORDER BY day",
    ],
    ids=["columns", "qualified", "distinct", "qualified-star", "star-exclude", "subquery"],
)
def test_a_bound_whose_query_passes_the_time_column_through_is_accepted(
    tmp_path: Path, sql: str
) -> None:
    assert config_problems(sql_model(tmp_path, BY_DAY, sql)) == []
