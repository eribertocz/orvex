"""Synthetic semantic results for the P2-06 tests: Arrow tables built in memory, no real data."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pyarrow as pa
from orvex.semantic.result import CellState, ColumnSpec, SemanticResult, build_result

QUERY = "sha256:" + "a" * 64
MODEL = "sha256:" + "b" * 64


def spec(name: str, encoding: str, logical: dict[str, Any] | None, **extra: Any) -> ColumnSpec:  # noqa: ANN401
    member = extra.pop("member", {"dataset": "sales", "name": name})
    role = extra.pop("role", "field")
    nullable = extra.pop("nullable", True)
    if "exactness" not in extra and logical is not None:
        extra["exactness"] = "approximate" if logical["family"] == "Float" else "exact"
    return ColumnSpec(
        name=name,
        member=member,
        role=role,
        encoding=encoding,
        nullable=nullable,
        logical=logical,
        **extra,
    )


def decimal(name: str, precision: int, scale: int, **extra: Any) -> ColumnSpec:  # noqa: ANN401
    logical = {"family": "Decimal", "precision": precision, "scale": scale}
    return spec(name, f"DECIMAL({precision},{scale})", logical, **extra)


def text(name: str, **extra: Any) -> ColumnSpec:  # noqa: ANN401
    return spec(name, "VARCHAR", {"family": "String"}, **extra)


def integer(name: str, encoding: str, width: int, signed: bool, **extra: Any) -> ColumnSpec:  # noqa: ANN401
    return spec(name, encoding, {"family": "Integer", "width": width, "signed": signed}, **extra)


def masked(name: str, mask: str = "***", **extra: Any) -> ColumnSpec:  # noqa: ANN401
    return spec(name, "VARCHAR", {"family": "String"}, mask=mask, nullable=False, **extra)


def table(columns: dict[str, pa.Array]) -> pa.Table:
    return pa.table(columns)


def detail(
    data: pa.Table,
    columns: Sequence[ColumnSpec],
    *,
    page: dict[str, Any] | None = None,
    **extra: Any,  # noqa: ANN401
) -> SemanticResult:
    return build_result(
        data,
        columns,
        mode="detail",
        query_fingerprint=QUERY,
        model_fingerprint=MODEL,
        page=page or {"size": 100},
        **extra,
    )


def aggregate(
    data: pa.Table,
    columns: Sequence[ColumnSpec],
    *,
    states: Sequence[CellState] = (),
    **extra: Any,  # noqa: ANN401
) -> SemanticResult:
    return build_result(
        data,
        columns,
        mode="aggregate",
        query_fingerprint=QUERY,
        model_fingerprint=MODEL,
        page=extra.pop("page", {"truncated": False}),
        cell_states=states,
        **extra,
    )


def top_n_with_others() -> SemanticResult:
    """`top_n` with `others: true`: two regions and the recomputed "Others" row, plus an S9
    comparison with no value at that row."""
    data = table(
        {
            "region": pa.array(["north", "south", None]),
            "revenue": pa.array([1250, 990, 310], pa.decimal128(18, 2)),
            "growth": pa.array([0.5, -0.25, None], pa.float64()),
        }
    )
    columns = [
        text("region", role="dimension"),
        decimal("revenue", 18, 2, role="metric", member={"name": "revenue"}),
        spec("growth", "DOUBLE", {"family": "Float"}, role="metric", member={"name": "growth"}),
    ]
    states = [CellState(2, 0, "others"), CellState(2, 2, "undefined_at_total")]
    return aggregate(data, columns, states=states, others_row=2)
