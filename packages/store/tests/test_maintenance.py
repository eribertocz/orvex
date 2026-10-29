"""P1-20 (#68): snapshot expiration, compaction and file cleanup, on both catalogs."""

from __future__ import annotations

import datetime as dt
import os
import threading
import time
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    PostgresLakeLocation,
    ReadRequest,
    SqliteLakeLocation,
    StatementRequest,
)
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

# The shared contract tests run on every catalog, SQLite included (#120).
EVERY_CATALOG = True


def statement(sql: str) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[])


def read(lake: Lake, sql: str, snapshot: int | None = None) -> pa.Table:
    pinned = lake.current_snapshot().id if snapshot is None else snapshot
    request = ReadRequest(kind="read", sql=sql, parameters=[], snapshot=pinned, batch_size=65536)
    return lake.read(request).read_all()


def write(lake: Lake, *statements: StatementRequest) -> int | None:
    with lake.transaction() as transaction:
        for item in statements:
            transaction.execute(item)
    assert transaction.result is not None
    return transaction.result.snapshot.id if transaction.result.snapshot else None


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def files_of(folder: Path, table: str) -> list[Path]:
    """The Parquet files of one lake table in the data folder (`<schema>/<table>/…`)."""
    return sorted(path for path in folder.rglob("*.parquet") if path.parent.name == table)


def build(lake: Lake) -> int:
    """Six appends of 1,000 rows each to `t` (six small files) and one table `u`; the snapshot
    of the first append."""
    first = write(lake, statement("CREATE TABLE t AS SELECT i AS id FROM range(0, 1000) AS r(i)"))
    assert first is not None
    for batch in range(1, 6):
        write(
            lake,
            statement(
                f"INSERT INTO t SELECT i AS id FROM range({batch * 1000}, {(batch + 1) * 1000}) "
                "AS r(i)"
            ),
        )
    write(lake, statement("CREATE TABLE u AS SELECT 'x' AS name"))
    return first


def rows(lake: Lake, snapshot: int | None = None) -> list[int]:
    table = read(lake, "SELECT id FROM t ORDER BY id", snapshot)
    return [int(value) for value in table.column("id").to_pylist()]


def data_folder(location: LocalLakeLocation | PostgresLakeLocation | SqliteLakeLocation) -> Path:
    if isinstance(location, LocalLakeLocation):
        return Path(location.path) / "data"
    return Path(location.data_path)


def test_an_aged_lake_keeps_only_its_current_snapshot(
    lake: Lake, location: LocalLakeLocation | PostgresLakeLocation
) -> None:
    first = build(lake)
    before = rows(lake)
    folder = data_folder(location)
    assert len(files_of(folder, "t")) >= 6
    on_disk = set(folder.rglob("*.parquet"))
    # Two days later, with one day of retention: every snapshot is older than the cutoff.
    report = lake.maintain(now() + dt.timedelta(days=1), now() - dt.timedelta(hours=1))
    assert report.files_merged >= 6
    # Only the current snapshot is left. Ids 0 … current were all committed, so every id below
    # it, and only those, expired: exactly as many as its id.
    assert report.oldest_snapshot.id == lake.current_snapshot().id
    assert report.snapshots_expired == report.oldest_snapshot.id
    assert rows(lake) == before  # the same data
    assert read(lake, "SELECT name FROM u").column("name").to_pylist() == ["x"]
    remaining = files_of(folder, "t")
    assert len(remaining) == 1
    # Exactly the files that left the disk (the six small ones, and nothing else).
    assert report.files_deleted == len(on_disk - set(folder.rglob("*.parquet")))
    assert report.files_deleted >= 6
    with pytest.raises(StoreError) as expired:
        rows(lake, first)
    assert expired.value.code == "STORE_SNAPSHOT_EXPIRED"
    assert str(report.oldest_snapshot.id) in expired.value.message
    with pytest.raises(StoreError) as missing:
        rows(lake, 999_999)
    assert missing.value.code == "STORE_SNAPSHOT_NOT_FOUND"


def test_snapshots_inside_retention_stay_exactly_readable(lake: Lake) -> None:
    first = build(lake)
    at_first = rows(lake, first)
    assert at_first == list(range(1000))
    # Ninety days of retention: nothing is old enough to expire.
    report = lake.maintain(now() - dt.timedelta(days=90), now() - dt.timedelta(hours=1))
    assert report.snapshots_expired == 0
    assert report.files_merged >= 6
    assert report.oldest_snapshot.id <= first
    assert rows(lake, first) == at_first  # time travel survives merging (law 4)
    assert rows(lake) == list(range(6000))


def test_orphaned_files_are_deleted_after_their_grace_period(
    lake: Lake, location: LocalLakeLocation | PostgresLakeLocation
) -> None:
    write(lake, statement("CREATE TABLE t AS SELECT 1 AS id"))
    folder = data_folder(location)
    old, fresh = folder / "orphan-old.parquet", folder / "orphan-fresh.parquet"
    scratch = duckdb.connect()
    for path in (old, fresh):
        scratch.execute(f"COPY (SELECT 1 AS a) TO '{path.as_posix()}' (FORMAT parquet)")
    scratch.close()
    two_hours_ago = time.time() - 2 * 3600
    os.utime(old, (two_hours_ago, two_hours_ago))
    report = lake.maintain(now() - dt.timedelta(days=90), now() - dt.timedelta(hours=1))
    assert report.orphaned_files_deleted == 1
    assert not old.exists()
    assert fresh.exists()
    assert rows(lake) == [1]


def test_cutoffs_must_be_aware(lake: Lake) -> None:
    with pytest.raises(StoreError) as refused:
        lake.maintain(dt.datetime(2026, 1, 1), now())
    assert refused.value.code == "STORE_PARAMETER_INVALID"


def test_a_read_waiting_behind_maintenance_sees_its_snapshot_expired(lake: Lake) -> None:
    """A read admitted after maintenance expired its snapshot fails with the expired code."""
    first = build(lake)
    current = lake.current_snapshot().id
    held = lake.start_read(
        ReadRequest(
            kind="read", sql="SELECT id FROM t", parameters=[], snapshot=current, batch_size=65536
        )
    )  # holds an admission slot until its stream is read
    results: dict[str, object] = {}

    def maintain() -> None:
        results["report"] = lake.maintain(now() + dt.timedelta(days=1), now())

    def late_read() -> None:
        try:
            rows(lake, first)
            results["read"] = "read"
        except StoreError as error:
            results["read"] = error.code

    maintenance = threading.Thread(target=maintain)
    maintenance.start()
    time.sleep(0.5)  # maintenance now waits for the held slot
    reader = threading.Thread(target=late_read)
    reader.start()
    time.sleep(0.5)  # the read passed its first check and waits behind maintenance
    assert held.reader.read_all().num_rows == 6000  # frees the slot
    maintenance.join(120)
    reader.join(120)
    assert "report" in results
    assert results["read"] == "STORE_SNAPSHOT_EXPIRED"
