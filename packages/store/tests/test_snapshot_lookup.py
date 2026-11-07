"""P2-27a (#363): a snapshot looked up by id, only while the lake keeps it."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
from orvex.contracts.store.v1 import LakeLocation, StatementRequest
from orvex.store.lake import Lake, Limits, open_lake

LIMITS = Limits(
    memory_limit="1GB", threads=2, max_concurrent=4, queue_size=8, default_timeout_ms=60_000
)


@pytest.fixture
def lake(location: LakeLocation) -> Iterator[Lake]:
    opened = open_lake(location, LIMITS)
    yield opened
    opened.close()


def write(lake: Lake, sql: str) -> int:
    with lake.transaction() as transaction:
        transaction.execute(StatementRequest(kind="statement", sql=sql, parameters=[]))
    assert transaction.result is not None
    assert transaction.result.snapshot is not None
    return transaction.result.snapshot.id


def test_a_kept_snapshot_is_found_and_an_expired_or_unknown_one_is_not(lake: Lake) -> None:
    first = write(lake, "CREATE TABLE t AS SELECT 1 AS id")
    second = write(lake, "INSERT INTO t VALUES (2)")
    found = lake.snapshot(first)
    assert found is not None
    assert found.id == first
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    lake.maintain(later, later)
    assert lake.snapshot(first) is None  # expired by maintenance
    current = lake.current_snapshot()  # maintenance may commit one of its own
    assert lake.snapshot(current.id) == current
    for unknown in (second + 100, -1, True):
        assert lake.snapshot(unknown) is None
