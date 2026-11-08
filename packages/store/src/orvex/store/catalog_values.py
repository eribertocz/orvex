"""Single values read from the lake's catalog on a cursor: one value of a one-row query, and a
snapshot from its id and commit time.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import duckdb
from orvex.contracts.store.v1 import Snapshot


def _snapshot_of(row: Sequence[object]) -> Snapshot:
    """A snapshot from (id, microseconds since the epoch): no time-zone library is needed."""
    committed = dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(microseconds=int(str(row[1])))
    return Snapshot(kind="snapshot", id=int(str(row[0])), committed_at=committed)


def _single_value(
    cursor: duckdb.DuckDBPyConnection, sql: str, parameters: dict[str, object]
) -> str:
    """One value of a one-row query on `cursor`, leaving the cursor free for the next statement."""
    result = cursor.execute(sql, parameters)
    row = result.fetchone()
    result.fetchone()  # reaches the end of the one-row result
    return "" if not row or row[0] is None else str(row[0])
