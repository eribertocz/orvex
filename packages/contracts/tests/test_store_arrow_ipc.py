"""#486 G5: Arrow crosses the store's contract as IPC streams, exactly and batch by batch."""

from __future__ import annotations

from decimal import Decimal

import pyarrow as pa
from orvex.contracts.store.arrow_ipc import batch_bytes, one_batch, stream_bytes, stream_reader


def test_ipc_carries_arrow_exactly_and_lazily() -> None:
    metadata = {b"duckdb_type": b"DECIMAL(38,10)"}
    field = pa.field("amount", pa.decimal128(38, 10), metadata=metadata)
    batch = pa.record_batch(
        [pa.array([Decimal("12345678901234567890.1234567890"), None], pa.decimal128(38, 10))],
        schema=pa.schema([field]),
    )
    assert stream_reader(stream_bytes(batch.schema)).schema == batch.schema
    crossed = one_batch(batch_bytes(batch))
    assert crossed.equals(batch)
    assert crossed.schema.equals(batch.schema, check_metadata=True)
    reader = stream_reader(batch_bytes(batch))
    assert isinstance(reader, pa.RecordBatchReader)  # a stream: batches are read on demand
    assert reader.read_next_batch().equals(batch)


def test_a_dictionary_column_crosses_in_its_own_stream() -> None:
    batch = pa.record_batch({"zone": pa.array(["north", "south", "north"]).dictionary_encode()})
    assert one_batch(batch_bytes(batch)).equals(batch)
