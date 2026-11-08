"""The ClickHouse adapter (P2-31, #225; ADR-0027 §8, ADR-0028 §3): a `SemanticPlan` as one
ClickHouse `SELECT` with bound parameters, for the store's ClickHouse read boundary
(`contracts/store/v1`, "ClickHouse Direct Query"). It writes the statement the DuckDB adapter
writes, with the same shape, and encodes every difference of meaning explicitly; it never
reads data and never transpiles.

- **Identifiers.** ClickHouse matches names with their case: each source column is written
  with its exact name in the engine's catalog, never the model's normalized one.
- **Values.** Every value is a typed placeholder `{pN:Type}` (`Nullable(Type)` for a `NULL`).
  A model's decimal constant is a `Decimal`, never a float (ClickHouse reads `0.5` as one).
- **Exact arithmetic.** ClickHouse keeps a decimal product at the narrower operand's precision
  and detects overflow only at its native integer's width, and its integer arithmetic wraps.
  So every exact operand of `+`, `-` and `*` is widened to `Decimal(38, s)` first; a sum of
  integers is a sum of `Int128`. `/` between exact values is never written: a ratio is C2's
  exact division, with `intDiv` and the remainder.
- **`NULL`.** A `sum` over no row or only `NULL`s is `NULL`, written as such (ClickHouse gives
  `0` for no row). `IN` and `NOT IN` of an expression are written with SQL's three-valued
  meaning: as `=` comparisons for a list, and, for a bound set, `NULL` membership explicit
  (ClickHouse's `2 NOT IN (1, NULL)` is true). A filter's values are never `NULL`, and its
  `NULL` rows are written apart, so its `IN` keeps the reference's form.
- **Dates.** A date bucket is computed in `Date32` (`toStartOf*` answers a 16-bit `Date`).
- **The result.** Each column is converted last to its exact store form: `HUGEINT`,
  `UHUGEINT` and `UUID` as text, `TIMESTAMPTZ` with microseconds in UTC, every other type by
  an explicit `CAST`; the order is that of the unconverted values.
- **Functions.** Only `FUNCTIONS` are written; `check_statement` refuses any other, any table
  function, and any `SETTINGS`, `FORMAT` or `;`.
- **Capabilities.** `CAPABILITIES`; a plan needing any other is refused before anything is
  written (`UNSUPPORTED_OPERATION`, naming it), as P2-29 defines. `LOWER` and `UPPER` are
  refused: ClickHouse's Unicode case mapping is not DuckDB's (`ß`, a final sigma).

The engine's profile is verified at connection by the store (`join_use_nulls = 1`,
`transform_null_in = 0`, `cast_keep_nullable = 1`, and the rest of its table); this adapter
relies on those values and writes no `SETTINGS`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from orvex.semantic.adapters.duckdb import (
    LEVEL,
    PhysicalQuery,
    PolicyValues,
    _rounded,
    _unsupported,
    _Writer,
    number_parameter,
    quote,
)
from orvex.semantic.compiler.catalog import column_key
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.compiler.typecheck import NodeType, TypedNode
from orvex.semantic.engines import required
from orvex.semantic.errors import PlanError, SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.planning import ISSUED_PLAN, SemanticPlan
from orvex.semantic.result import ColumnSpec

NAME = "clickhouse"
VERSION = "1"
# What this adapter runs (`engines.CAPABILITIES`); the rest lands with #225 PR 3.
CAPABILITIES = frozenset({"count_distinct", "exact_decimal"})

_TYPES = {
    "BOOLEAN": "Bool",
    "TINYINT": "Int8",
    "SMALLINT": "Int16",
    "INTEGER": "Int32",
    "BIGINT": "Int64",
    "HUGEINT": "Int128",
    "UTINYINT": "UInt8",
    "USMALLINT": "UInt16",
    "UINTEGER": "UInt32",
    "UBIGINT": "UInt64",
    "UHUGEINT": "UInt128",
    "FLOAT": "Float32",
    "DOUBLE": "Float64",
    "VARCHAR": "String",
    "UUID": "UUID",
    "DATE": "Date32",
    "TIMESTAMPTZ": "DateTime64(6, 'UTC')",
}
_DECIMAL = re.compile(r"DECIMAL\((\d+),(\d+)\)")
_EXACT = frozenset({"Integer", "Decimal"})
_DATE_BUCKETS = {
    "day": "{d}",
    "week": "({d} - (toDayOfWeek({d}) - 1))",  # Monday weeks
    "month": "makeDate32(toYear({d}), toMonth({d}), 1)",
    "quarter": "makeDate32(toYear({d}), intDiv(toMonth({d}) - 1, 3) * 3 + 1, 1)",
    "year": "makeDate32(toYear({d}), 1, 1)",
}

# Every function a statement may call; a name before `(` that is neither one of these nor a
# keyword is refused, and so is every table function.
FUNCTIONS = frozenset(
    {
        "CAST",
        "abs",
        "coalesce",
        "count",
        "countIf",
        "empty",
        "has",
        "if",
        "intDiv",
        "JSONExtract",
        "makeDate32",
        "multiIf",
        "nullIf",
        "row_number",
        "sum",
        "sumIf",
        "toDateTime64",
        "toDecimal128",
        "toDayOfWeek",
        "toMonth",
        "toString",
        "toYear",
        "uniqExact",
        "uniqExactIf",
    }
)
_KEYWORDS = frozenset(
    {
        "AND",
        "AS",
        "BETWEEN",
        "BY",
        "ELSE",
        "END",
        "EXISTS",
        "FROM",
        "IN",
        "IS",
        "JOIN",
        "NOT",
        "NULL",
        "ON",
        "OR",
        "OVER",
        "QUALIFY",
        "SELECT",
        "THEN",
        "WHEN",
        "WHERE",
        "CASE",
        "PARTITION",
        "DESC",
        "ASC",
        "LAST",
        "FIRST",
        "NULLS",
    }
)
_TYPE_NAMES = frozenset({"Array", "DateTime64", "Decimal", "DECIMAL", "Nullable"})  # in a CAST
_TOKENS = re.compile(
    r"""(?P<string>'(?:[^'\\]|\\.|'')*')"""
    r"""|(?P<name>"(?:[^"\\]|\\.|"")*")"""
    r"""|(?P<parameter>\{[A-Za-z_][A-Za-z0-9_]*:[^{}]*\})"""
    r"""|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"""
    r"""|(?P<other>\S)""",
    re.S,
)


def engine_type(kind: str) -> str:
    """The ClickHouse type of a store type (`contracts/store/v1`, "ClickHouse Direct Query")."""
    found = _DECIMAL.fullmatch(kind)
    if found is not None:
        return f"Decimal({found.group(1)}, {found.group(2)})"
    if kind not in _TYPES:
        raise _unsupported()
    return _TYPES[kind]


def quote_name(name: str) -> str:
    """A name from the engine's catalog: ClickHouse reads a backslash in a quoted name as an
    escape, so it is escaped too."""
    return '"' + name.replace("\\", "\\\\").replace('"', '""') + '"'


def _scale(node: NodeType | None) -> int | None:
    """The scale an exact value keeps, or None when it is not exact."""
    if node is None or node.logical.datatype not in _EXACT:
        return None
    return int(node.logical.scale or 0) if node.logical.datatype == "Decimal" else 0


class _ClickHouse(_Writer):
    def __init__(
        self,
        plan: SemanticPlan,
        columns: Mapping[str, Iterable[str]],
        values: Mapping[int, PolicyValues],
    ) -> None:
        super().__init__(plan, values)
        # Each table's columns, by DuckDB's identity of a name (the compiler's), exact.
        self.names: dict[str, dict[str, str]] = {}
        for source, names in columns.items():
            exact: dict[str, str] = {}
            for name in names:
                if column_key(name) in exact:
                    raise PlanError("two columns of one ClickHouse table differ only in case")
                exact[column_key(name)] = name
            self.names[source] = exact
        # The type of every expression node of the model, by identity (the compiled trees).
        self.types: dict[int, NodeType] = {}
        for member in plan.compiled.members.values():
            trees = [member.typed.tree, *(v for _, v in member.typed.payload)]
            for tree in trees:
                if isinstance(tree, TypedNode):
                    self.index(tree)

    def index(self, tree: TypedNode) -> None:
        self.types[id(tree.node)] = tree.type
        for child in tree.children:
            self.index(child)

    # hooks

    def bind(self, kind: str, value: str | None) -> str:
        name = f"p{len(self.parameters)}"
        written = engine_type(kind)
        self.parameters.append({"name": name, "type": kind, "value": value})
        return "{" + name + ":" + (f"Nullable({written})" if value is None else written) + "}"

    def column_ref(self, alias: str, dataset: str, name: str) -> str:
        source = self.tables.get(dataset)
        exact = self.names.get(source, {}).get(column_key(name)) if source else None
        if exact is None:
            raise PlanError("a column the ClickHouse catalog does not hold")
        return f"{quote(alias)}.{quote_name(exact)}"

    def table_sql(self, source: str) -> str:
        return ".".join(quote_name(part) for part in source.split("."))

    def cast_to(self, expression: str, kind: str) -> str:
        found = _DECIMAL.fullmatch(kind)
        if found is not None:  # ClickHouse casts no `Int128` to a `Decimal`; this converts all
            return f"toDecimal128({expression}, {int(found.group(2))})"
        return f"CAST({expression} AS {engine_type(kind)})"

    def sum_of(self, expression: str, member: MemberId) -> str:
        integer = self.compiled.require(member).typed.type.logical.datatype == "Integer"
        return self.total(f"CAST({expression} AS Int128)" if integer else expression, None)

    def total(self, value: str, where: str | None) -> str:
        """`sum`, `NULL` over no row or only `NULL`s: ClickHouse's own gives `0` for no row."""
        if where is None:
            return f"if(count({value}) = 0, NULL, sum({value}))"
        return f"if(countIf({value}, {where}) = 0, NULL, sumIf({value}, {where}))"

    def latest_first(self, axis: str) -> str:
        return f"{axis} DESC NULLS LAST"

    def list_text(self, values: Any) -> str:  # noqa: ANN401
        return json.dumps([None if v is None else str(v) for v in values])

    def bound_list(self, listed: str, kind: str) -> str:
        return (
            f"CAST(JSONExtract({listed}, 'Array(Nullable(String))') AS "
            f"Array(Nullable({engine_type(kind)})))"
        )

    def membership(self, field: str, values: str, negated: bool) -> str:
        """`field IN values` with SQL's meaning: false for an empty set, `NULL` for a `NULL`
        field, or for a set holding `NULL` without the field."""
        test = (
            f"multiIf(empty({values}), false, {field} IS NULL, NULL, has({values}, {field}), "
            f"true, has({values}, NULL), NULL, false)"
        )
        return f"(NOT {test})" if negated else f"({test})"

    def power(self, exponent: int) -> str:
        return f"CAST('1{'0' * exponent}' AS Int128)"

    def unscaled(self, value: str, scale: int) -> str:
        if scale == 0:
            return f"CAST({value} AS Int128)"
        wide = f"CAST({value} AS Decimal(38, {scale}))"
        whole = f"CAST({wide} AS Int128)"  # toward zero, as `trunc`
        return (
            f"({whole} * {self.power(scale)}"
            f" + CAST(({wide} - {whole}) * {self.power(scale)} AS Int128))"
        )

    def rounded(self, a: str, b: str, mode: str) -> str:
        return _rounded(a, b, mode, f"intDiv({a}, {b})")

    def shifted(self, value: str, scale: int) -> str:
        if not scale:
            return value
        return f"{value} * CAST('0.{'0' * (scale - 1)}1' AS Decimal(38, {scale}))"

    def ordered(
        self,
        text: str,
        columns: list[ColumnSpec],
        checks: list[tuple[str, str]],
        order: Any,  # noqa: ANN401
        limit: int | None,
    ) -> str:
        """Every column in its exact store form, over the rows ordered by their own values."""
        names = [(c.name, c.encoding) for c in columns] + [(n, "BOOLEAN") for n, _ in checks]
        names += [(n, "BIGINT") for n, _ in [*self.weights, *self.as_of]]
        names += [(LEVEL, "INTEGER")] if self.plan.totals is not None else []
        row = quote("#r")
        for name, _ in names:
            if "\\" in name:
                raise _unsupported()
        converted = ", ".join(
            f"{_converted(f'{row}.{quote(name)}', kind)} AS {quote(name)}" for name, kind in names
        )
        text = f"SELECT {converted} FROM ({text}) AS {row}"
        if order:
            text += " ORDER BY " + ", ".join(
                f"{row}.{quote(s.name)} {'DESC' if s.descending else 'ASC'} "
                f"NULLS {'FIRST' if s.nulls_first else 'LAST'}"
                for s in order
            )
        if limit is not None:
            text += f" LIMIT {int(limit) + 1}"
        return text

    # expressions

    def node(self, node: sql.Node, alias: str, dataset: str, context: str) -> str:
        def again(child: sql.Node) -> str:
            return self.node(child, alias, dataset, context)

        if isinstance(node, sql.Number):
            if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", node.text):
                raise _unsupported()
            if "." not in node.text:
                return node.text
            return f"CAST('{node.text}' AS {engine_type(number_parameter(node.text))})"
        if isinstance(node, sql.Unary) and node.op in ("-", "+"):
            return f"({node.op}{self.exact(node.operand, again)})"
        if isinstance(node, sql.Binary) and node.op in ("+", "-", "*"):
            left, right = self.types.get(id(node.left)), self.types.get(id(node.right))
            if _scale(left) is not None and _scale(right) is not None:
                written = (self.exact(node.left, again), self.exact(node.right, again))
            elif left is not None and right is not None:
                written = (again(node.left), again(node.right))  # a float's own arithmetic
            else:
                raise _unsupported()
            return f"({written[0]} {node.op} {written[1]})"
        if isinstance(node, sql.Binary) and node.op == "/":
            sides = (self.types.get(id(node.left)), self.types.get(id(node.right)))
            if not all(s is not None and s.logical.datatype == "Float" for s in sides):
                raise _unsupported()  # an exact quotient is C2's division, never `/`
            return f"({again(node.left)} / {again(node.right)})"
        if isinstance(node, sql.InList):
            operand = again(node.operand)
            equal = " OR ".join(f"{operand} = {again(i)}" for i in node.items) or "false"
            return f"(NOT ({equal}))" if node.negated else f"({equal})"
        return super().node(node, alias, dataset, context)

    def cast(self, node: sql.Cast, again: Any, dataset: str, context: str) -> str:  # noqa: ANN401
        super().cast(node, again, dataset, context)  # the one cast with an exact meaning
        return self.cast_to(
            again(node.operand), f"DECIMAL({int(node.arguments[0])},{int(node.arguments[1])})"
        )

    def exact(self, node: sql.Node, again: Callable[[sql.Node], str]) -> str:
        """An exact value widened to `Decimal(38, s)`, so no step narrows or wraps it."""
        scale = _scale(self.types.get(id(node)))
        if scale is None:
            return again(node)
        return f"toDecimal128({again(node)}, {scale})"  # an `Int128` too, which CAST refuses

    def call(self, node: sql.Call, again: Any, context: str, dataset: str) -> str:  # noqa: ANN401
        name = node.name.upper()
        distinct = node.distinct and name == "COUNT" and context == "metric"
        if node.order is not None or (node.distinct and not distinct):
            raise _unsupported()
        if name == "DATE_TRUNC":
            return super().call(node, again, context, dataset)
        if name in ("SUM", "COUNT") and context == "metric":
            arguments = node.arguments
            where = again(node.filter) if node.filter is not None else None
            if name == "COUNT" and len(arguments) == 1 and isinstance(arguments[0], sql.Star):
                return "count()" if where is None else f"countIf({where})"
            if len(arguments) != 1:
                raise _unsupported()
            value = again(arguments[0])
            if name == "SUM":
                kind = self.types.get(id(arguments[0]))
                if kind is None:
                    raise _unsupported()
                if kind.logical.datatype == "Integer":
                    value = f"CAST({value} AS Int128)"
                return self.total(value, where)
            function = "uniqExact" if distinct else "count"
            if where is None:
                return f"{function}({value})"
            return f"{function}If({value}, {where})"
        if name in ("COALESCE", "NULLIF", "ABS") and node.filter is None:
            function = {"COALESCE": "coalesce", "NULLIF": "nullIf", "ABS": "abs"}[name]
            return f"{function}({', '.join(again(a) for a in node.arguments)})"
        raise _unsupported()  # LOWER and UPPER included: ClickHouse's case mapping differs

    def cut(self, unit: str, value: str, zone: str | None) -> str:
        if zone is not None:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "time_zone"})
        if unit not in _DATE_BUCKETS:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "bucket"})
        return _DATE_BUCKETS[unit].format(d=value)


def _converted(column: str, kind: str) -> str:
    """A result column in the exact form the store reads it in (`contracts/store/v1`)."""
    if kind in ("HUGEINT", "UHUGEINT"):
        return f"toString(CAST({column} AS {engine_type(kind)}))"
    if kind == "UUID":
        return f"toString({column})"
    if kind == "TIMESTAMPTZ":
        return f"toDateTime64({column}, 6, 'UTC')"
    found = _DECIMAL.fullmatch(kind)
    if found is not None:  # the store narrows it to its precision, refusing what overflows
        return f"CAST({column} AS Decimal(38, {int(found.group(2))}))"
    return f"CAST({column} AS {engine_type(kind)})"


def compile_plan(
    plan: SemanticPlan,
    columns: Mapping[str, Iterable[str]],
    values: Mapping[int, PolicyValues],
) -> PhysicalQuery:
    """The ClickHouse query of a plan the planner made. `columns`: each table's column names as
    the engine's catalog holds them; `values`: each mapping subquery's values, resolved on the
    managed lake (`mapping_queries`). A plan needing a capability this adapter lacks is refused
    with `UNSUPPORTED_OPERATION`, naming it, before anything is written."""
    if plan.issued is not ISSUED_PLAN:
        raise PlanError("not a plan of the planner")
    missing = sorted(required(plan) - CAPABILITIES)
    if missing:
        raise SemanticError("UNSUPPORTED_OPERATION", {"operation": missing[0]})
    query = _ClickHouse(plan, columns, values).statement()
    check_statement(query.sql, {table for table, _ in query.reads})
    return PhysicalQuery(
        query.sql,
        query.parameters,
        query.columns,
        query.plan_fingerprint,
        adapter=NAME,
        adapter_version=VERSION,
        checks=query.checks,
        zones=query.zones,
        weights=query.weights,
        as_of=query.as_of,
        hidden=query.hidden,
        sorts=query.sorts,
        level=query.level,
        reads=query.reads,
    )


def check_statement(statement: str, tables: Iterable[str]) -> None:
    """A statement this adapter may send: one `SELECT`, no `SETTINGS`, `FORMAT` or `;`, every
    function in `FUNCTIONS`, and every table read one of `tables` or a subquery. Anything else
    is an internal error, never sent."""
    allowed = {tuple(_name(part) for part in table.split(".")) for table in tables}
    tokens = [(m.lastgroup, m.group()) for m in _TOKENS.finditer(statement)]
    if not tokens or tokens[0] != ("word", "SELECT"):
        raise PlanError("a ClickHouse statement is one SELECT")
    for at, (kind, text) in enumerate(tokens):
        following = tokens[at + 1][1] if at + 1 < len(tokens) else ""
        if kind == "other" and text == ";":
            raise PlanError("a ClickHouse statement is one statement")
        if kind == "word" and text.upper() in ("SETTINGS", "FORMAT", "INTO"):
            raise PlanError(f"a ClickHouse statement never has {text.upper()}")
        called = kind == "word" and following == "("
        if called and text not in FUNCTIONS | _TYPE_NAMES and text.upper() not in _KEYWORDS:
            raise PlanError(f"a function outside the allow-list: {text}")
        if (kind == "word" and text.upper() in ("FROM", "JOIN")) or (kind, text) == ("other", ","):
            if kind == "other" and not _in_from(tokens, at):
                continue
            target = _table_at(tokens, at + 1)
            if target is not None and target not in allowed:
                raise PlanError("a ClickHouse statement reads a table the model does not name")


def _name(written: str) -> str:
    if written.startswith('"'):
        return written[1:-1].replace('""', '"').replace("\\\\", "\\")
    return written


def _table_at(tokens: Sequence[tuple[str | None, str]], at: int) -> tuple[str, ...] | None:
    """The table named at `at` (a subquery: None); a word there is a table function's or a
    table's bare name, both refused."""
    if at >= len(tokens) or tokens[at][1] == "(":
        return None
    parts: list[str] = []
    while at < len(tokens) and tokens[at][0] in ("name", "word"):
        if tokens[at][0] == "word":
            raise PlanError("a ClickHouse statement reads a table by a bare name")
        parts.append(_name(tokens[at][1]))
        if at + 1 < len(tokens) and tokens[at + 1][1] == ".":
            at += 2
            continue
        break
    return tuple(parts)


def _in_from(tokens: Sequence[tuple[str | None, str]], at: int) -> bool:
    """Whether the comma at `at` separates tables of a `FROM` list at its own depth."""
    depth = 0
    for kind, text in reversed(tokens[:at]):
        if text == ")":
            depth += 1
        elif text == "(":
            if depth == 0:
                return False
            depth -= 1
        elif depth == 0 and kind == "word":
            upper = text.upper()
            if upper == "FROM":
                return True
            if upper in ("SELECT", "WHERE", "ON", "BY", "JOIN", "QUALIFY"):
                return False
    return False


# Cost (law 7; ADR-0027 §7): ClickHouse's own estimate of the rows a statement reads.


def estimate_statement(query: PhysicalQuery) -> str:
    """`EXPLAIN ESTIMATE` of the statement, with the same parameters: one row per MergeTree-
    family table it reads (`database`, `table`, `parts`, `rows`, `marks`); a table of any other
    engine has no row."""
    return f"EXPLAIN ESTIMATE {query.sql}"


def rows_scanned(
    query: PhysicalQuery, rows: Sequence[Mapping[str, object]], database: str
) -> int | None:
    """The rows the statement reads, from its `EXPLAIN ESTIMATE` rows: each table read counts
    its estimated rows once per read. A table with no estimate (not MergeTree) means there are
    no statistics, so no estimate at all (`semantic-engine.md` §8)."""
    found: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (str(row["database"]), str(row["table"]))
        found[key] = found.get(key, 0) + int(str(row["rows"]))
    total = 0
    for source, _ in query.reads:
        parts = source.split(".")
        key = (parts[0], parts[1]) if len(parts) == 2 else (database, parts[0])
        if key not in found:
            return None
        total += found[key]
    return total


def refuse_over_budget(estimate: int | None, limit: int, restricted: bool) -> None:
    """`QUERY_BUDGET_EXCEEDED` (`rows_scanned`) before the read when the estimate is above the
    budget. The estimate is named only when no row policy restricts a dataset read, since it
    counts the rows a policy hides (`semantic-error/v1`). No estimate decides nothing."""
    if estimate is None or estimate <= limit:
        return
    details = {"budget": "rows_scanned", "limit": str(limit)}
    if not restricted:
        details["estimate"] = str(estimate)
    raise SemanticError("QUERY_BUDGET_EXCEEDED", details)
