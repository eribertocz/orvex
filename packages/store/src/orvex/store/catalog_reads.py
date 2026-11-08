"""What the managed lake's own catalog says about its tables at a snapshot, read by DuckDB on a
request's cursor: each table's file statistics (#352) and what changed in it between two
snapshots (#355, #364).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import duckdb
from orvex.contracts.store.v1 import TableStatistics
from orvex.store.errors import StoreError
from orvex.store.lake_names import LAKE
from orvex.store.sql_text import identifier as _identifier

# The lake's one-row catalog query (`Lake._one`), on a request's cursor.
type One = Callable[[str, dict[str, object], duckdb.DuckDBPyConnection], tuple[object, ...] | None]

# An entry of `ducklake_snapshot_changes.changes_made` that names a table by its id, and the
# kinds that leave every row of the table as it was (#364).
_CHANGE = re.compile(r"([a-z_]+):([0-9]+)")
_ROWS_KEPT = frozenset({"compacted_table"})


def _table_id(
    one: One, meta: str, schema: str, table: str, snapshot: int, cursor: duckdb.DuckDBPyConnection
) -> int | None:
    alive = "{0}.begin_snapshot <= $n AND ({0}.end_snapshot IS NULL OR {0}.end_snapshot > $n)"
    row = one(
        f"SELECT min(t.table_id), count(*) FROM {meta}.ducklake_table AS t "
        f"JOIN {meta}.ducklake_schema AS s USING (schema_id) "
        "WHERE lower(s.schema_name) = lower($schema) AND lower(t.table_name) = lower($table) "
        f"AND {alive.format('t')} AND {alive.format('s')}",
        {"schema": schema, "table": table, "n": snapshot},
        cursor,
    )
    if row is None or row[1] != 1:
        return None
    return int(str(row[0]))


def _columns_of(
    one: One, meta: str, table_id: int, snapshot: int, cursor: duckdb.DuckDBPyConnection
) -> list[str]:
    alive = "{0}.begin_snapshot <= $n AND ({0}.end_snapshot IS NULL OR {0}.end_snapshot > $n)"
    row = one(
        "SELECT string_agg(c.column_name, chr(31) ORDER BY c.column_order) "
        f"FROM {meta}.ducklake_column AS c WHERE c.table_id = $id "
        f"AND c.parent_column IS NULL AND {alive.format('c')}",
        {"id": table_id, "n": snapshot},
        cursor,
    )
    return str(row[0]).split(chr(31)) if row is not None and row[0] is not None else []


def table_changes(
    one: One,
    meta: str,
    name: str,
    since: int,
    snapshot: int,
    cursor: duckdb.DuckDBPyConnection,
    count: bool = True,
) -> dict[str, Any]:
    """`changed` from the change history (rows, columns, the table's identity); and
    `removed_rows` by value: the rows of the table at `since`, as a multiset, less the rows
    it has at `snapshot`, read by DuckDB at both versions. Exact whatever happened between:
    a merge's unchanged rows, a row added then deleted, a table rebuilt with every row it
    had. Absent when a column it had at `since` is gone at `snapshot`. Without `count`, the
    catalog alone is read: `changed` only, and no row of the table (#364)."""
    schema, _, table = name.rpartition(".")
    schema = schema or "main"
    before = _table_id(one, meta, schema, table, since, cursor)
    after = _table_id(one, meta, schema, table, snapshot, cursor)
    if before is None and after is None:
        raise StoreError(
            "STORE_STATEMENT_INVALID",
            f"{name!r} is not a lake table at snapshot {since} or {snapshot}.",
            {"table": name, "snapshot": str(snapshot)},
        )
    if not count:
        changed = (
            before is None
            or after is None
            or before != after
            or (
                since != snapshot
                and _touched(one, meta, schema, table, after, since, snapshot, cursor)
            )
        )
        return {"table": name, "changed": changed}
    if before is None:  # created since: it had no rows to remove
        return {"table": name, "changed": True, "removed_rows": 0}
    relation = f"{LAKE}.{_identifier(schema)}.{_identifier(table)}"
    if after is None:  # dropped since: every row it had is gone
        gone = one(f"SELECT count(*) FROM {relation} AT (VERSION => {int(since)})", {}, cursor)
        return {
            "table": name,
            "changed": True,
            "removed_rows": int(str(gone[0])) if gone else 0,
        }
    changed = before != after or (
        since != snapshot and _touched(one, meta, schema, table, after, since, snapshot, cursor)
    )
    if not changed:
        return {"table": name, "changed": False, "removed_rows": 0}
    had = _columns_of(one, meta, before, since, cursor)
    has = {c.casefold() for c in _columns_of(one, meta, after, snapshot, cursor)}
    if not had or any(c.casefold() not in has for c in had):
        return {"table": name, "changed": True}
    listed = ", ".join(_identifier(c) for c in had)  # the catalog's own column names
    removed = one(
        f"SELECT count(*) FROM (SELECT {listed} FROM {relation} AT (VERSION => {int(since)}) "
        f"EXCEPT ALL SELECT {listed} FROM {relation} AT (VERSION => {int(snapshot)}))",
        {},
        cursor,
    )
    return {
        "table": name,
        "changed": True,
        "removed_rows": int(str(removed[0])) if removed is not None else 0,
    }


def _touched(
    one: One,
    meta: str,
    schema: str,
    table: str,
    table_id: int,
    since: int,
    snapshot: int,
    cursor: duckdb.DuckDBPyConnection,
) -> bool:
    """Any row or column of one table changed after `since` up to `snapshot`, from the
    catalog alone (#364): every snapshot in that range lists what it changed
    (`ducklake_snapshot_changes`), and any entry naming the table's id is a change, an
    inlined insert or delete included; so is a column added, changed or dropped. Only
    merging files (`compacted_table`) leaves every row as it was. No data file is opened,
    and an entry this reader does not know counts as a change (fail closed)."""
    moved = (
        "(({0}.begin_snapshot > $since AND {0}.begin_snapshot <= $n) "
        "OR ({0}.end_snapshot > $since AND {0}.end_snapshot <= $n))"
    )
    found = one(
        f"SELECT (SELECT string_agg(s.changes_made, ',') FROM {meta}.ducklake_snapshot_changes "
        "AS s WHERE s.snapshot_id > $since AND s.snapshot_id <= $n), "
        f"(SELECT count(*) FROM {meta}.ducklake_column AS c "
        f"WHERE c.table_id = $id AND {moved.format('c')})",
        {"id": table_id, "since": since, "n": snapshot},
        cursor,
    )
    if found is None:
        return True  # nothing answered: changed (fail closed)
    if int(str(found[1])) > 0:
        return True
    for kind, named in _CHANGE.findall(str(found[0] or "")):
        if int(named) == table_id and kind not in _ROWS_KEPT:
            return True
    return False


def table_statistics(
    one: One, meta: str, name: str, snapshot: int, cursor: duckdb.DuckDBPyConnection
) -> TableStatistics:
    """One table's statistics as one JSON row computed by DuckDB on the catalog: its id, its
    files with their columns, and its inlined-data tables."""
    schema, _, table = name.rpartition(".")
    alive = "{0}.begin_snapshot <= $n AND ({0}.end_snapshot IS NULL OR {0}.end_snapshot > $n)"
    row = one(
        f"WITH t AS (SELECT t.table_id FROM {meta}.ducklake_table AS t "
        f"JOIN {meta}.ducklake_schema AS s USING (schema_id) "
        f"WHERE lower(s.schema_name) = lower($schema) AND lower(t.table_name) = lower($table) "
        f"AND {alive.format('t')} AND {alive.format('s')}), "
        f"c AS (SELECT c.column_id, c.column_name FROM {meta}.ducklake_column AS c "
        f"WHERE c.table_id IN (SELECT table_id FROM t) AND c.parent_column IS NULL "
        f"AND {alive.format('c')}), "
        f"f AS (SELECT f.data_file_id, f.record_count, f.file_size_bytes, f.file_order "
        f"FROM {meta}.ducklake_data_file AS f WHERE f.table_id IN (SELECT table_id FROM t) "
        f"AND {alive.format('f')}) "
        "SELECT (SELECT count(*) FROM t), "
        "(SELECT CAST(to_json(list({'rows': f.record_count, 'bytes': f.file_size_bytes, "
        "'columns': (SELECT list({'column': c.column_name, "
        "'min': CASE WHEN st.min_is_exact THEN st.min_value END, "
        "'max': CASE WHEN st.max_is_exact THEN st.max_value END, "
        "'nulls': st.null_count} ORDER BY c.column_id) "
        f"FROM {meta}.ducklake_file_column_stats AS st JOIN c USING (column_id) "
        "WHERE st.data_file_id = f.data_file_id)} "
        "ORDER BY f.file_order, f.data_file_id)) AS VARCHAR) FROM f), "
        f"(SELECT string_agg(i.table_name, '|' ORDER BY i.schema_version) "
        f"FROM {meta}.ducklake_inlined_data_tables AS i "
        "WHERE i.table_id IN (SELECT table_id FROM t))",
        {"schema": schema or "main", "table": table, "n": snapshot},
        cursor,
    )
    if row is None or row[0] != 1:
        raise StoreError(
            "STORE_STATEMENT_INVALID",
            f"{name!r} is not a lake table at snapshot {snapshot}.",
            {"table": name, "snapshot": str(snapshot)},
        )
    files = json.loads(str(row[1])) if row[1] is not None else []
    inlined = 0
    for inlined_table in str(row[2]).split("|") if row[2] else ():
        # The catalog's own table name, read from the catalog: quoted as an identifier.
        found = one(
            f"SELECT count(*) FROM {meta}.{_identifier(inlined_table)} AS i "
            "WHERE i._ducklake_begin_snapshot <= $n "
            "AND (i._ducklake_end_snapshot IS NULL OR i._ducklake_end_snapshot > $n)",
            {"n": snapshot},
            cursor,
        )
        inlined += int(str(found[0])) if found is not None else 0
    complete = all(f["rows"] is not None for f in files)
    return TableStatistics.model_validate(
        {
            "table": name,
            "complete": complete,
            "files": [
                {
                    "rows": f["rows"] or 0,
                    "bytes": f["bytes"] or 0,
                    "columns": [
                        {k: v for k, v in c.items() if v is not None} for c in f["columns"] or ()
                    ],
                }
                for f in files
            ],
            "inlined_rows": inlined,
        }
    )
