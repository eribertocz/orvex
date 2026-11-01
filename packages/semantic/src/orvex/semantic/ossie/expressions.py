"""Identifiers and the little of an Ossie expression this adapter reads: its references and the
decomposability class of its aggregates.

Ossie identifiers follow ANSI SQL (`expression_language.md` L86-L88): at most 128 characters,
an unquoted identifier is case-insensitive (normalized to upper case), a double-quoted one is
compared exactly. Names written in the document follow the same rule.

This is a lexer, not a parser: it splits an OSSIE_SQL_2026 expression into tokens and reads
(a) the column references, which are the identifiers that are not keywords and not function
names, and (b) the aggregate functions of the decomposability reference (L281-L290). Typing and
the full grammar are P2-04's; P2-04 moves these two checks onto its parser with the same results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MAX_IDENTIFIER = 128

AggregateClass = Literal["distributive", "algebraic", "sketch", "holistic"]
# Most restrictive last: a composite expression has the class of its most restrictive aggregate.
AGGREGATE_CLASSES: tuple[AggregateClass, ...] = ("distributive", "algebraic", "sketch", "holistic")
_AGGREGATES: dict[str, AggregateClass] = {
    "SUM": "distributive",
    "COUNT": "distributive",
    "MIN": "distributive",
    "MAX": "distributive",
    "AVG": "algebraic",
    "STDDEV": "algebraic",
    "STDDEV_POP": "algebraic",
    "STDDEV_SAMP": "algebraic",
    "VARIANCE": "algebraic",
    "VAR_POP": "algebraic",
    "VAR_SAMP": "algebraic",
    "MEDIAN": "holistic",
    "PERCENTILE_CONT": "holistic",
    "PERCENTILE_DISC": "holistic",
    "APPROX_COUNT_DISTINCT": "sketch",
    "APPROX_PERCENTILE": "sketch",
}
# Words of the OSSIE_SQL_2026 subset that are never a column reference: operators, clauses,
# literals, date parts and type names.
KEYWORDS = frozenset(
    """
    AND OR NOT IS NULL TRUE FALSE IN BETWEEN LIKE ILIKE ESCAPE CASE WHEN THEN ELSE END AS
    DISTINCT ALL CAST TRY_CAST FILTER WHERE OVER PARTITION BY ORDER ASC DESC NULLS FIRST LAST
    ROWS RANGE GROUPS UNBOUNDED PRECEDING FOLLOWING CURRENT ROW WITHIN GROUP INTERVAL EXTRACT
    FROM FOR CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP LOCALTIME LOCALTIMESTAMP
    YEAR QUARTER MONTH WEEK DAY DAYOFWEEK DAYOFYEAR HOUR MINUTE SECOND MILLISECOND MICROSECOND
    EPOCH DATE TIME TIMESTAMP TIMESTAMPTZ ZONE WITH WITHOUT AT
    INTEGER INT BIGINT SMALLINT TINYINT DECIMAL NUMERIC REAL FLOAT DOUBLE PRECISION BOOLEAN
    VARCHAR CHAR CHARACTER VARYING STRING TEXT
    """.split()  # noqa: SIM905 - a word list reads better than one string per line
)

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<comment>--[^\n]*)
  | (?P<string>'(?:[^']|'')*')
  | (?P<quoted>"(?:[^"]|"")+")
  | (?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)
  | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<symbol><>|<=|>=|!=|\|\||::|[-+*/%(),.<>=\[\]])
    """,
    re.VERBOSE,
)


class ExpressionUnreadable(ValueError):
    """The text is not a sequence of OSSIE_SQL_2026 tokens."""


def normalize(name: str) -> str:
    """The identity of an identifier: exact inside double quotes, upper case otherwise."""
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        return name[1:-1].replace('""', '"')
    return name.upper()


def identifier_length(name: str) -> int:
    return len(normalize(name))


@dataclass(frozen=True)
class Reference:
    """A column reference: one part (`field`) or two (`dataset.field`), each normalized."""

    parts: tuple[str, ...]


@dataclass(frozen=True)
class Reading:
    references: tuple[Reference, ...]
    aggregates: tuple[AggregateClass, ...]

    @property
    def aggregate_class(self) -> AggregateClass | None:
        """The most restrictive class among the aggregates, or None when there is none."""
        if not self.aggregates:
            return None
        return max(self.aggregates, key=AGGREGATE_CLASSES.index)


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str


def _tokens(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    at = 0
    while at < len(text):
        match = _TOKEN.match(text, at)
        if match is None:
            raise ExpressionUnreadable("an unexpected character")
        at = match.end()
        kind = match.lastgroup
        assert kind is not None
        if kind not in ("space", "comment"):
            tokens.append(_Token(kind, match.group()))
    return tokens


def read(text: str) -> Reading:
    """The references and aggregates of an OSSIE_SQL_2026 expression."""
    tokens = _tokens(text)
    references: list[Reference] = []
    aggregates: list[AggregateClass] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if token.kind == "word" and following is not None and following.text == "(":
            name = token.text.upper()
            aggregate = _AGGREGATES.get(name)
            if aggregate is not None:
                after = tokens[index + 2] if index + 2 < len(tokens) else None
                if name == "COUNT" and after is not None and after.text.upper() == "DISTINCT":
                    aggregate = "holistic"
                aggregates.append(aggregate)
            index += 1
            continue
        if token.kind == "quoted" or (token.kind == "word" and token.text.upper() not in KEYWORDS):
            if index > 0 and tokens[index - 1].text == "::":
                index += 1  # a type name after a cast operator
                continue
            parts = [normalize(token.text)]
            index += 1
            while (
                index + 1 < len(tokens)
                and tokens[index].text == "."
                and tokens[index + 1].kind in ("word", "quoted")
            ):
                parts.append(normalize(tokens[index + 1].text))
                index += 2
            if any(len(part) > MAX_IDENTIFIER for part in parts):
                raise ExpressionUnreadable("an identifier longer than 128 characters")
            references.append(Reference(tuple(parts)))
            continue
        index += 1
    return Reading(tuple(references), tuple(aggregates))
