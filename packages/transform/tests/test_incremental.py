"""P1-11, P1-12: incremental checks and run steps, without a lake (plain DuckDB)."""

from __future__ import annotations

from pathlib import Path

import duckdb
from orvex.contracts.store.v1 import StatementRequest
from orvex.transform.incremental import (
    Check,
    Count,
    Refuse,
    column_problems,
    config_problems,
    schema_statements,
    steps,
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
            connection.execute(step.sql)
            continue
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
