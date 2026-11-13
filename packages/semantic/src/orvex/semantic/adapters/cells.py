"""A masked field of a detail page, as a statement writes it (P2-24; #442 for `visible_when`).

- **Masked for every row:** the mask itself, bound; the value is never read. Its column is
  `VARCHAR`, never `NULL`, and says it is masked.
- **Masked by a condition (`visible_when`):** each row shows its value as text where the
  condition is true for that row and that caller, and the mask everywhere else, `NULL`
  included: a condition that is false or unknown masks. The column is plain nullable `String`
  text (`VARCHAR`, exact) for every caller and every row, with no semantic type, unit, currency
  or mask: nothing but the text tells a shown row from a masked one. A value shows as its text
  in the managed lake's own form (`CAST ... AS VARCHAR`, DuckDB), and a `NULL` value where the
  condition holds stays `NULL`.
  A condition whose attribute is absent or does not fit (`Cell.condition` None) masks every row.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from orvex.semantic.result.descriptors import ColumnSpec


def masked(writer: Any, row: Any, spec: ColumnSpec, mask: str) -> tuple[ColumnSpec, str]:  # noqa: ANN401
    """The column and the SQL of the masked field `row` of a detail page."""
    text = writer.cast_to(writer.bind("VARCHAR", mask), "VARCHAR")
    placement = writer.placement
    cell = placement.cells.get(row.member) if placement is not None else None
    if cell is None:
        return replace(spec, encoding="VARCHAR", nullable=False, mask=mask), text
    shown = ColumnSpec(
        spec.name,
        spec.member,
        spec.role,
        "VARCHAR",
        True,
        {"family": "String"},
        None,
        None,
        None,
        "exact",
    )
    if cell.condition is None:  # an attribute it needs is absent: never the value
        return shown, text
    holds = writer.predicate_sql(cell.condition, cell.dataset, row.source)
    value = f"CAST({writer.field(row.member, row.source)} AS VARCHAR)"
    return shown, f"(CASE WHEN COALESCE(({holds}), FALSE) THEN {value} ELSE {text} END)"
