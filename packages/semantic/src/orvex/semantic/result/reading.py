"""The reference reader of `semantic-result` v1 (P2-06).

It checks what the schema checks and every rule that needs a cell's position, which a JSON
Schema cannot express (README, "What the schema checks, and what the reader adds"), and, given
the Arrow form, that Arrow states exactly the same cells. A state it does not know is
*unavailable*: never `NULL`, zero, empty or a value, so never used in a calculation, a total or
a sort.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pyarrow as pa
from orvex.semantic.canonical import canonicalize
from orvex.semantic.result.building import (
    CELL_STATES_KEY,
    DUCKDB_TYPE,
    OTHERS_ROW_KEY,
    ROW_ROLES_KEY,
    CellState,
    arrow_field,
    check_row_roles,
    check_states,
    schema_validator,
    strict_json,
)
from orvex.semantic.result.descriptors import (
    KNOWN_STATES,
    InvalidResult,
    arrow_type,
    check_descriptor,
    value_kind_fits,
)

type Kind = Literal["value", "null", "mask", "state", "unavailable"]


class NoValue(LookupError):
    """A cell that is not an ordinary value was asked for one."""


@dataclass(frozen=True, slots=True)
class ReadCell:
    """One cell as the reader understands it. Only a `value` cell has a value: asking any other
    kind for one raises `NoValue`, so a `NULL`, a mask or a state never stands in for data."""

    kind: Kind
    state: str | None = None
    _value: object = None

    @property
    def value(self) -> object:
        if self.kind != "value":
            raise NoValue(f"a {self.kind} cell has no value")
        return self._value


@dataclass(frozen=True, slots=True)
class ReadResult:
    envelope: Mapping[str, Any]
    cells: tuple[tuple[ReadCell, ...], ...]


def _cell(cell: object, column: Mapping[str, Any], state: str | None) -> ReadCell:
    if state is not None:
        return ReadCell("state" if state in KNOWN_STATES else "unavailable", state=state)
    if column.get("masked") is True:
        return ReadCell("mask")
    if cell is None:
        return ReadCell("null")
    return ReadCell("value", _value=cell)


def _check_cells(
    rows: Sequence[object], columns: list[Mapping[str, Any]], states: dict[tuple[int, int], str]
) -> None:
    for row_index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != len(columns):
            raise InvalidResult("row_width", f"row {row_index} has the wrong width")
        for column_index, (cell, column) in enumerate(zip(row, columns, strict=True)):
            place = (row_index, column_index)
            state = states.get(place)
            if isinstance(cell, dict):
                if state is None:
                    raise InvalidResult("state_without_entry", f"cell {place}")
                if cell != {"state": state}:
                    raise InvalidResult("state_disagrees", f"cell {place}")
            elif state is not None:
                raise InvalidResult("entry_without_state", f"cell {place} is not its state")
            elif column.get("masked") is True:
                if cell != column["mask"]:
                    reason = "masked_null" if cell is None else "masked_values"
                    raise InvalidResult(reason, f"cell {place} is not the mask")
            elif cell is None:
                if column["nullable"] is not True:
                    raise InvalidResult("null_in_non_nullable", f"cell {place}")
            elif not value_kind_fits(str(column["encoding"]), cell):
                raise InvalidResult("value_kind", f"cell {place}: {column['encoding']}")


def _check_arrow(
    table: pa.Table,
    envelope: Mapping[str, object],
    columns: list[Mapping[str, Any]],
    rows: list[list[object]],
    entries: list[dict[str, Any]],
) -> None:
    if table.num_rows != len(rows) or table.column_names != [c["name"] for c in columns]:
        raise InvalidResult("arrow_shape", "the Arrow form has other rows or columns")
    metadata = table.schema.metadata or {}
    others_row = envelope.get("others_row")
    expected = {CELL_STATES_KEY: canonicalize(entries)}
    if others_row is not None:
        expected[OTHERS_ROW_KEY] = str(others_row).encode("ascii")
    row_roles = envelope.get("row_roles")
    if row_roles is not None:
        expected[ROW_ROLES_KEY] = canonicalize(row_roles)
    if metadata != expected:
        raise InvalidResult("arrow_metadata", "the Arrow schema metadata differs from the envelope")
    states = {(e["row"], e["column"]) for e in entries}
    for index, column in enumerate(columns):
        field = table.schema.field(index)
        if not field.type.equals(arrow_type(str(column["encoding"]))):
            raise InvalidResult("arrow_type", f"column {column['name']!r}: {field.type}")
        if not arrow_field(column, field.type).equals(field, check_metadata=True):
            raise InvalidResult("arrow_metadata", f"column {column['name']!r}")
        if (field.metadata or {}).get(DUCKDB_TYPE) != str(column["encoding"]).encode():
            raise InvalidResult("arrow_metadata", f"column {column['name']!r}: duckdb_type")
        data = table.column(index)
        for row_index, row in enumerate(rows):
            valid = data[row_index].is_valid
            if (row_index, index) in states:
                if valid:
                    raise InvalidResult("state_not_null", f"cell {row_index, index} in Arrow")
            elif valid == (row[index] is None):
                raise InvalidResult("arrow_disagrees", f"cell {row_index, index}")


def read_result(
    document: str | Mapping[str, object],
    arrow: pa.Table | None = None,
    receipt: Mapping[str, Any] | None = None,
) -> ReadResult:
    """The cells of a `semantic-result` v1 document, or `InvalidResult`. Given its Arrow form,
    it also checks that both forms state the same cells; given a view of its receipt, that the
    result's reference names that receipt."""
    parsed = strict_json(document) if isinstance(document, str) else document
    problems = sorted(schema_validator().iter_errors(parsed), key=lambda e: list(e.path))
    if problems:
        where = "/".join(str(part) for part in problems[0].absolute_path)
        raise InvalidResult("schema", f"/{where}: {problems[0].message}")
    if not isinstance(parsed, Mapping):
        raise InvalidResult("schema", "a result is an object")
    columns: list[Mapping[str, Any]] = list(parsed["columns"])
    rows: list[list[object]] = list(parsed["rows"])
    entries: list[dict[str, Any]] = list(parsed["cell_states"])
    names = [column["name"] for column in columns]
    if len(set(names)) != len(names):
        raise InvalidResult("duplicate_name", f"repeated column names {names}")
    for column in columns:
        check_descriptor(column, str(parsed["mode"]))
    if parsed["row_count"] != len(rows):
        raise InvalidResult("row_count", f"row_count {parsed['row_count']} for {len(rows)} rows")
    states = [CellState(e["row"], e["column"], e["state"]) for e in entries]
    others_row = parsed.get("others_row")
    check_states(states, columns, len(rows), others_row if isinstance(others_row, int) else None)
    by_cell = {(entry.row, entry.column): entry.state for entry in states}
    _check_cells(rows, columns, by_cell)
    roles = parsed.get("row_roles")
    if roles is not None:
        check_row_roles(
            list(roles),
            columns,
            len(rows),
            str(parsed["mode"]),
            lambda row, column: rows[row][column] is not None or (row, column) in by_cell,
        )
    if arrow is not None:
        _check_arrow(arrow, parsed, columns, rows, entries)
    if receipt is not None:
        reference = parsed.get("receipt")
        if (
            not isinstance(reference, Mapping)
            or reference.get("receipt_id") != receipt.get("receipt_id")
            or reference.get("reproducibility") != receipt.get("reproducibility", {}).get("class")
            or parsed["query_fingerprint"] != receipt.get("query_fingerprint")
        ):
            raise InvalidResult("receipt", "the result does not reference this receipt")
    cells = tuple(
        tuple(
            _cell(cell, column, by_cell.get((row_index, column_index)))
            for column_index, (cell, column) in enumerate(zip(row, columns, strict=True))
        )
        for row_index, row in enumerate(rows)
    )
    return ReadResult(envelope={k: v for k, v in parsed.items() if k != "rows"}, cells=cells)
