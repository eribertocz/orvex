"""#441 AC7 (#417 audit F4): the managed lake's connection has its configuration locked at the
end of its sandbox setup. Once open, no setting can change on it, through any cursor, so a
statement that got past the SQL guard still could not lift the sandbox, the time zone or the
replacement scan. The lake still reads and writes as before, in UTC."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import LocalLakeLocation, ReadRequest, StatementRequest
from orvex.store.lake import Lake, Limits, open_lake

LIMITS = Limits(
    memory_limit="1GB", threads=2, max_concurrent=8, queue_size=64, default_timeout_ms=60_000
)


@pytest.fixture
def lake(tmp_path: Path) -> Iterator[Lake]:
    location = LocalLakeLocation(kind="lake_location", catalog="local", path=str(tmp_path / "l"))
    opened = open_lake(location, LIMITS)
    yield opened
    opened.close()


@pytest.mark.parametrize(
    "statement",
    [
        "SET enable_external_access = true",
        "SET allowed_directories = ['/']",
        "SET TimeZone = 'America/La_Paz'",
        "SET python_enable_replacements = true",
        "SET memory_limit = '64GB'",
        "SET GLOBAL threads = 64",
        "RESET enable_external_access",
        "SET lock_configuration = false",
    ],
)
def test_no_setting_changes_once_the_lake_is_open(lake: Lake, statement: str) -> None:
    cursor = lake._cursor()  # below the SQL guard: the lock is the layer under it
    with pytest.raises(duckdb.Error):  # the lock, or a sandbox that cannot be undone anyway
        cursor.sql(statement)
    cursor.close()


def test_every_cursor_reads_in_utc_without_replacements(lake: Lake) -> None:
    cursor = lake._cursor()

    def setting(name: str) -> object:
        return cursor.execute(f"SELECT current_setting('{name}')").fetchall()[0][0]

    assert setting("TimeZone") == "UTC"
    assert setting("python_enable_replacements") is False
    assert setting("lock_configuration") is True
    cursor.close()


def test_the_lake_still_writes_and_reads(lake: Lake) -> None:
    with lake.transaction() as transaction:
        transaction.execute(
            StatementRequest(
                kind="statement",
                sql="CREATE TABLE t AS SELECT TIMESTAMPTZ '2026-11-12 10:00:00-04' AS moment",
                parameters=[],
            )
        )
    snapshot = lake.current_snapshot().id
    read = lake.read(
        ReadRequest(kind="read", sql="SELECT CAST(moment AS VARCHAR) AS v FROM t", parameters=[],
                    snapshot=snapshot, batch_size=10, timeout_ms=None)
    )  # fmt: skip
    assert read.read_all().to_pylist() == [{"v": "2026-11-12 14:00:00+00"}]
