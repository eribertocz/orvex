"""What a run reads from the lake: a model's table columns, its watermark and `$since`, its rows
in the run's transaction, and the snapshot that committed the run's records."""

from __future__ import annotations

from orvex.contracts.store.v1 import ReadRequest, StatementRequest
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Transaction
from orvex.transform import incremental
from orvex.transform.incremental import Columns
from orvex.transform.models import Model
from orvex.transform.run_records import committed_request


def _since(
    lake: Lake, model: Model, output: Columns, table: Columns | None, full_refresh: bool
) -> tuple[str | None, str]:
    """The model's previous watermark and its `$since`.

    No table, or a full refresh: no watermark, `$since` is `-infinity` (every row loads). Else
    the watermark stored with its table; for a table written before watermarks were stored, the
    largest `time_column` in it.
    """
    incremental_config = model.config.incremental
    assert incremental_config is not None
    time_column = incremental_config.time_column.root.lower()
    kind = {name.lower(): typ for name, typ in output}.get(time_column, "")
    watermark: str | None = None
    if table is not None and not full_refresh:
        snapshot = lake.current_snapshot().id
        try:
            rows = _column(lake, incremental.watermark_request(snapshot, model))
        except StoreError as error:
            if error.code != "STORE_STATEMENT_INVALID":
                raise
            rows = []  # no watermarks table yet
        if not rows and kind in incremental.PARAMETER_TYPES:
            rows = _column(lake, incremental.table_watermark_request(snapshot, model, kind))
        watermark = rows[0] if rows else None
    return watermark, incremental.since_value(kind, watermark, incremental_config.lookback)


def _column(lake: Lake, request: ReadRequest) -> list[str | None]:
    values: list[str | None] = lake.read(request).read_all().column("watermark").to_pylist()
    return values


def _table_columns(lake: Lake, model: Model) -> Columns | None:
    """The columns of a model's table in the lake now, or None when it does not exist."""
    request = ReadRequest(
        kind="read",
        sql=f'SELECT * FROM "{model.schema}"."{model.table}" LIMIT 0',
        parameters=[],
        snapshot=lake.current_snapshot().id,
        batch_size=1,
    )
    try:
        schema = lake.read(request).schema
    except StoreError as error:
        if error.code == "STORE_STATEMENT_INVALID":
            return None  # not a table of the lake (yet)
        raise
    return [
        (field.name, (field.metadata or {}).get(b"duckdb_type", b"").decode()) for field in schema
    ]


def _count(transaction: Transaction, model: Model) -> str | None:
    """The rows of `model`'s table as this transaction leaves them, exact digits (#394)."""
    return transaction.value(
        StatementRequest(
            kind="statement",
            sql=f'SELECT count(*) FROM "{model.schema}"."{model.table}"',
            parameters=[],
        )
    )


def _committed_snapshot(lake: Lake, run_id: str) -> int | None:
    """The snapshot holding run `run_id`'s success records, or None when it committed none: a
    run stopped after its block ran learns from its own records whether the commit happened
    (#394). None too when that cannot be read: the run is then recorded as rolled back."""
    try:
        tables = lake.tables()  # before the snapshot: record tables are never dropped
        if ("_meta", "run_records_v2") not in tables:
            return None
        request = committed_request(lake.current_snapshot().id, run_id)
        found: int | None = lake.read(request).read_all().column("snapshot")[0].as_py()
    except StoreError:
        return None
    return found
