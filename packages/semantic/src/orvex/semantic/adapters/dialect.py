"""The SQL each dialect spells its own way, as DuckDB spells it (P2-31, #225): the hooks the
DuckDB adapter's writer inherits and another dialect's adapter overrides, never the shape of the
statement, with the quoting and the exact division they write."""

from __future__ import annotations

import json
from typing import Any

from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.errors import SemanticError
from orvex.semantic.result import ColumnSpec


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table(source: str) -> str:
    """A dataset's source table as SQL: `table` (schema `main`) or `schema.table`, each part an
    identifier of its own (#359), as the store resolves it and the compiler admits it."""
    return ".".join(quote(part) for part in source.split("."))


def _unsupported() -> SemanticError:
    return SemanticError("UNSUPPORTED_OPERATION", {"operation": "expression"})


def _power(exponent: int) -> str:
    """10^exponent as an exact `HUGEINT`; past its range, the cast fails, never a float."""
    return f"CAST('1{'0' * exponent}' AS HUGEINT)"


def _unscaled(value: str, scale: int) -> str:
    """The exact integer behind a `DECIMAL(p, scale)`, as a `HUGEINT`: its integer part and its
    fraction are scaled apart, so a 38-digit value never overflows a `DECIMAL(38)` on the way."""
    if scale == 0:
        return f"CAST({value} AS HUGEINT)"
    return (
        f"(CAST(trunc({value}) AS HUGEINT) * {_power(scale)}"
        f" + CAST(({value} - trunc({value})) * {_power(scale)} AS HUGEINT))"
    )


def _rounded(a: str, b: str, mode: str, quotient: str | None = None) -> str:
    """The quotient a / b rounded to an integer in `mode`, from truncating integer division
    (`quotient`, the dialect's) and its remainder; written so that no step can exceed the
    operands' range."""
    quotient, rest = quotient or f"({a} // {b})", f"({a} % {b})"
    away = f"(CASE WHEN ({a} < 0) <> ({b} < 0) THEN -1 ELSE 1 END)"
    half = f"abs({rest}) >= abs({b}) - abs({rest})"
    above = f"abs({rest}) > abs({b}) - abs({rest})"
    tie = f"abs({rest}) = abs({b}) - abs({rest})"
    if mode == "down":
        step = "0"
    elif mode == "up":
        step = f"CASE WHEN {rest} <> 0 THEN {away} ELSE 0 END"
    elif mode == "half_away_from_zero":
        step = f"CASE WHEN {half} THEN {away} ELSE 0 END"
    elif mode == "half_even":
        step = f"CASE WHEN {above} OR ({tie} AND {quotient} % 2 <> 0) THEN {away} ELSE 0 END"
    else:
        raise _unsupported()
    return f"({quotient} + {step})"


class DuckDBDialect:
    """Each hook writes what DuckDB writes."""

    def column_ref(self, alias: str, dataset: str, name: str) -> str:
        """A source column of `dataset`, read from the row `alias`."""
        return f"{quote(alias)}.{quote(name)}"

    def table_sql(self, source: str) -> str:
        return table(source)

    def cast_to(self, expression: str, kind: str) -> str:
        """`expression` as the store type `kind` (`contracts/store/v1`)."""
        return f"CAST({expression} AS {kind})"

    def sum_of(self, expression: str, member: MemberId) -> str:
        """The sum of a stored column `member` (C8)."""
        return f"SUM({expression})"

    def latest_first(self, axis: str) -> str:
        return f"{axis} DESC"

    def list_text(self, values: Any) -> str:  # noqa: ANN401
        return json.dumps(list(values))

    def bound_list(self, listed: str, kind: str) -> str:
        """The values of the bound JSON list `listed`, each of the type `kind`."""
        return f"(SELECT unnest(CAST(CAST({listed} AS JSON) AS {kind}[])))"

    def membership(self, field: str, values: str, negated: bool) -> str:
        return f"({field} {'NOT ' if negated else ''}IN {values})"

    def power(self, exponent: int) -> str:
        return _power(exponent)

    def unscaled(self, value: str, scale: int) -> str:
        return _unscaled(value, scale)

    def scaled(self, value: str, exponent: int) -> str:
        """`value`, an exact integer, times 10^exponent; an overflow is an error."""
        return f"{value} * {self.power(exponent)}"

    def rounded(self, a: str, b: str, mode: str) -> str:
        return _rounded(a, b, mode)

    def shifted(self, value: str, scale: int) -> str:
        """An integer-valued `DECIMAL(38,0)` moved `scale` digits to the right of the point."""
        return f"{value} * 0.{'0' * (scale - 1)}1" if scale else value

    def ordered(
        self,
        text: str,
        columns: list[ColumnSpec],
        checks: list[tuple[str, str]],
        order: Any,  # noqa: ANN401
        limit: int | None,
    ) -> str:
        """The statement's rows in `order` (none: as they come), cut to `limit` and one more."""
        if order:
            text += " ORDER BY " + ", ".join(
                f"{quote(s.name)} {'DESC' if s.descending else 'ASC'} "
                f"NULLS {'FIRST' if s.nulls_first else 'LAST'}"
                for s in order
            )
        if limit is not None:
            text += f" LIMIT {int(limit) + 1}"  # one more row says the page is truncated
        return text
