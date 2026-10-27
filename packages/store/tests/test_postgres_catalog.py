"""P1-02: DuckLake with a PostgreSQL catalog, concurrent writers and secret handling."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from orvex.contracts.store.v1 import Parameter, PostgresLakeLocation, ReadRequest, StatementRequest
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, open_lake

LIMITS = Limits(memory_limit="512MB", threads=1)
PASSWORD_ENV = "STORE_TEST_POSTGRES_PASSWORD"

OTHER_PROCESS = """
import sys
from pathlib import Path
from orvex.contracts.store.v1 import PostgresLakeLocation, StatementRequest
from orvex.store.lake import Limits, open_lake

lake = open_lake(PostgresLakeLocation.model_validate_json(sys.argv[1]), Limits("512MB", 1))
with lake.transaction() as transaction:
    transaction.execute(StatementRequest(kind="statement", sql=sys.argv[2], parameters=[]))
lake.close()
"""


def statement(sql: str) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[])


def rows(lake: Lake, sql: str, snapshot: int | None = None) -> list[dict[str, object]]:
    request = ReadRequest(
        kind="read",
        sql=sql,
        parameters=[],
        snapshot=lake.current_snapshot().id if snapshot is None else snapshot,
        batch_size=1024,
    )
    result: list[dict[str, object]] = lake.read(request).read_all().to_pylist()
    return result


def write(lake: Lake, sql: str) -> int:
    with lake.transaction() as transaction:
        transaction.execute(statement(sql))
    assert transaction.result is not None
    assert transaction.result.snapshot is not None
    return transaction.result.snapshot.id


def run_elsewhere(location: PostgresLakeLocation, sql: str) -> None:
    """Commit `sql` from a separate Python process on the same lake."""
    subprocess.run(
        [sys.executable, "-c", OTHER_PROCESS, location.model_dump_json(), sql],
        check=True,
        timeout=120,
    )


@pytest.fixture
def location(postgres_location: PostgresLakeLocation) -> PostgresLakeLocation:
    return postgres_location


def test_open_read_survives_a_commit_from_another_process(location: PostgresLakeLocation) -> None:
    lake = open_lake(location, LIMITS)
    try:
        pinned = write(lake, "CREATE TABLE readings AS SELECT range AS id FROM range(200000)")
        request = ReadRequest(
            kind="read",
            sql="SELECT id FROM readings",
            parameters=[],
            snapshot=pinned,
            batch_size=1000,
        )
        reader = lake.read(request)
        seen = reader.read_next_batch().num_rows
        run_elsewhere(location, "DELETE FROM readings WHERE id % 2 = 0")
        seen += sum(batch.num_rows for batch in reader)
        assert seen == 200000
        latest = rows(lake, "SELECT count(*) AS n FROM readings")
        assert latest == [{"n": 100000}]
    finally:
        lake.close()


def test_writers_of_different_tables_both_commit(location: PostgresLakeLocation) -> None:
    one = open_lake(location, LIMITS)
    two = open_lake(location, LIMITS)
    try:
        write(one, "CREATE TABLE a (x INTEGER)")
        write(one, "CREATE TABLE b (y INTEGER)")
        start = one.current_snapshot().id
        with one.transaction() as first:
            first.execute(statement("INSERT INTO a VALUES (1)"))
            with two.transaction() as second:
                second.execute(statement("INSERT INTO b VALUES (2)"))
        assert first.result is not None
        assert first.result.snapshot is not None
        assert second.result is not None
        assert second.result.snapshot is not None
        assert {first.result.snapshot.id, second.result.snapshot.id} == {start + 1, start + 2}
        assert rows(one, "SELECT x FROM a") == [{"x": 1}]
        assert rows(one, "SELECT y FROM b") == [{"y": 2}]
    finally:
        one.close()
        two.close()


def test_conflicting_writers_one_wins(location: PostgresLakeLocation) -> None:
    one = open_lake(location, LIMITS)
    two = open_lake(location, LIMITS)
    try:
        write(one, "CREATE TABLE a AS SELECT 1 AS id, 'old' AS v")

        def conflict() -> None:
            with one.transaction() as first:
                first.execute(statement("UPDATE a SET v = 'one' WHERE id = 1"))
                with two.transaction() as second:
                    second.execute(statement("UPDATE a SET v = 'two' WHERE id = 1"))

        with pytest.raises(StoreError) as raised:
            conflict()
        assert raised.value.code == "STORE_TRANSACTION_CONFLICT"
        assert rows(one, "SELECT v FROM a") == [{"v": "two"}]
    finally:
        one.close()
        two.close()


def test_password_never_appears(
    location: PostgresLakeLocation,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.DEBUG)
    password = os.environ[PASSWORD_ENV]
    lake = open_lake(location, LIMITS)
    try:
        assert password not in repr(lake)
        with pytest.raises(StoreError) as raised:
            rows(lake, "SELECT * FROM no_such_table")
        assert password not in str(raised.value)
        assert all(password not in value for value in raised.value.details.values())
    finally:
        lake.close()
    wrong = location.model_copy(update={"connection": location.connection + " port=1"})
    with pytest.raises(StoreError) as unreachable:
        open_lake(wrong, LIMITS)
    assert unreachable.value.code == "STORE_CATALOG_UNAVAILABLE"
    assert password not in str(unreachable.value)
    assert all(password not in value for value in unreachable.value.details.values())
    assert password not in caplog.text


def test_missing_password_variable(
    location: PostgresLakeLocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PASSWORD_ENV)
    with pytest.raises(StoreError) as raised:
        open_lake(location, LIMITS)
    assert raised.value.code == "STORE_SECRET_MISSING"
    assert raised.value.details == {"variable": PASSWORD_ENV}


@pytest.mark.parametrize(
    "connection",
    [
        "host=h dbname=d password=x",
        "host=h dbname=d PASSWORD = x",
        "host=h dbname=d sslpassword=x",
        "postgresql://user:x@h/d",
        "postgresql://h/d?password=x",
    ],
)
def test_inline_password_is_refused(connection: str, tmp_path: Path) -> None:
    """Checked before anything else, so no database is needed."""
    inline = PostgresLakeLocation(
        kind="lake_location",
        catalog="postgres",
        connection=connection,
        password_env="NOT_USED_BY_THIS_TEST",
        data_path=tmp_path.as_posix(),
    )
    with pytest.raises(StoreError) as raised:
        open_lake(inline, LIMITS)
    assert raised.value.code == "STORE_SECRET_INLINE"
    assert "x" not in raised.value.details.values()


def test_default_metadata_schema(location: PostgresLakeLocation) -> None:
    public = location.model_copy(update={"metadata_schema": None})
    lake = open_lake(public, LIMITS)
    try:
        snapshot = write(lake, "CREATE TABLE in_public AS SELECT 1 AS x")
        assert rows(lake, "SELECT x FROM in_public", snapshot) == [{"x": 1}]
    finally:
        lake.close()


def test_metadata_schema_is_checked_again(location: PostgresLakeLocation) -> None:
    hostile = location.model_construct(**{**location.model_dump(), "metadata_schema": "x; DROP"})
    with pytest.raises(StoreError) as raised:
        open_lake(hostile, LIMITS)
    assert raised.value.code == "STORE_STATEMENT_INVALID"


def test_parameter_round_trip(location: PostgresLakeLocation) -> None:
    lake = open_lake(location, LIMITS)
    try:
        request = ReadRequest(
            kind="read",
            sql="SELECT $d AS d",
            parameters=[Parameter(name="d", type="DECIMAL(18,4)", value="1.2345")],  # type: ignore[arg-type]
            snapshot=lake.current_snapshot().id,
            batch_size=16,
        )
        assert str(lake.read(request).read_all().column("d")[0]) == "1.2345"
    finally:
        lake.close()
