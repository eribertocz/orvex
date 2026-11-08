"""The statements the ClickHouse adapter may send: one `SELECT`, no `SETTINGS`, `FORMAT`,
`INTO` or `;`, every function in `FUNCTIONS`, and every table read one of the plan's tables
or a subquery (never a table function or a bare name). Anything else is an internal error,
never sent."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from orvex.semantic.adapters.clickhouse.names import unquote_name
from orvex.semantic.errors import PlanError

# Every function a statement may call; a name before `(` that is neither one of these nor a
# keyword is refused, and so is every table function.
FUNCTIONS = frozenset(
    {
        "CAST",
        "COALESCE",
        "GROUPING",
        "MAX",
        "abs",
        "arrayJoin",
        "arrayMap",
        "arrayZip",
        "coalesce",
        "count",
        "countIf",
        "empty",
        "fromUnixTimestamp64Micro",
        "has",
        "if",
        "intDiv",
        "isNaN",
        "JSONExtract",
        "makeDate32",
        "multiIf",
        "nullIf",
        "parseDateTime64BestEffort",
        "positiveModulo",
        "row_number",
        "splitByChar",
        "sum",
        "sumIf",
        "throwIf",
        "timeZoneOffset",
        "toDate32",
        "toDateTime64",
        "toDecimal256",
        "toDayOfWeek",
        "toInt256",
        "toInt64",
        "toMonth",
        "toString",
        "toTimeZone",
        "toUnixTimestamp64Micro",
        "toYear",
        "tupleElement",
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
        "SETS",
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


def check_statement(statement: str, tables: Iterable[str]) -> None:
    """Refuse, with a `PlanError`, a statement outside the allow-lists; `tables` are the
    tables the plan reads."""
    allowed = {tuple(unquote_name(part) for part in table.split(".")) for table in tables}
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
        reads = kind == "word" and text.upper() in ("FROM", "JOIN")
        if reads and at and tokens[at - 1][1].upper() == "DISTINCT":  # `IS NOT DISTINCT FROM`
            continue
        if reads or (kind, text) == ("other", ","):
            if kind == "other" and not _in_from(tokens, at):
                continue
            target = _table_at(tokens, at + 1)
            if target is not None and target not in allowed:
                raise PlanError("a ClickHouse statement reads a table the model does not name")


def _table_at(tokens: Sequence[tuple[str | None, str]], at: int) -> tuple[str, ...] | None:
    """The table named at `at` (a subquery: None); a word there is a table function's or a
    table's bare name, both refused."""
    if at >= len(tokens) or tokens[at][1] == "(":
        return None
    parts: list[str] = []
    while at < len(tokens) and tokens[at][0] in ("name", "word"):
        if tokens[at][0] == "word":
            raise PlanError("a ClickHouse statement reads a table by a bare name")
        parts.append(unquote_name(tokens[at][1]))
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
