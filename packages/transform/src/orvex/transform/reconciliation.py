"""Reconciliation: compare a model with its source after each run (P1-16).

Each check groups the model's table and its source the same way, computes the same measure on
both, and counts the groups that differ by more than the tolerance (or exist on one side only).
It runs in DuckDB inside the run transaction, after the model's tests. The sidecar's `measure`
and `group_by` are SQL expressions: each must parse as exactly one expression that reads no
other table, and what reaches the query is DuckDB's own rendering of that checked parse tree.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import duckdb
from orvex.contracts.model_config.v1 import ModelError, Reconciliation
from orvex.contracts.store.v1 import Parameter, ParameterType, StatementRequest
from orvex.transform.model_tests import (
    INTEGER_RANGES,
    decimal_bound,
    integer_bound,
    number_text,
)
from orvex.transform.models import Model, problem

_DECIMAL_TYPE = re.compile(r"DECIMAL\((\d+),(\d+)\)")
_REFUSED_CLASSES = frozenset({"SUBQUERY", "WINDOW", "PARAMETER", "STAR"})
_REFUSED_TYPES = frozenset({"BASE_TABLE", "TABLE_FUNCTION", "SUBQUERY", "JOIN"})


@dataclass(frozen=True, slots=True)
class Reconcile:
    """One reconciliation check: `request` counts the failing groups (0 passes)."""

    name: str
    request: StatementRequest


def _walk(node: object) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def expression(text: str) -> str | None:
    """DuckDB's rendering of `text` when it is exactly one expression that reads no table and
    has no subquery, window, parameter or alias; None otherwise."""
    connection = duckdb.connect()
    try:
        connection.execute("SET enable_external_access = false")
        row = connection.execute("SELECT json_serialize_sql($sql)", {"sql": f"SELECT {text}"})
        tree: dict[str, Any] = json.loads(str(row.fetchone()[0]))  # type: ignore[index]
        if tree.get("error") or len(tree.get("statements", [])) != 1:
            return None
        node = tree["statements"][0]["node"]
        wanted = {"type", "cte_map", "select_list", "from_table", "aggregate_handling"}
        filled = {key for key, value in node.items() if value not in (None, [], {}, "")}
        if node.get("type") != "SELECT_NODE" or not filled <= wanted:
            return None
        if node["from_table"].get("type") != "EMPTY" or node["cte_map"].get("map"):
            return None
        (item,) = node["select_list"] if len(node["select_list"]) == 1 else (None,)
        if item is None or item.get("alias"):
            return None
        for part in _walk(item):
            if part.get("class") in _REFUSED_CLASSES or part.get("type") in _REFUSED_TYPES:
                return None
            if part.get("class") == "COLUMN_REF" and len(part.get("column_names", [])) != 1:
                return None  # `t.column` would name another relation
        rendered = connection.execute(
            "SELECT json_deserialize_sql($tree)", {"tree": json.dumps(tree)}
        )
        text_out = str(rendered.fetchone()[0])  # type: ignore[index]
    except duckdb.Error:
        return None
    finally:
        connection.close()
    return text_out.removeprefix("SELECT ")


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _source(text: str) -> list[str]:
    return text.split(".")


def problems(model: Model, connections: Mapping[str, str]) -> list[ModelError]:
    """Rules that need only the sidecar and the project's connections (`name` → type)."""
    file = model.config_file or model.sql_file
    found: list[ModelError] = []

    def invalid(pointer: str, message: str, code: str = "MODEL_CONFIG_INVALID") -> None:
        found.append(
            problem(code, f"{file}: {pointer}: {message}", file, model=model.name, path=pointer)
        )

    names: set[str] = set()
    for index, check in enumerate(model.config.reconciliation or []):
        base = f"/reconciliation/{index}"
        if check.name in names:
            invalid(f"{base}/name", f"{check.name} is used twice in this model.")
        names.add(check.name)
        if expression(check.measure) is None:
            invalid(
                f"{base}/measure",
                "write one expression (for example sum(kwh)) without subqueries, windows, "
                "parameters or other tables.",
            )
        for position, item in enumerate(check.group_by or []):
            if expression(item.root) is None:
                invalid(f"{base}/group_by/{position}", "write one expression per item.")
        parts = _source(check.source)
        if len(parts) not in {2, 3} or connections.get(parts[0]) in {None, "clickhouse"}:
            invalid(
                f"{base}/source",
                f"{check.source} is not a table of a connection (<connection>.<table>).",
                "MODEL_REFERENCE_UNKNOWN",
            )
        text = tolerance_text(model, index, check)
        if text is None or text.startswith("-"):
            invalid(
                f"{base}/tolerance", "write a non-negative decimal number of at most 38 digits."
            )
    return found


def tolerance_text(model: Model, index: int, check: Reconciliation) -> str | None:
    value = check.tolerance if check.tolerance is not None else 0
    return number_text(model, f"/reconciliation/{index}/tolerance", value)


def _sides(check: Reconciliation, relation: str) -> tuple[str, str]:
    groups = [expression(item.root) for item in check.group_by or []]
    measure = expression(check.measure)
    assert measure is not None  # checked at validation
    assert None not in groups
    selected = [f"{group} AS {_identifier(f'g{i}')}" for i, group in enumerate(groups)]
    columns = ", ".join([*selected, f"{measure} AS m", "true AS present"])
    # Grouped by position: a measure that is not an aggregate, or an aggregate among the groups,
    # is refused by the binder instead of silently becoming a grouping key.
    positions = ", ".join(str(i + 1) for i in range(len(groups)))
    grouping = f" GROUP BY {positions}" if groups else " GROUP BY ()"
    source = ".".join(_identifier(part) for part in _source(check.source))
    return (
        f"SELECT {columns} FROM {relation} AS t{grouping}",
        f"SELECT {columns} FROM {source} AS t{grouping}",
    )


def _joined(check: Reconciliation, relation: str) -> tuple[str, str]:
    """(the CTEs and join of both sides, the difference expression)."""
    model_side, source_side = _sides(check, relation)
    matched = " AND ".join(
        f"model.{_identifier(f'g{i}')} IS NOT DISTINCT FROM source.{_identifier(f'g{i}')}"
        for i in range(len(check.group_by or []))
    )
    return (
        f"WITH model AS ({model_side}), source AS ({source_side}) SELECT {{select}} "
        f"FROM model FULL OUTER JOIN source ON {matched or 'TRUE'}",
        # Larger minus smaller: never below zero, so unsigned measures cannot overflow.
        "greatest(model.m, source.m) - least(model.m, source.m)",
    )


def difference_sql(check: Reconciliation, relation: str) -> str:
    """A query whose columns have the types of the model's measure, the source's measure and
    their difference (for binding)."""
    template, difference = _joined(check, relation)
    columns = f"model.m AS model, source.m AS source, {difference} AS difference"
    return template.replace("{select}", columns, 1)


def exact_difference(kind: str) -> bool:
    return kind in INTEGER_RANGES or _DECIMAL_TYPE.fullmatch(kind) is not None


def steps(model: Model, relation: str, differences: Sequence[str]) -> list[Reconcile]:
    """The reconciliation checks of a model whose rows are `relation`; `differences` holds the
    type of each check's measure difference (from the binder)."""
    found: list[Reconcile] = []
    for index, (check, kind) in enumerate(
        zip(model.config.reconciliation or [], differences, strict=True)
    ):
        template, difference = _joined(check, relation)
        text = tolerance_text(model, index, check)
        assert text is not None  # checked at validation
        convert = integer_bound if kind in INTEGER_RANGES else decimal_bound
        tolerance, everything = convert(text, "max", kind)
        parameters: list[Parameter] = []
        if everything:
            beyond = "TRUE"  # cannot happen for a non-negative tolerance; kept for symmetry
        elif tolerance is None:
            beyond = "FALSE"  # a tolerance beyond the type's range: no difference exceeds it
        else:
            parameters.append(
                Parameter(name="p_tolerance", type=ParameterType(kind), value=tolerance)
            )
            beyond = f"{difference} > $p_tolerance"
        failing = (
            "model.present IS NULL OR source.present IS NULL "
            "OR (model.m IS NULL) <> (source.m IS NULL) "
            f"OR {beyond}"
        )
        sql = template.replace("{select}", "count(*)", 1) + f" WHERE {failing}"
        found.append(
            Reconcile(
                check.name, StatementRequest(kind="statement", sql=sql, parameters=parameters)
            )
        )
    return found
