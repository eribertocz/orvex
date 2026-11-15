"""P3-14 (#448; #417): the two parsers that read text from users, fuzzed.

- **The project's SQL parser** (`orvex.semantic.ossie.sql.parse`). On grammar-guided
  expressions, then mutated, it returns a tree or raises `SqlSyntaxError` or `SqlUnsupported`,
  never anything else; it is deterministic; whitespace between tokens never changes the tree;
  nesting is bounded (`MAX_DEPTH`) and a long input ends fast.
- **The ClickHouse statement scanner** (`orvex.store.clickhouse._bound`). On statements built
  from the adapter's own pieces and the lexer's hard cases (strings and their escapes,
  heredocs, quoted names, every comment form, placeholders, `;`, `FORMAT`, `SETTINGS`), it
  accepts or refuses with `STORE_STATEMENT_INVALID` or `STORE_PARAMETER_INVALID`. Every
  statement it accepts is checked against `clickhouse_view`, a lexer written here from
  ClickHouse's documented syntax, independently of the scanner: what ClickHouse would run is
  one `SELECT` (or `WITH`), with no `;` and no `FORMAT`, `SETTINGS` or `INTO OUTFILE`. A
  scanner that agrees only with itself proves nothing.
"""

from __future__ import annotations

import re
import time

from hypothesis import example, given
from hypothesis import strategies as st
from orvex.contracts.store.v1 import Parameter
from orvex.semantic.ossie import sql
from orvex.store import clickhouse
from orvex.store.errors import StoreError

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


# --- the ClickHouse scanner ------------------------------------------------------------------

PIECES = (
    "SELECT", "WITH x AS (SELECT 1)", "(SELECT", ")", "`t`.`amount`", '"t"."zone"', "x", "1",
    ",", "FROM `db`.`t`", "WHERE", "=", "AND", "GROUP BY 1", "ORDER BY 1", "LIMIT 10",
    "{p0:String}", "{p1:Nullable(String)}", "{p2:Int64}", "'a'", "'it''s'", "'\\''",
    "'\\\\'", "$$ raw ; FORMAT $$", "$tag$ x $tag$", "`we``ird`", '"a\\"b"',
    "-- line ; FORMAT\n", "# hash SETTINGS a=1\n", "#! bang ;\n", "#x\n", "// slashes ;\n",
    "/* c ; */", "/* /* nested */ SETTINGS x = 1 */", "/* /* */ '", "' */", "' -- '", "*/",
    "/* open", "'open", "`open", "$x$ open",
    ";", "FORMAT JSON", "SETTINGS max_threads = 1", "INTO OUTFILE 'f'", "format", "\n", " ",
    "Settings", "formatDateTime(x, '%F')", "settingsx", "FORMAT_x",
)  # fmt: skip
TYPES = {"p0": "String", "p1": "Nullable(String)", "p2": "Int64"}


@st.composite
def statements(draw: st.DrawFn) -> str:
    start = draw(st.sampled_from(("SELECT", "WITH x AS (SELECT 1) SELECT", "", "(SELECT")))
    rest = draw(st.lists(st.sampled_from(PIECES), max_size=10))
    glue = draw(st.sampled_from((" ", "", "\n")))
    return glue.join([start, *rest]).strip()


def parameters(statement: str) -> list[Parameter]:
    """One parameter for each placeholder name written anywhere, as the adapter would bind it."""
    named = sorted(set(re.findall(r"\{(p[0-9]):", statement)))
    values = {"p0": ("VARCHAR", "a"), "p1": ("VARCHAR", None), "p2": ("BIGINT", "7")}
    return [
        Parameter.model_validate({"name": n, "type": values[n][0], "value": values[n][1]})
        for n in named
    ]


def clickhouse_view(text: str) -> str | None:
    """What ClickHouse's lexer leaves as code, from its documented syntax (written here, apart
    from the scanner): strings `'…'` with `\\` escapes and `''`; heredocs `$tag$…$tag$`;
    names in backquotes or double quotes with `\\` escapes and doubled quotes; comments `--`,
    `#!`, `# ` and `//` to the end of the line, and `/* … */`, which nest. Each string and name
    becomes `''` or `x`, each comment a space. None when the lexer would refuse the text: an
    unclosed quote or comment, or a `#` that starts no comment."""
    code, at = [], 0
    while at < len(text):
        char, two = text[at], text[at : at + 2]
        if two in {"--", "//", "#!", "# "}:
            end = text.find("\n", at)
            at = len(text) if end < 0 else end
            code.append(" ")
        elif char == "#":
            return None
        elif two == "/*":
            depth, at = 1, at + 2
            while depth:
                if at >= len(text):
                    return None
                if text.startswith("/*", at):
                    depth, at = depth + 1, at + 2
                elif text.startswith("*/", at):
                    depth, at = depth - 1, at + 2
                else:
                    at += 1
            code.append(" ")
        elif char == "$" and (tag := re.match(r"\$(\w*)\$", text[at:])):
            close = text.find(tag.group(0), at + len(tag.group(0)))
            if close < 0:
                return None
            at = close + len(tag.group(0))
            code.append(" '' ")
        elif char in "'`\"":
            at += 1
            while True:
                if at >= len(text):
                    return None
                if text[at] == "\\" or (text[at] == char and text[at + 1 : at + 2] == char):
                    at += 2
                elif text[at] == char:
                    at += 1
                    break
                else:
                    at += 1
            code.append(" '' " if char == "'" else " x ")
        else:
            code.append(char)
            at += 1
    return "".join(code)


PLACEHOLDER = r"\{([A-Za-z_][A-Za-z0-9_]*):([^{}]+)\}"


def runs_as_one_select(code: str) -> bool:
    plain = re.sub(PLACEHOLDER, " 0 ", code).strip()
    return (
        ";" not in plain
        and re.match(r"(?is)^\(*\s*(SELECT|WITH)\b", plain) is not None
        and re.search(r"(?i)\b(SETTINGS|FORMAT|INTO\s+OUTFILE)\b", plain) is None
    )


@PROFILE
@example("SELECT 1 /* /* */ SETTINGS max_threads = 1 */")
@example("SELECT 1 // ; FORMAT JSON\n")
@example("SELECT 1 #SETTINGS max_threads = 1")
@example("SELECT 1 $$ ; $$ FORMAT JSON")
@example("SELECT '\\'' ; SELECT 2")
# Found by the Reviewer pass on #448: a nested comment made the scanner read code as a string.
@example("SELECT 1 /* /* */ ' */ SETTINGS max_execution_time = 0 -- '")
@given(statements())
def test_what_the_scanner_accepts_runs_on_clickhouse_as_one_plain_select(statement: str) -> None:
    try:
        clickhouse._bound(statement, parameters(statement))
    except StoreError as error:
        refused: str | None = error.code
    else:
        refused = None
    if refused is not None:
        assert refused in {"STORE_STATEMENT_INVALID", "STORE_PARAMETER_INVALID"}
        return
    code = clickhouse_view(statement)
    assert code is None or runs_as_one_select(code), code
    if code is not None:  # each placeholder found once, with its written type, where it runs
        written = [(n, t.strip()) for n, t in re.findall(PLACEHOLDER, code)]
        assert clickhouse._scan(statement)[1] == written


def test_the_reference_lexer_sees_what_clickhouse_documents() -> None:
    """The checks above are only as good as this lexer: each documented case, and each
    property, fails on a statement built to break it."""
    assert clickhouse_view("SELECT 1 -- ; x\n") == "SELECT 1  \n"
    assert clickhouse_view("SELECT /* a /* b */ c */ 1") == "SELECT   1"
    assert clickhouse_view("SELECT 'a\\'b'") == "SELECT  '' "
    assert clickhouse_view("SELECT $t$ ; $t$") == "SELECT  '' "
    assert clickhouse_view("SELECT 'open") is None
    assert clickhouse_view("SELECT 1 #x") is None
    assert runs_as_one_select("SELECT 1")
    for broken in (
        "SELECT 1; SELECT 2",
        "SELECT 1 FORMAT JSON",
        "DROP TABLE t",
        "SELECT 1 settings a = 1",
    ):
        assert not runs_as_one_select(broken), broken
