"""P1-11: incremental checks and run steps, without a lake."""

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
    steps,
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


def test_config_rules(tmp_path: Path) -> None:
    merge = model(tmp_path / "a", MERGE.replace(", unique_key: [id]", ""))
    assert [p.code.root for p in config_problems(merge)] == ["MISSING_UNIQUE_KEY"]
    append = model(tmp_path / "b", APPEND.replace("read_at}", "read_at, lookback: 2h}"))
    assert [p.code.root for p in config_problems(append)] == ["LOOKBACK_REQUIRES_UNIQUE_KEY"]
    assert config_problems(model(tmp_path / "c", MERGE)) == []


def test_column_rules(tmp_path: Path) -> None:
    merge = model(tmp_path, MERGE)
    assert column_problems(merge, COLUMNS) == []
    text = [("id", "BIGINT"), ("read_at", "VARCHAR")]
    assert [p.code.root for p in column_problems(merge, text)] == ["TIME_COLUMN_NOT_TEMPORAL"]
    no_key = [("kwh", "DOUBLE"), ("read_at", "DATE")]
    (item,) = column_problems(merge, no_key)
    assert item.code.root == "MODEL_CONFIG_INVALID"
    assert item.details.path == "/incremental/unique_key/0"


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


def test_schema_change_and_unsupported_strategy_refuse(tmp_path: Path) -> None:
    merge = model(tmp_path / "a", MERGE)
    (refuse,) = steps(merge, "SELECT 1", COLUMNS, [("id", "BIGINT")])
    assert isinstance(refuse, Refuse)
    assert refuse.code == "SCHEMA_CHANGED"
    assert "added kwh, read_at" in refuse.message
    later = model(tmp_path / "b", MERGE.replace("merge", "partition_overwrite"))
    (refuse,) = steps(later, "SELECT 1", COLUMNS, None)
    assert isinstance(refuse, Refuse)
    assert refuse.code == "UNSUPPORTED_MODEL_KIND"


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
