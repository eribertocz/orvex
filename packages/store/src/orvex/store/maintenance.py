"""The managed lake's maintenance steps (DuckLake's own `CHECKPOINT` order) and the report of what
each one did.
"""

from __future__ import annotations

import duckdb
from orvex.contracts.store.v1 import MaintenanceReport
from orvex.store.catalog_values import _single_value, _snapshot_of
from orvex.store.errors import StoreError
from orvex.store.lake_names import LAKE


def maintained(cursor: duckdb.DuckDBPyConnection, cutoff: str, orphans: str) -> MaintenanceReport:
    """DuckLake's own `CHECKPOINT` steps on `cursor`, each one counted in SQL: flush inlined data,
    expire the snapshots before `cutoff`, merge, rewrite, expire again, clean up, and delete the
    orphaned files older than `orphans`. The caller runs it alone (`Lake.maintain`)."""
    lake: dict[str, object] = {"lake": LAKE}
    expire = (
        "SELECT count(*) FROM ducklake_expire_snapshots("
        "$lake, older_than => CAST($cutoff AS TIMESTAMPTZ))"
    )
    compact = (
        "SELECT coalesce(sum(files_processed), 0) || ',' || coalesce(sum(files_created), 0) "
        "FROM {function}($lake)"
    )
    try:
        _single_value(cursor, "SELECT count(*) FROM ducklake_flush_inlined_data($lake)", lake)
        expired = int(_single_value(cursor, expire, {**lake, "cutoff": cutoff}))
        merged, created = _single_value(
            cursor, compact.format(function="ducklake_merge_adjacent_files"), lake
        ).split(",")
        rewritten, rewritten_into = _single_value(
            cursor, compact.format(function="ducklake_rewrite_data_files"), lake
        ).split(",")
        expired += int(_single_value(cursor, expire, {**lake, "cutoff": cutoff}))
        deleted = _single_value(
            cursor,
            "SELECT count(*) FROM ducklake_cleanup_old_files($lake, cleanup_all => true)",
            lake,
        )
        orphaned = _single_value(
            cursor,
            "SELECT count(*) FROM ducklake_delete_orphaned_files("
            "$lake, older_than => CAST($orphans AS TIMESTAMPTZ))",
            {**lake, "orphans": orphans},
        )
        oldest = _snapshot_of(
            _single_value(
                cursor,
                "SELECT snapshot_id || ',' || epoch_us(snapshot_time) "
                "FROM ducklake_snapshots($lake) ORDER BY snapshot_id LIMIT 1",
                lake,
            ).split(",")
        )
    except duckdb.Error as error:
        raise StoreError(
            "STORE_QUERY_FAILED", "Lake maintenance failed.", {"duckdb": str(error)}
        ) from error
    return MaintenanceReport(
        kind="maintenance_report",
        snapshots_expired=expired,
        files_merged=int(merged),
        files_created=int(created) + int(rewritten_into),
        files_rewritten=int(rewritten),
        files_deleted=int(deleted),
        orphaned_files_deleted=int(orphaned),
        oldest_snapshot=oldest,
    )
