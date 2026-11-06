"""#352: a snapshot's file statistics, read-only, exact, and nothing else of the catalog."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from orvex.contracts.store.v1 import (
    LakeLocation,
    StatementRequest,
    StatisticsRequest,
    StatisticsResult,
)
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


def stats(lake: Lake, snapshot: int, *tables: str) -> StatisticsResult:
    return lake.statistics(
        StatisticsRequest(kind="statistics", tables=list(tables), snapshot=snapshot)  # type: ignore[arg-type]
    )


FILLED = (
    "CREATE TABLE t AS SELECT range AS a, CASE WHEN range % 10 = 0 THEN NULL "
    "ELSE 'v' || lpad(CAST(range AS VARCHAR), 5, '0') END AS s FROM range(0, 3000)"
)


@pytest.fixture
def two_files(lake: Lake) -> tuple[int, int]:
    first = write(lake, FILLED)
    second = write(lake, "INSERT INTO t SELECT range, NULL FROM range(3000, 5000)")
    return first, second


def test_counts_and_bounds_are_exact(lake: Lake, two_files: tuple[int, int]) -> None:
    (table,) = stats(lake, two_files[1], "t").tables
    assert table.complete
    assert table.inlined_rows == 0
    assert [f.rows for f in table.files] == [3000, 2000]
    assert all(f.bytes > 0 for f in table.files)
    columns = [{c.column: (c.min, c.max, c.nulls) for c in f.columns} for f in table.files]
    assert columns == [
        {"a": ("0", "2999", 0), "s": ("v00001", "v02999", 300)},
        {"a": ("3000", "4999", 0), "s": (None, None, 2000)},
    ]


def test_statistics_are_those_of_the_snapshot_asked(lake: Lake, two_files: tuple[int, int]) -> None:
    first, second = two_files
    assert [f.rows for f in stats(lake, first, "t").tables[0].files] == [3000]
    assert [f.rows for f in stats(lake, second, "main.t").tables[0].files] == [3000, 2000]
    dropped = write(lake, "DROP TABLE t")
    with pytest.raises(StoreError) as refused:
        stats(lake, dropped, "t")
    assert refused.value.code == "STORE_STATEMENT_INVALID"
    assert [f.rows for f in stats(lake, second, "t").tables[0].files] == [3000, 2000]


def test_deleted_rows_keep_the_count_an_upper_bound(lake: Lake, two_files: tuple[int, int]) -> None:
    after = write(lake, "DELETE FROM t WHERE a < 1000")
    assert [f.rows for f in stats(lake, after, "t").tables[0].files] == [3000, 2000]


def test_rows_kept_in_the_catalog_are_counted_at_their_snapshot(lake: Lake) -> None:
    created = write(lake, "CREATE TABLE small (a BIGINT)")
    three = write(lake, "INSERT INTO small VALUES (1), (2), (3)")
    two = write(lake, "DELETE FROM small WHERE a = 2")
    counted = {
        s: (stats(lake, s, "small").tables[0].inlined_rows, stats(lake, s, "small").tables[0].files)
        for s in (created, three, two)
    }
    assert counted == {created: (0, []), three: (3, []), two: (2, [])}


@pytest.mark.parametrize(
    "name",
    [
        "missing",
        "ducklake_data_file",  # the catalog's own tables
        "ducklake_snapshot",
        "memory.t",
        "system.t",
        "main.missing",
    ],
)
def test_only_lake_tables_are_answered(lake: Lake, two_files: tuple[int, int], name: str) -> None:
    with pytest.raises(StoreError) as refused:
        stats(lake, two_files[1], "t", name)
    assert refused.value.code == "STORE_STATEMENT_INVALID"
    assert refused.value.details == {"table": name, "snapshot": str(two_files[1])}


def test_an_unknown_snapshot_is_refused(lake: Lake, two_files: tuple[int, int]) -> None:
    with pytest.raises(StoreError) as refused:
        stats(lake, two_files[1] + 5, "t")
    assert refused.value.code == "STORE_SNAPSHOT_NOT_FOUND"


def test_no_path_or_other_catalog_detail_is_exposed(lake: Lake, two_files: tuple[int, int]) -> None:
    result = stats(lake, two_files[1], "t")
    text = result.model_dump_json()
    for detail in (".parquet", "/", "ducklake"):
        assert detail not in text
    document = json.loads(text)
    assert set(document) == {"kind", "snapshot", "tables"}
    assert set(document["tables"][0]) == {"table", "complete", "files", "inlined_rows"}
    assert {k for f in document["tables"][0]["files"] for k in f} == {"rows", "bytes", "columns"}


def test_a_value_is_never_a_statement(lake: Lake, two_files: tuple[int, int]) -> None:
    """A table name is bound, never written into SQL."""
    for name in ["t' OR '1'='1", 't"; DROP TABLE t; --']:
        with pytest.raises(StoreError):
            stats(lake, two_files[1], name)
    assert stats(lake, two_files[1], "t").tables[0].complete
