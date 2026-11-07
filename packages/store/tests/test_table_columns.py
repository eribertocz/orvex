"""#358: every lake table and its columns as of one snapshot, from that version only."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from orvex.contracts.store.v1 import LakeLocation, StatementRequest, TablesRequest
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, open_lake

LIMITS = Limits(
    memory_limit="1GB", threads=2, max_concurrent=4, queue_size=8, default_timeout_ms=60_000
)


@pytest.fixture
def lake(location: LakeLocation) -> Iterator[Lake]:
    opened = open_lake(location, LIMITS)
    yield opened
    opened.close()


def write(lake: Lake, *statements: str) -> int:
    with lake.transaction() as transaction:
        for sql in statements:
            transaction.execute(StatementRequest(kind="statement", sql=sql, parameters=[]))
    assert transaction.result is not None
    assert transaction.result.snapshot is not None
    return transaction.result.snapshot.id


def columns(lake: Lake, snapshot: int) -> dict[str, list[tuple[str, str, bool]]]:
    found = lake.table_columns(TablesRequest(kind="tables", snapshot=snapshot))
    assert found.snapshot == snapshot
    return {
        f"{t.schema_}.{t.table}": [(c.name, c.type, c.nullable) for c in t.columns]
        for t in found.tables
    }


def test_each_snapshot_reads_as_it_was(lake: Lake) -> None:
    first = write(
        lake,
        "CREATE SCHEMA marts",
        "CREATE TABLE marts.sales (id BIGINT NOT NULL, amount DECIMAL(18,2), note VARCHAR)",
        "CREATE TABLE gone (x INTEGER)",
    )
    second = write(
        lake,
        "ALTER TABLE marts.sales ALTER amount TYPE DECIMAL(20,2)",
        "ALTER TABLE marts.sales DROP COLUMN note",
        "DROP TABLE gone",
    )
    assert columns(lake, first) == {
        "main.gone": [("x", "INTEGER", True)],
        "marts.sales": [
            ("id", "BIGINT", False),
            ("amount", "DECIMAL(18,2)", True),
            ("note", "VARCHAR", True),
        ],
    }
    assert columns(lake, second) == {
        "marts.sales": [("id", "BIGINT", False), ("amount", "DECIMAL(20,2)", True)]
    }
    assert columns(lake, first)["main.gone"] == [("x", "INTEGER", True)]  # still as it was


def test_an_unknown_snapshot_is_refused(lake: Lake) -> None:
    current = write(lake, "CREATE TABLE t (x INTEGER)")
    with pytest.raises(StoreError) as refused:
        lake.table_columns(TablesRequest(kind="tables", snapshot=current + 3))
    assert refused.value.code == "STORE_SNAPSHOT_NOT_FOUND"


def test_no_path_or_other_catalog_detail_is_exposed(lake: Lake) -> None:
    current = write(lake, "CREATE TABLE t (x INTEGER)")
    text = lake.table_columns(TablesRequest(kind="tables", snapshot=current)).model_dump_json()
    for detail in ("/", ".parquet", "ducklake", "table_id"):
        assert detail not in text
