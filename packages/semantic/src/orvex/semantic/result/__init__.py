"""Semantic results (`semantic-result` v1, P2-06): building, encoding, reading and cursors."""

from orvex.semantic.result.building import CellState, SemanticResult, ValueEncoder, build_result
from orvex.semantic.result.cursors import (
    CursorBinding,
    CursorForm,
    CursorState,
    SnapshotStatus,
    issue_cursor,
    verify_cursor,
)
from orvex.semantic.result.descriptors import KNOWN_STATES, ColumnSpec, InvalidResult
from orvex.semantic.result.reading import NoValue, ReadCell, ReadResult, read_result

__all__ = [
    "KNOWN_STATES",
    "CellState",
    "ColumnSpec",
    "CursorBinding",
    "CursorForm",
    "CursorState",
    "InvalidResult",
    "NoValue",
    "ReadCell",
    "ReadResult",
    "SemanticResult",
    "SnapshotStatus",
    "ValueEncoder",
    "build_result",
    "issue_cursor",
    "read_result",
    "verify_cursor",
]
