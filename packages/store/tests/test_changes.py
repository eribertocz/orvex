"""#355: what changed in lake tables between two snapshots, counted by DuckDB."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from orvex.contracts.store.v1 import ChangesRequest, LakeLocation, StatementRequest
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


def changes(lake: Lake, since: int, snapshot: int, *tables: str) -> list[tuple[bool, int | None]]:
    found = lake.changes(
        ChangesRequest.model_validate(
            {"kind": "changes", "tables": list(tables), "since": since, "snapshot": snapshot}
        )
    )
    return [(t.changed, t.removed_rows) for t in found.tables]


@pytest.fixture
def base(lake: Lake) -> int:
    return write(
        lake,
        "CREATE TABLE t AS SELECT range AS id, range * 10 AS v FROM range(50)",
        "CREATE TABLE other (x INTEGER)",
    )


def test_rows_added_elsewhere_change_nothing(lake: Lake, base: int) -> None:
    later = write(lake, "INSERT INTO other VALUES (1)")
    assert changes(lake, base, later, "t", "other") == [(False, 0), (True, 0)]
    assert changes(lake, base, base, "t") == [(False, 0)]


@pytest.mark.parametrize(
    ("statements", "expected"),
    [
        (("INSERT INTO t VALUES (100, 1)",), (True, 0)),
        (("DELETE FROM t WHERE id < 3",), (True, 3)),
        (("UPDATE t SET v = -1 WHERE id = 7",), (True, 1)),
        (("UPDATE t SET v = 70 WHERE id = 7",), (True, 0)),  # the same value
        (("DELETE FROM t WHERE id = 8", "INSERT INTO t VALUES (8, 80)"), (True, 0)),
        (("INSERT INTO t VALUES (200, 1)", "DELETE FROM t WHERE id = 200"), (True, 0)),
        (("ALTER TABLE t ADD COLUMN w INTEGER",), (True, 0)),
        (("DROP TABLE t", "CREATE TABLE t AS SELECT 1 AS id"), (True, None)),  # lost `v`
        (  # rebuilt with every row it had, and more: nothing removed
            ("CREATE OR REPLACE TABLE t AS SELECT range AS id, range * 10 AS v FROM range(60)",),
            (True, 0),
        ),
        (  # rebuilt without some of its rows
            ("CREATE OR REPLACE TABLE t AS SELECT range AS id, range * 10 AS v FROM range(45)",),
            (True, 5),
        ),
        (  # a baseline row inserted again then deleted in another snapshot: one is still gone
            ("INSERT INTO t VALUES (0, 0)", "DELETE FROM t WHERE id = 0"),
            (True, 1),
        ),
        (("ALTER TABLE t DROP COLUMN v",), (True, None)),
    ],
)
def test_changes_are_counted_by_value_over_every_snapshot_between(
    lake: Lake, base: int, statements: tuple[str, ...], expected: tuple[bool, int | None]
) -> None:
    for statement in statements:
        later = write(lake, statement)
    assert changes(lake, base, later, "t") == [expected]


def test_a_dropped_table_lost_every_row_and_one_neither_snapshot_has_is_refused(
    lake: Lake, base: int
) -> None:
    later = write(lake, "DROP TABLE t")
    assert changes(lake, base, later, "t") == [(True, 50)]
    created = write(lake, "CREATE TABLE fresh (x INTEGER)", "INSERT INTO fresh VALUES (1)")
    assert changes(lake, later, created, "fresh") == [(True, 0)]
    with pytest.raises(StoreError) as refused:
        changes(lake, base, later, "missing")
    assert refused.value.code == "STORE_STATEMENT_INVALID"


def test_snapshots_out_of_order_or_unknown_are_refused(lake: Lake, base: int) -> None:
    later = write(lake, "INSERT INTO t VALUES (100, 1)")
    with pytest.raises(StoreError) as backwards:
        changes(lake, later, base, "t")
    assert backwards.value.code == "STORE_STATEMENT_INVALID"
    with pytest.raises(StoreError) as unknown:
        changes(lake, base, later + 5, "t")
    assert unknown.value.code == "STORE_SNAPSHOT_NOT_FOUND"


def test_a_value_is_never_a_statement(lake: Lake, base: int) -> None:
    for name in ["t' OR '1'='1", 't"; DROP TABLE t; --']:
        with pytest.raises(StoreError):
            changes(lake, base, base, name)
    assert changes(lake, base, base, "t") == [(False, 0)]


@pytest.mark.parametrize(
    "statements",
    [
        ("INSERT INTO t VALUES (100, 1)",),
        ("DELETE FROM t WHERE id < 3",),
        ("ALTER TABLE t DROP COLUMN v",),
        ("DROP TABLE t",),
        ("CREATE OR REPLACE TABLE t AS SELECT range AS id, range * 10 AS v FROM range(45)",),
    ],
)
def test_without_counts_only_changed_is_answered_from_the_catalog(
    lake: Lake, base: int, statements: tuple[str, ...]
) -> None:
    """#364: `count_removed: false` reads no row: `changed` as with counts, no `removed_rows`."""
    later = write(lake, *statements)
    found = lake.changes(
        ChangesRequest.model_validate(
            {
                "kind": "changes",
                "tables": ["t", "other"],
                "since": base,
                "snapshot": later,
                "count_removed": False,
            }
        )
    )
    counted = changes(lake, base, later, "t", "other")
    assert [t.changed for t in found.tables] == [c for c, _ in counted]
    assert all(t.removed_rows is None for t in found.tables)


def test_without_counts_no_data_file_is_opened(lake: Lake, base: int, tmp_path: Path) -> None:
    """#364: with every data file of the lake gone, `changed` is still answered, from the
    catalog alone; counting removed rows would have to open them."""
    later = write(lake, "INSERT INTO t SELECT range + 1000, 1 FROM range(5000)")
    files = list(tmp_path.rglob("*.parquet"))
    if not files:
        pytest.skip("this catalog keeps its data elsewhere")
    for path in files:
        path.unlink()
    found = lake.changes(
        ChangesRequest.model_validate(
            {
                "kind": "changes",
                "tables": ["t", "other"],
                "since": base,
                "snapshot": later,
                "count_removed": False,
            }
        )
    )
    assert [(t.changed, t.removed_rows) for t in found.tables] == [(True, None), (False, None)]
