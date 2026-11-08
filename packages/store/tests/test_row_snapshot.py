"""#394: DuckLake's virtual column `snapshot_id`, the snapshot that inserted a row. Run records
resolve their snapshot from it, so the behavior they rely on is pinned here: DuckLake does not
document it, and a DuckDB or DuckLake update that changes it fails this test."""

from __future__ import annotations

import datetime as dt

import pyarrow as pa
import pytest
from orvex.contracts.store.v1 import ReadRequest, StatementRequest
from orvex.store.lake import Lake


def statement(sql: str) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[])


def read(lake: Lake, sql: str, snapshot: int | None = None) -> pa.Table:
    pinned = lake.current_snapshot().id if snapshot is None else snapshot
    request = ReadRequest(kind="read", sql=sql, parameters=[], snapshot=pinned, batch_size=1000)
    return lake.read(request).read_all()


def write(lake: Lake, *statements: StatementRequest) -> int:
    with lake.transaction() as transaction:
        for item in statements:
            transaction.execute(item)
    assert transaction.result is not None
    assert transaction.result.snapshot is not None
    return transaction.result.snapshot.id


def by_row(lake: Lake, snapshot: int | None = None, table: str = "t") -> list[tuple[int, int]]:
    result = read(lake, f"SELECT id, snapshot_id FROM {table} ORDER BY id", snapshot)
    return list(
        zip(result.column("id").to_pylist(), result.column("snapshot_id").to_pylist(), strict=True)
    )


def test_the_virtual_column_is_null_inside_the_open_transaction(lake: Lake) -> None:
    first = write(lake, statement("CREATE TABLE t AS SELECT 1 AS id"))
    with lake.transaction() as transaction:
        transaction.execute(statement("INSERT INTO t VALUES (2), (3)"))
        unknown = transaction.value(statement("SELECT count(*) FROM t WHERE snapshot_id IS NULL"))
        known = transaction.value(statement("SELECT max(snapshot_id) FROM t"))
    assert (unknown, known) == ("2", str(first))  # only the rows of this transaction


def test_a_pinned_read_selects_the_commit_s_id_as_bigint(lake: Lake) -> None:
    first = write(lake, statement("CREATE TABLE t AS SELECT 1 AS id"))
    second = write(
        lake, statement("INSERT INTO t VALUES (2)"), statement("INSERT INTO t VALUES (3)")
    )
    assert second > first
    assert by_row(lake) == [(1, first), (2, second), (3, second)]
    assert by_row(lake, first) == [(1, first)]  # AT (VERSION => first)
    table = read(lake, "SELECT snapshot_id FROM t")
    assert table.schema.field("snapshot_id").type == pa.int64()  # BIGINT, never a float


def test_maintenance_keeps_each_row_s_snapshot_in_an_append_only_table(lake: Lake) -> None:
    # `t` only grows (as the run records do): small files for the merge. `d` loses most of its
    # rows, so the rewrite has files to rewrite in the same maintenance.
    for table in ("t", "d"):
        write(lake, statement(f"CREATE TABLE {table} AS SELECT i AS id FROM range(0, 100) AS r(i)"))
        for batch in range(1, 6):
            write(
                lake,
                statement(
                    f"INSERT INTO {table} SELECT i AS id "
                    f"FROM range({batch * 100}, {(batch + 1) * 100}) AS r(i)"
                ),
            )
    deleted = write(lake, statement("DELETE FROM d WHERE id % 100 <> 0"))
    before = by_row(lake)
    assert len({snapshot for _, snapshot in before}) == 6
    now = dt.datetime.now(dt.UTC)
    # Every snapshot but the current one expires, including those that inserted the rows.
    report = lake.maintain(now + dt.timedelta(days=1), now - dt.timedelta(hours=1))
    assert report.files_merged > 0
    assert report.files_rewritten > 0
    assert report.snapshots_expired > 0
    assert by_row(lake) == before
    # Measured limit: rows of a file rewritten for its deletes take the rewrite's snapshot.
    # Run records are never deleted, so their files are never rewritten.
    assert {snapshot for _, snapshot in by_row(lake, table="d")} == {lake.current_snapshot().id}
    assert lake.current_snapshot().id > deleted


def test_a_real_column_of_the_same_name_hides_it(lake: Lake) -> None:
    write(
        lake,
        statement("CREATE TABLE u (snapshot_id BIGINT)"),
        statement("INSERT INTO u VALUES (77)"),
    )
    assert read(lake, "SELECT snapshot_id FROM u").column("snapshot_id").to_pylist() == [77]


@pytest.mark.parametrize("sql", ["SELECT snapshot_id FROM t", "SELECT t.snapshot_id FROM t"])
def test_the_store_s_read_guard_accepts_the_virtual_column(lake: Lake, sql: str) -> None:
    snapshot = write(lake, statement("CREATE TABLE t AS SELECT 1 AS id"))
    assert read(lake, sql).column("snapshot_id").to_pylist() == [snapshot]
