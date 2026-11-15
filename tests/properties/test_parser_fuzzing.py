"""P3-14 (#448; #417): the project's SQL parser, which reads text from users, fuzzed (the
ClickHouse statement scanner's half is the store's own, `test_clickhouse_scanner_fuzzing.py`).

- **The project's SQL parser** (`orvex.semantic.ossie.sql.parse`). On grammar-guided
  expressions, then mutated, it returns a tree or raises `SqlSyntaxError` or `SqlUnsupported`,
  never anything else; it is deterministic; whitespace between tokens never changes the tree;
  nesting is bounded (`MAX_DEPTH`) and a long input ends fast.
"""

from __future__ import annotations

import re
import time

from hypothesis import example, given
from hypothesis import strategies as st
from orvex.semantic.ossie import sql

from tests.properties.world import PROFILE

# --- the SQL parser --------------------------------------------------------------------------

LEAVES = ("amount", "t.amount", '"Quoted Name"', "1", "2.50", "'text'", "'it''s'", "NULL",
          "TRUE", "DATE '2026-01-01'", "*")  # fmt: skip
UNARY = ("NOT {}", "-{}", "({})", "{} IS NULL", "{} IS NOT NULL", "CAST({} AS DECIMAL(18, 2))",
         "SUM({})", "COUNT(DISTINCT {})", "COALESCE({}, 0)", "EXTRACT(YEAR FROM {})")  # fmt: skip
BINARY = ("{} + {}", "{} * {}", "{} / {}", "{} = {}", "{} <> {}", "{} AND {}", "{} OR {}",
          "{} || {}", "{} IN ({}, 1)", "{} BETWEEN {} AND 3", "{} LIKE {}",
          "CASE WHEN {} THEN {} ELSE NULL END", "{} IS DISTINCT FROM {}")  # fmt: skip
NOISE = ("(", ")", "'", '"', ",", ";", "--", "/*", "*/", "\x00", "\n", "OVER (", "SELECT",
         "ORDER BY", "$1", "::", "[", "]", "{", "é", "\\")  # fmt: skip
PARSE_LIMIT_S = 2.0


def expressions() -> st.SearchStrategy[str]:
    return st.recursive(
        st.sampled_from(LEAVES),
        lambda inner: st.one_of(
            st.tuples(st.sampled_from(UNARY), inner).map(lambda p: p[0].format(p[1])),
            st.tuples(st.sampled_from(BINARY), inner, inner).map(lambda p: p[0].format(p[1], p[2])),
        ),
        max_leaves=12,
    )


@st.composite
def mutated(draw: st.DrawFn) -> str:
    """A generated expression with a few characters inserted, deleted or replaced."""
    text = draw(expressions())
    for _ in range(draw(st.integers(0, 3))):
        at = draw(st.integers(0, len(text)))
        how = draw(st.sampled_from(("insert", "delete", "replace")))
        piece = draw(st.sampled_from(NOISE))
        if how == "insert":
            text = text[:at] + piece + text[at:]
        elif how == "delete":
            text = text[:at] + text[at + 1 :]
        else:
            text = text[:at] + piece + text[at + len(piece) :]
    return text


def outcome(text: str) -> object:
    """The tree, or the kind of refusal: anything else escapes and fails the property."""
    try:
        return sql.parse(text)
    except (sql.SqlSyntaxError, sql.SqlUnsupported) as error:
        return type(error).__name__


@PROFILE
@example("(" * (sql.MAX_DEPTH + 1) + "1" + ")" * (sql.MAX_DEPTH + 1))
@example("NOT " * 5000 + "TRUE")
@example("-" * 5000 + "1")
@example("'unclosed")
@given(st.one_of(expressions(), mutated(), st.text(max_size=80)))
def test_the_sql_parser_returns_a_tree_or_refuses_and_ends_fast(text: str) -> None:
    started = time.monotonic()
    first = outcome(text)
    assert time.monotonic() - started < PARSE_LIMIT_S
    assert outcome(text) == first  # deterministic


@PROFILE
@given(expressions())
def test_whitespace_between_tokens_never_changes_the_tree(text: str) -> None:
    spaced = re.sub(r"([(),])", r"  \1\n ", text)  # tokens never part of another
    assert outcome(spaced) == outcome(text)
