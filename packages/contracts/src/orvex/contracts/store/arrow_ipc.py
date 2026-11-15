"""How Arrow crosses the store's interface contract, on both sides: as IPC streams, exactly
(every type, its metadata and every NULL as written), never as a live reader."""

from __future__ import annotations

from collections.abc import Iterable

import pyarrow as pa


def stream_bytes(schema: pa.Schema, batches: Iterable[pa.RecordBatch] = ()) -> bytes:
    """`batches` as one IPC stream of `schema` (dictionaries included); no batch: the schema."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, schema) as writer:
        for batch in batches:
            writer.write_batch(batch)
    written: bytes = sink.getvalue().to_pybytes()
    return written


def batch_bytes(batch: pa.RecordBatch, schema: pa.Schema | None = None) -> bytes:
    """One batch as an IPC stream of its own, written with `schema` (its own by default)."""
    return stream_bytes(batch.schema if schema is None else schema, (batch,))


def stream_reader(data: bytes) -> pa.RecordBatchReader:
    """The reader of one IPC stream: its batches are decoded as they are read."""
    return pa.ipc.open_stream(pa.py_buffer(data))


def stream_schema(data: bytes) -> pa.Schema:
    return stream_reader(data).schema


def one_batch(data: bytes) -> pa.RecordBatch:
    """The one batch of a stream written by `batch_bytes`."""
    return stream_reader(data).read_next_batch()
