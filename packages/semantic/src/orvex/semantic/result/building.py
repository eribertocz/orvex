"""The semantic result: `build_result`, its envelope, rows, Arrow and canonical forms (P2-06).

`build_result` fails closed: a table, descriptors or states that break `semantic-result` v1
raise `InvalidResult`, never a partial result. Rows are produced by `encode_rows` from the one
`store/v1` `ResultJson` encoder, passed in by the composing module, with the one overlay of
masks and cell states made here; this module never encodes a value itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.compute as pc
from jsonschema import Draft202012Validator
from orvex.semantic.canonical import canonicalize
from orvex.semantic.result.descriptors import (
    KNOWN_STATES,
    ColumnSpec,
    InvalidResult,
    arrow_type,
    check_descriptor,
    descriptor,
    value_kind_fits,
)

if TYPE_CHECKING:
    from orvex.semantic.errors import SemanticError

SCHEMA_FILE = "semantic-result-v1.json"
DUCKDB_TYPE = b"duckdb_type"
CELL_STATES_KEY = b"semantic.cell_states"
OTHERS_ROW_KEY = b"semantic.others_row"
FIELD_KEYS = (
    "member",
    "role",
    "context",
    "apply",
    "bucket",
    "logical",
    "semantic_type",
    "unit",
    "currency",
    "exactness",
    "nullable",
    "masked",
    "mask",
)
"""Descriptor members carried in each Arrow field's metadata as `semantic.<member>`: strings as
they are, anything else as canonical JSON text. `encoding` is the `duckdb_type` key."""

type Cell = str | int | float | bool | dict[str, str] | None
type ValueEncoder = Callable[[pa.RecordBatchReader], Iterable[str]]
"""The `store/v1` `ResultJson` encoder: an Arrow stream in, the document's text fragments out."""


@cache
def schema_validator() -> Draft202012Validator:
    schema = json.loads(files("orvex.semantic.result").joinpath(SCHEMA_FILE).read_bytes())
    return Draft202012Validator(schema)


def _strict(pairs: list[tuple[str, object]]) -> dict[str, object]:
    members: dict[str, object] = {}
    for key, value in pairs:
        if key in members:
            raise InvalidResult("json", f"duplicate key {key!r}")
        members[key] = value
    return members


def _no_constant(name: str) -> object:
    raise InvalidResult("json", f"{name} is not JSON")


def strict_json(text: str) -> object:
    """JSON text with no duplicate key and no `NaN` or `Infinity` constant."""
    try:
        return json.loads(text, object_pairs_hook=_strict, parse_constant=_no_constant)
    except json.JSONDecodeError as error:
        raise InvalidResult("json", "not JSON text") from error


@dataclass(frozen=True, slots=True, order=True)
class CellState:
    """A cell with no ordinary value: `(row, column)` and the reason (`state`)."""

    row: int
    column: int
    state: str

    def to_json(self) -> dict[str, object]:
        return {"row": self.row, "column": self.column, "state": self.state}


def check_states(
    states: Sequence[CellState],
    columns: Sequence[Mapping[str, object]],
    row_count: int,
    others_row: int | None,
) -> None:
    """The normative rule of §1.4 on `cell_states` alone: sorted by row then column, at most one
    entry per cell, inside the rows and columns, never on a masked column; `others` exactly on
    the dimension cells of `others_row`, and `others_row` only with them."""
    previous: tuple[int, int] | None = None
    for entry in states:
        place = (entry.row, entry.column)
        if previous is not None and place == previous:
            raise InvalidResult("state_duplicate", f"cell {place} has more than one entry")
        if previous is not None and place < previous:
            raise InvalidResult("state_order", f"cell {place} comes after {previous}")
        previous = place
        if not (0 <= entry.row < row_count and 0 <= entry.column < len(columns)):
            raise InvalidResult("state_position", f"cell {place} is outside the result")
        if columns[entry.column].get("masked") is True:
            raise InvalidResult("state_on_masked", f"cell {place} is on a masked column")
    others = {(entry.row, entry.column) for entry in states if entry.state == "others"}
    if others_row is None:
        if others:
            raise InvalidResult("others_without_row", "others cells without others_row")
        return
    if not 0 <= others_row < row_count:
        raise InvalidResult("others_row_position", f"others_row {others_row} has no row")
    expected = {
        (others_row, index)
        for index, column in enumerate(columns)
        if column.get("role") == "dimension"
    }
    if not expected:
        raise InvalidResult("others_without_dimension", "others_row in a result without dimensions")
    if others != expected:
        raise InvalidResult(
            "others_placement",
            f"others cells {sorted(others)} are not the dimension cells {sorted(expected)}",
        )


def _metadata_text(value: object) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else canonicalize(value)


def arrow_field(column: Mapping[str, object], arrow: pa.DataType) -> pa.Field:
    metadata = {DUCKDB_TYPE: str(column["encoding"]).encode("utf-8")}
    for key in FIELD_KEYS:
        if key in column:
            metadata[f"semantic.{key}".encode()] = _metadata_text(column[key])
    # A state cell is Arrow null even in a column whose values are never NULL, so only a masked
    # column (every cell its mask) is a non-nullable Arrow field.
    nullable = column.get("masked") is not True
    return pa.field(str(column["name"]), arrow, nullable=nullable, metadata=metadata)


def _is_true(scalar: pa.Scalar) -> bool:
    return bool(scalar.equals(pa.scalar(True)))


def _check_column(
    column: Mapping[str, object],
    spec: ColumnSpec,
    field: pa.Field,
    data: pa.ChunkedArray,
    states_here: int,
) -> None:
    name = column["name"]
    declared = (field.metadata or {}).get(DUCKDB_TYPE)
    if spec.mask is not None:
        if declared not in {None, b"VARCHAR"} or not field.type.equals(pa.string()):
            raise InvalidResult("masked_values", f"column {name!r} carries a typed value")
        # min_count=0: an empty column (a page with no rows) holds nothing but the mask.
        every = pc.all(pc.equal(data, spec.mask), options=pc.ScalarAggregateOptions(min_count=0))
        if data.null_count or not _is_true(every):
            raise InvalidResult("masked_values", f"column {name!r} holds more than its mask")
        return
    encoding = str(column["encoding"])
    expected = arrow_type(encoding)
    logical = column["logical"]
    family = logical["family"] if isinstance(logical, Mapping) else None
    if pa.types.is_floating(field.type) and family != "Float":
        raise InvalidResult("exact_as_float", f"column {name!r}: exact values as {field.type}")
    if not field.type.equals(expected):
        raise InvalidResult("arrow_type", f"column {name!r}: {field.type} for {encoding}")
    if declared is not None and declared != encoding.encode("utf-8"):
        raise InvalidResult("arrow_metadata", f"column {name!r}: duckdb_type {declared!r}")
    if not spec.nullable and data.null_count > states_here:
        raise InvalidResult("null_in_non_nullable", f"column {name!r} is not nullable")


class SemanticResult:
    """One answer of the semantic engine, immutable: descriptors and the Arrow table, the sparse
    cell states, `others_row` and the notices, the page and the fingerprints."""

    __slots__ = ("_envelope", "_states", "_table")

    def __init__(self, envelope: bytes, table: pa.Table, states: tuple[CellState, ...]) -> None:
        self._envelope = envelope
        self._table = table
        self._states = states

    @property
    def cell_states(self) -> tuple[CellState, ...]:
        return self._states

    @property
    def others_row(self) -> int | None:
        others_row = self.envelope().get("others_row")
        return others_row if isinstance(others_row, int) else None

    def envelope(self) -> dict[str, Any]:
        """Every member of the JSON form but `rows`, as a new document on each call."""
        document = json.loads(self._envelope)
        if not isinstance(document, dict):
            raise InvalidResult("envelope", "the envelope is an object")
        return document

    def arrow(self) -> pa.Table:
        """The Arrow form: `store/v1` types with their `duckdb_type`, the descriptor under
        neutral `semantic.*` keys, and `cell_states` and `others_row` in the schema metadata."""
        return self._table

    def encode_rows(self, value_encoder: ValueEncoder) -> list[list[Cell]]:
        """`rows`: the `ResultJson` values of `value_encoder`, with every cell of a masked
        column as its mask and every cell of `cell_states` as its state object."""
        columns = self.envelope()["columns"]
        if not isinstance(columns, list):
            raise InvalidResult("envelope", "the envelope lists its columns")
        table = self._table
        text = "".join(
            value_encoder(pa.RecordBatchReader.from_batches(table.schema, table.to_batches()))
        )
        document = strict_json(text)
        expected = [{"name": c["name"], "type": c["encoding"]} for c in columns]
        if (
            not isinstance(document, dict)
            or document.get("kind") != "result_json"
            or document.get("columns") != expected
            or not isinstance(document.get("rows"), list)
            or len(document["rows"]) != table.num_rows
        ):
            raise InvalidResult("encoder", "the value encoder did not return this table's rows")
        states = {(entry.row, entry.column): entry.state for entry in self._states}
        rows: list[list[Cell]] = []
        for row_index, values in enumerate(document["rows"]):
            if not isinstance(values, list) or len(values) != len(columns):
                raise InvalidResult("encoder", f"row {row_index} has the wrong width")
            row: list[Cell] = []
            for column_index, (value, column) in enumerate(zip(values, columns, strict=True)):
                state = states.get((row_index, column_index))
                if column.get("masked") is True:
                    if value != column["mask"]:
                        raise InvalidResult("masked_values", f"row {row_index} unmasked")
                    row.append(column["mask"])
                elif state is not None:
                    if value is not None:
                        raise InvalidResult("state_not_null", f"cell {row_index, column_index}")
                    row.append({"state": state})
                elif value is None:
                    if column["nullable"] is not True:
                        raise InvalidResult("null_in_non_nullable", f"row {row_index}")
                    row.append(None)
                elif value_kind_fits(column["encoding"], value):
                    row.append(value)
                else:
                    raise InvalidResult("value_kind", f"cell {row_index, column_index}: {value!r}")
            rows.append(row)
        return rows

    def wire(self, value_encoder: ValueEncoder) -> str:
        """The JSON form: the envelope with `rows`, exactly in the `ResultJson` encoding."""
        document = self.envelope()
        document["rows"] = self.encode_rows(value_encoder)
        return json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    def canonical(self, value_encoder: ValueEncoder) -> bytes:
        """The canonical form (RFC 8785) of the JSON form, with every number cell as its text:
        integers in decimal, floats as their shortest round-trip text (keeping `-0.0`)."""
        document = self.envelope()
        document["rows"] = [
            [canonical_cell(cell) for cell in row] for row in self.encode_rows(value_encoder)
        ]
        return canonicalize(document)


def canonical_cell(cell: Cell) -> Cell:
    if isinstance(cell, bool) or not isinstance(cell, int | float):
        return cell
    return str(cell) if isinstance(cell, int) else repr(cell)


def build_result(
    table: pa.Table,
    columns: Sequence[ColumnSpec],
    *,
    mode: str,
    query_fingerprint: str,
    model_fingerprint: str,
    page: Mapping[str, object],
    cell_states: Sequence[CellState] = (),
    others_row: int | None = None,
    notices: Sequence[SemanticError] = (),
    locale: str | None = None,
    audience: str | None = None,
) -> SemanticResult:
    """The result of `table` described by `columns`, or `InvalidResult`.

    `notices` are warnings and notices, rendered in the caller's `audience` view and `locale`.
    """
    descriptors = [descriptor(spec) for spec in columns]
    if table.num_columns != len(columns) or table.column_names != [s.name for s in columns]:
        raise InvalidResult("columns", f"{table.column_names} for {[s.name for s in columns]}")
    if len(set(table.column_names)) != len(columns):
        raise InvalidResult("duplicate_name", f"repeated column names {table.column_names}")
    for column in descriptors:
        check_descriptor(column, mode)
    states = tuple(cell_states)
    for entry in states:
        if entry.state not in KNOWN_STATES:
            raise InvalidResult("state_unknown", f"{entry.state!r} is not a state of v1")
    check_states(states, descriptors, table.num_rows, others_row)
    per_column = [0] * len(columns)
    for entry in states:
        per_column[entry.column] += 1
        if table.column(entry.column)[entry.row].is_valid:
            raise InvalidResult("state_not_null", f"cell {entry.row, entry.column} holds a value")
    fields = []
    for index, (spec, column) in enumerate(zip(columns, descriptors, strict=True)):
        field = table.schema.field(index)
        _check_column(column, spec, field, table.column(index), per_column[index])
        fields.append(arrow_field(column, field.type))
    messages = []
    for notice in notices:
        if notice.severity == "error":
            raise InvalidResult("notice_severity", f"{notice.code} is an error, not a notice")
        messages.append(
            notice.to_message(locale, audience).model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
        )
    state_list = [entry.to_json() for entry in states]
    envelope: dict[str, object] = {
        "kind": "semantic_result",
        "version": 1,
        "mode": mode,
        "query_fingerprint": query_fingerprint,
        "model_fingerprint": model_fingerprint,
        "columns": descriptors,
        "row_count": table.num_rows,
        "cell_states": state_list,
        "page": dict(page),
        "notices": messages,
    }
    if others_row is not None:
        envelope["others_row"] = others_row
    problems = sorted(
        schema_validator().iter_errors({**envelope, "rows": []}), key=lambda e: list(e.path)
    )
    if problems:
        where = "/".join(str(part) for part in problems[0].absolute_path)
        raise InvalidResult("schema", f"/{where}: {problems[0].message}")
    metadata = {CELL_STATES_KEY: canonicalize(state_list)}
    if others_row is not None:
        metadata[OTHERS_ROW_KEY] = str(others_row).encode("ascii")
    arrow = pa.Table.from_arrays(
        [table.column(index) for index in range(table.num_columns)],
        schema=pa.schema(fields, metadata=metadata),
    )
    return SemanticResult(canonicalize(envelope), arrow, states)
