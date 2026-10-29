"""ClickHouse extraction models: validated on ClickHouse itself, without running them (P1-17).

An extraction model's SQL is ClickHouse SQL and runs on ClickHouse (ADR-0026); it is never parsed
here as SQL. Only its text is scanned, for what must be known before asking the server: whether
there is more than one statement, and the `{since:Type}` query parameter (strings, quoted
identifiers and comments skipped). ClickHouse then parses the SQL (`EXPLAIN AST`: one `SELECT`)
and describes its output columns (`DESCRIBE`, nothing executed), with `since` bound server-side
and every query read-only (`readonly = 1`). Every output type maps to an exact DuckDB type, or
the column is refused (`UNSUPPORTED_SOURCE_TYPE`) with a hint to cast it in the SQL.

Describing a query can still make ClickHouse evaluate parts of it (scalar subqueries, also inside
views). Every validation query therefore carries a row limit that ClickHouse enforces, and the
reader must not be able to lift it (decision #114). ClickHouse applies that limit to
MergeTree-family tables; it does not to some other engines (`Memory`, `Log`, `TinyLog`, `File`,
`URL`, and possibly integration engines such as `S3` or `PostgreSQL`): reads of those are bounded
only by the time limit (documented in `contracts/model-config/v1`).

`clickhouse-connect` is ClickHouse, Inc.'s official client (Apache-2.0, ADR-0002).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
import re
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import clickhouse_connect
import pyarrow as pa
import pyarrow.ipc as pa_ipc
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError
from orvex.contracts.model_config.v1 import ModelError
from orvex.contracts.project.v1 import ClickhouseConnection
from orvex.transform.incremental import lookback_microseconds
from orvex.transform.models import Model, problem

SINCE = "since"
CONNECT_TIMEOUT = 10  # seconds
QUERY_TIMEOUT = 30  # seconds, for validation queries (EXPLAIN, DESCRIBE)
# ClickHouse evaluates scalar subqueries while it describes a query: reading is capped, and a
# query that would read rows fails (TOO_MANY_ROWS) after at most one block (MergeTree-family
# tables; ClickHouse does not limit some other engines, see the module docstring).
VALIDATION_LIMITS: tuple[tuple[str, object], ...] = (
    ("max_execution_time", QUERY_TIMEOUT),
    ("max_rows_to_read", 1),
    ("read_overflow_mode", "throw"),
)
_TOO_MANY_ROWS = 158
_READONLY = 164
# Values that lift each validation limit, for the connection check.
_LIFTED_LIMITS = (
    ("max_rows_to_read", "1000000000"),
    ("read_overflow_mode", "'break'"),
    ("max_execution_time", "3600"),
)
_REFERENCE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")

# --- scanning the SQL text ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Parameter:
    """A `{name:Type}` query parameter: its name, its type as written, and its offset."""

    name: str
    type: str
    offset: int


@dataclass(frozen=True, slots=True)
class Scan:
    parameters: tuple[Parameter, ...]
    statements: int  # statements separated by `;` (a final `;` alone does not count)
    text: str  # the SQL without a final `;`, for wrapping in EXPLAIN and DESCRIBE
    # A quoted name with a backslash escape or a character that is not printable (a line break,
    # a separator): ClickHouse prints names unescaped in `EXPLAIN AST`, where it could forge a
    # line of the tree.
    unsafe_name: bool = False


class ScanError(ValueError):
    """Text ClickHouse could not read either: an unterminated string, identifier or comment."""


def _skip_quoted(sql: str, start: int, quote: str) -> int:
    """The offset after the quoted token starting at `start` (backslash escapes and doubled
    quotes, as ClickHouse reads them)."""
    index = start + 1
    while index < len(sql):
        char = sql[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            if index + 1 < len(sql) and sql[index + 1] == quote:
                index += 2
                continue
            return index + 1
        index += 1
    raise ScanError(f"unterminated {quote}…{quote} at offset {start}")


def _skip_block_comment(sql: str, start: int) -> int:
    depth, index = 0, start
    while index < len(sql):
        if sql.startswith("/*", index):
            depth, index = depth + 1, index + 2
        elif sql.startswith("*/", index):
            depth, index = depth - 1, index + 2
            if depth == 0:
                return index
        else:
            index += 1
    raise ScanError(f"unterminated /* comment at offset {start}")


# ClickHouse heredoc strings: `$$…$$` and `$tag$…$tag$`, where a tag may start with a digit.
_HEREDOC = re.compile(r"\$([A-Za-z0-9_]*)\$")
_NAME_CHARACTER = re.compile(r"[A-Za-z0-9_$]")
# `{name:Type}`: ClickHouse reads a parameter only without spaces before the `:`.
_PARAMETER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*):")


def _tokens(sql: str) -> Iterator[tuple[int, int, str]]:
    """(start, end, kind) of the tokens that matter: `code` runs between strings, quoted
    identifiers and comments, which are skipped."""
    index, code_start = 0, 0
    while index < len(sql):
        char = sql[index]
        end: int | None = None
        if char in "'\"`":
            end = _skip_quoted(sql, index, char)
        elif sql.startswith("--", index) or (
            char == "#" and sql[index + 1 : index + 2] in {" ", "!"}
        ):
            newline = sql.find("\n", index)
            end = len(sql) if newline < 0 else newline + 1
        elif sql.startswith("/*", index):
            end = _skip_block_comment(sql, index)
        elif char == "$" and not (index and _NAME_CHARACTER.match(sql[index - 1])):
            # Inside an unquoted name (`a$1$`), `$` is part of the name, not a heredoc.
            heredoc = _HEREDOC.match(sql, index)
            if heredoc is not None:
                closing = sql.find(heredoc.group(0), heredoc.end())
                if closing < 0:
                    raise ScanError(f"unterminated {heredoc.group(0)} string at offset {index}")
                end = closing + len(heredoc.group(0))
        if end is None:
            index += 1
            continue
        if index > code_start:
            yield code_start, index, "code"
        yield index, end, "skipped"
        index = code_start = end
    if code_start < len(sql):
        yield code_start, len(sql), "code"


def _parameter_type(sql: str, start: int) -> tuple[str, int]:
    """The type text of a parameter whose `:` ends before `start`, and the offset after `}`."""
    depth, index = 0, start
    while index < len(sql):
        char = sql[index]
        if char in "'\"`":
            index = _skip_quoted(sql, index, char)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "}" and depth == 0:
            return sql[start:index].strip(), index + 1
        index += 1
    raise ScanError(f"unterminated query parameter at offset {start}")


def scan(sql: str) -> Scan:
    """The query parameters and statement count of ClickHouse SQL; `ScanError` when a string,
    quoted identifier, comment or parameter is not terminated."""
    # The SQL with strings, quoted identifiers and comments blanked out (same offsets).
    masked = list(sql)
    unsafe_name = False
    for start, end, kind in _tokens(sql):
        if kind == "skipped":
            masked[start:end] = [" "] * (end - start)
            if sql[start] in '"`':
                name = sql[start + 1 : end - 1]
                unsafe_name = unsafe_name or "\\" in name or not name.isprintable()
    code = "".join(masked)
    parameters: list[Parameter] = []
    index = 0
    while (index := code.find("{", index)) >= 0:
        match = _PARAMETER.match(code, index)
        if match is None:
            index += 1
            continue
        # The type may hold quoted text (`DateTime64(6, 'UTC')`): it is read from the SQL.
        type_text, after = _parameter_type(sql, match.end())
        parameters.append(Parameter(match.group(1), type_text, index))
        code = code[:index] + " " * (after - index) + code[after:]
        index = after
    segments = code.split(";")
    statements = sum(1 for segment in segments if segment.strip())
    text = sql
    if len(segments) > 1 and not segments[-1].strip():
        last = code.rindex(";")
        text = sql[:last] + sql[last + 1 :]
    return Scan(tuple(parameters), statements, text, unsafe_name)


# --- the `since` parameter type -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SinceType:
    """The declared type of `{since:…}`: its family, precision and time zone."""

    family: str  # Date, Date32, DateTime, DateTime64
    precision: int | None
    zone: str | None
    text: str  # as written, normalized

    def lowest(self) -> str:
        """The lowest value of the type, as ClickHouse reads it for a parameter: what a first
        run and a full refresh bind (transformation.md §3.3)."""
        if self.family == "Date":
            return "1970-01-01"
        if self.family == "Date32":
            return "1900-01-01"
        if self.family == "DateTime":
            return "1970-01-01 00:00:00"
        return "1900-01-01 00:00:00"


_ZONE = r"'([A-Za-z0-9_+\-/]+)'"
_SINCE_TYPES = (
    (re.compile(r"Date"), "Date"),
    (re.compile(r"Date32"), "Date32"),
    (re.compile(r"DateTime"), "DateTime"),
    (re.compile(rf"DateTime\(\s*{_ZONE}\s*\)"), "DateTime"),
    (re.compile(r"DateTime64\(\s*([0-9])\s*\)"), "DateTime64"),
    (re.compile(rf"DateTime64\(\s*([0-9])\s*,\s*{_ZONE}\s*\)"), "DateTime64"),
)


def since_type(text: str) -> SinceType | None:
    """The parameter type when it is a date or time type ClickHouse can compare a time column
    with: `Date`, `Date32`, `DateTime`, `DateTime('tz')`, `DateTime64(p)`, `DateTime64(p, 'tz')`."""
    for pattern, family in _SINCE_TYPES:
        match = pattern.fullmatch(text.strip())
        if match is None:
            continue
        groups = match.groups()
        precision = int(groups[0]) if family == "DateTime64" else None
        zone = groups[-1] if groups and not groups[-1].isdigit() else None
        return SinceType(family, precision, zone, " ".join(text.split()))
    return None


def since_problems(model: Model) -> list[ModelError]:
    """The rules of `{since:Type}` in an extraction model, from its text alone
    (transformation.md §3.5): incremental models need it, others must not use it, its type is a
    date or time type, and no other parameter can be bound."""
    file = model.sql_file
    try:
        found = scan(model.sql)
    except ScanError as error:
        return [problem("INVALID_MODEL_SQL", f"{file}: {error}.", file, model=model.name)]
    if found.unsafe_name:
        return [
            problem(
                "INVALID_MODEL_SQL",
                f"{file}: a quoted name of an extraction model cannot contain a line break, a "
                "control character or a backslash escape.",
                file,
                model=model.name,
            )
        ]
    if found.statements != 1:
        return [
            problem(
                "INVALID_MODEL_SQL",
                f"{file}: an extraction model is exactly one SELECT; found {found.statements} "
                "statements.",
                file,
                model=model.name,
            )
        ]
    problems: list[ModelError] = []
    incremental = model.config.materialized == "incremental"
    for parameter in found.parameters:
        line = model.sql.count("\n", 0, parameter.offset) + 1
        if parameter.name != SINCE:
            problems.append(
                problem(
                    "INVALID_MODEL_SQL",
                    f"{file}:{line}: the query parameter {{{parameter.name}:…}} is never bound; "
                    "only {since:Type} is.",
                    file,
                    model=model.name,
                    line=line,
                )
            )
        elif not incremental:
            problems.append(
                problem(
                    "INVALID_MODEL_SQL",
                    f"{file}:{line}: {{since:…}} is only bound for incremental models.",
                    file,
                    model=model.name,
                    line=line,
                )
            )
        elif since_type(parameter.type) is None:
            problems.append(
                problem(
                    "MODEL_CONFIG_INVALID",
                    f"{file}:{line}: {{since:{parameter.type}}} must be Date, Date32, DateTime, "
                    "DateTime('zone'), DateTime64(p) or DateTime64(p, 'zone').",
                    file,
                    model=model.name,
                    line=line,
                )
            )
    since = {" ".join(p.type.split()) for p in found.parameters if p.name == SINCE}
    if len(since) > 1:
        problems.append(
            problem(
                "MODEL_CONFIG_INVALID",
                f"{file}: {{since:…}} is declared with different types "
                f"({', '.join(sorted(since))}).",
                file,
                model=model.name,
            )
        )
    filter_kind = model.config.incremental.filter if model.config.incremental else None
    if incremental and (filter_kind or "auto") == "manual" and not since:
        problems.append(
            problem(
                "MISSING_SINCE_PARAMETER",
                f"{file}: an incremental extraction model filters its own rows with "
                "{since:Type} (for example WHERE event_time > {since:DateTime64(6, 'UTC')}).",
                file,
                model=model.name,
            )
        )
    return problems


def declared_since(model: Model) -> SinceType | None:
    """The type of the model's `{since:…}` parameter, when it is declared and valid."""
    try:
        found = scan(model.sql)
    except ScanError:
        return None
    for parameter in found.parameters:
        if parameter.name == SINCE:
            return since_type(parameter.type)
    return None


# --- types ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Type:
    name: str
    arguments: tuple[_Type | str, ...]


def _parse_type(text: str) -> _Type:
    """A ClickHouse type expression: `Name` or `Name(argument, …)`, where an argument is a type,
    a number or a quoted string (kept as text)."""
    position = 0

    def skip_spaces() -> None:
        nonlocal position
        while position < len(text) and text[position].isspace():
            position += 1

    def parse() -> _Type | str:
        nonlocal position
        skip_spaces()
        if position < len(text) and text[position] == "'":
            end = _skip_quoted(text, position, "'")
            literal = text[position:end]
            position = end
            skip_spaces()
            if text.startswith("=", position):  # Enum entries: 'a' = 1
                position += 1
                skip_spaces()
                number = re.match(r"-?[0-9]+", text[position:])
                if number is None:
                    raise ValueError(text)
                position += number.end()
            return literal
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*|-?[0-9]+", text[position:])
        if match is None:
            raise ValueError(text)
        word = match.group(0)
        position += match.end()
        skip_spaces()
        if word[0].isdigit() or word[0] == "-":
            return word
        # Named tuple elements (`a UInt8`) and similar: the word is a name, the type follows.
        if position < len(text) and (text[position].isalpha() or text[position] == "_"):
            inner = parse()
            return inner if isinstance(inner, _Type) else _Type(word, ())
        arguments: list[_Type | str] = []
        if text.startswith("(", position):
            position += 1
            while True:
                arguments.append(parse())
                skip_spaces()
                if text.startswith(",", position):
                    position += 1
                    continue
                if text.startswith(")", position):
                    position += 1
                    break
                raise ValueError(text)
        return _Type(word, tuple(arguments))

    parsed = parse()
    skip_spaces()
    if position != len(text) or not isinstance(parsed, _Type):
        raise ValueError(text)
    return parsed


_STRING = _Type("String", ())
_INTEGERS = {
    "Int8": "TINYINT",
    "Int16": "SMALLINT",
    "Int32": "INTEGER",
    "Int64": "BIGINT",
    "Int128": "HUGEINT",
    "UInt8": "UTINYINT",
    "UInt16": "USMALLINT",
    "UInt32": "UINTEGER",
    "UInt64": "UBIGINT",
    "UInt128": "UHUGEINT",
}
_SIMPLE = {
    **_INTEGERS,
    "Float32": "FLOAT",
    "Float64": "DOUBLE",
    "String": "VARCHAR",
    "Bool": "BOOLEAN",
    "UUID": "UUID",
    "Date": "DATE",
    "Date32": "DATE",
}


@dataclass(frozen=True, slots=True)
class Mapped:
    """A ClickHouse column type mapped to DuckDB: the type, whether it may be NULL, and the
    time zone of a `DateTime` column (None: the server's)."""

    duckdb: str
    nullable: bool
    zone: str | None = None


def map_type(text: str) -> Mapped | None:
    """The exact DuckDB type of a ClickHouse column type, or None when there is none
    (transformation.md §3.5; `contracts/model-config/v1`, `UNSUPPORTED_SOURCE_TYPE`)."""
    try:
        parsed = _parse_type(text)
    except ValueError:
        return None
    nullable = False
    if parsed.name == "LowCardinality":
        # Only over strings (the listed mapping); LowCardinality(Nullable(String)) included.
        inner = parsed.arguments[0] if len(parsed.arguments) == 1 else None
        if not isinstance(inner, _Type) or inner not in (_STRING, _Type("Nullable", (_STRING,))):
            return None
        parsed = inner
    if parsed.name == "Nullable" and len(parsed.arguments) == 1:
        inner = parsed.arguments[0]
        if not isinstance(inner, _Type):
            return None
        parsed, nullable = inner, True
    name, arguments = parsed.name, parsed.arguments
    if name in _SIMPLE and not arguments:
        return Mapped(_SIMPLE[name], nullable)
    if name == "FixedString" and len(arguments) == 1:
        return Mapped("VARCHAR", nullable)
    if name in {"Enum8", "Enum16"} and arguments:
        return Mapped("VARCHAR", nullable)
    if name == "Decimal" and len(arguments) == 2 and all(isinstance(a, str) for a in arguments):
        precision, scale = (int(str(a)) for a in arguments)
        if 1 <= precision <= 38 and 0 <= scale <= precision:
            return Mapped(f"DECIMAL({precision},{scale})", nullable)
        return None
    if name == "DateTime" and len(arguments) <= 1:
        zone = _zone(arguments[0]) if arguments else None
        if arguments and zone is None:
            return None
        return Mapped("TIMESTAMPTZ", nullable, zone)
    if name == "DateTime64" and 1 <= len(arguments) <= 2:
        precision_text = arguments[0]
        if not isinstance(precision_text, str) or not precision_text.isdigit():
            return None
        if int(precision_text) > 6:
            return None  # DuckDB keeps microseconds: more would be rounded (law 11)
        zone = _zone(arguments[1]) if len(arguments) == 2 else None
        if len(arguments) == 2 and zone is None:
            return None
        return Mapped("TIMESTAMPTZ", nullable, zone)
    return None


def _zone(argument: _Type | str) -> str | None:
    if isinstance(argument, str) and len(argument) >= 2 and argument[0] == argument[-1] == "'":
        return argument[1:-1]
    return None


# Subqueries ClickHouse does not evaluate while it describes a query: tables in FROM, named WITH
# tables, and the right side of IN.
_TABLE_PARENTS = frozenset({"TableExpression", "WithElement"})
_IN_FUNCTIONS = frozenset(
    {
        "in",
        "notIn",
        "globalIn",
        "globalNotIn",
        "nullIn",
        "notNullIn",
        "globalNullIn",
        "globalNotNullIn",
    }
)
# A line of `EXPLAIN AST`: indentation, a node kind, then its details.
_AST_LINE = re.compile(r"( *)([A-Z][A-Za-z]*(?:::[A-Z][A-Za-z]*)*)(?: (.*))?")
_CHILDREN = re.compile(r"\(children ([0-9]+)\)$")


@dataclass
class _Node:
    kind: str
    detail: str
    children: list[_Node]


def _ast(tree: str) -> _Node | None:
    """The `EXPLAIN AST` text as a tree, or None when a line is not a node line (a name with a
    line break would forge one; names with them are refused before, this fails closed)."""
    root = _Node("", "", [])
    stack: list[tuple[int, _Node]] = [(-1, root)]
    for line in tree.split("\n"):
        if not line:
            continue
        match = _AST_LINE.fullmatch(line)
        if match is None:
            return None
        depth = len(match.group(1))
        if depth > stack[-1][0] + 1:
            return None
        while stack[-1][0] >= depth:
            stack.pop()
        node = _Node(match.group(2), match.group(3) or "", [])
        stack[-1][1].children.append(node)
        stack.append((depth, node))
    # Every node has exactly the children it announces, under one root: a forged line breaks it.
    if len(root.children) != 1 or not _counts_match(root.children[0]):
        return None
    return root


def _counts_match(node: _Node) -> bool:
    announced = _CHILDREN.search(node.detail)
    expected = int(announced.group(1)) if announced else 0
    return len(node.children) == expected and all(_counts_match(c) for c in node.children)


def _function_name(node: _Node) -> str:
    return node.detail.split(" ", 1)[0] if node.kind == "Function" else ""


def sets_settings(tree: str) -> bool:
    """Whether the `EXPLAIN AST` tree has a `SETTINGS` clause (a `Set` node) anywhere, or cannot
    be read as a tree: a model must not change the limits its queries run with."""
    root = _ast(tree)
    if root is None:
        return True

    def visit(node: _Node) -> bool:
        return node.kind == "Set" or any(visit(child) for child in node.children)

    return visit(root)


def evaluated_while_describing(tree: str) -> bool:
    """Whether describing the query would make ClickHouse read data: a scalar or EXISTS subquery
    (anything but a table in FROM, a named WITH table or the right side of IN). An `EXPLAIN AST`
    text that cannot be read as a tree counts as yes. This refuses early, with a clear message,
    what the row limit would stop anyway (views the SQL reads are covered only by the limit)."""
    root = _ast(tree)
    if root is None:
        return True

    def visit(node: _Node, parent: _Node | None, grandparent: _Node | None, index: int) -> bool:
        if node.kind == "Subquery":
            right_of_in = (
                parent is not None
                and parent.kind == "ExpressionList"
                and grandparent is not None
                and _function_name(grandparent) in _IN_FUNCTIONS
                and index == 1
            )
            if not right_of_in and (parent is None or parent.kind not in _TABLE_PARENTS):
                return True
        return any(
            visit(child, node, parent, position) for position, child in enumerate(node.children)
        )

    return any(visit(child, None, None, position) for position, child in enumerate(root.children))


# --- the server -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    clickhouse: str  # the type ClickHouse reports
    mapped: Mapped


@dataclass(frozen=True, slots=True)
class Described:
    """An extraction model's output: its columns, and the server's time zone (the zone of
    `DateTime` columns that declare none)."""

    columns: tuple[Column, ...]
    server_zone: str


class SourceError(Exception):
    """A ClickHouse failure, already worded for the user (no credentials in it)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def password_variable(connection: ClickhouseConnection) -> str:
    match = _REFERENCE.fullmatch(connection.password.root)
    if match is None:  # the project contract allows nothing else
        raise ValueError("the password is not a ${env:NAME} reference")
    return match.group(1)


def _clean(text: str, secret: str) -> str:
    """ClickHouse's message without the password, should a driver or proxy ever echo it."""
    return text.replace(secret, "***") if secret else text


class Source:
    """A read-only client of a ClickHouse connection: every query the engine sends carries the
    validation limits and `readonly = 1`, and the connection is refused unless the user accepts
    them and cannot lift them; connect and receive timeouts are explicit, and no session state is
    kept. On connecting, the client itself reads the server's version, time zone and settings
    (system tables, no user data)."""

    def __init__(self, name: str, connection: ClickhouseConnection, password: str) -> None:
        self.name = name
        self._password = password
        self._connection = connection
        try:
            self._client: Client = clickhouse_connect.get_client(
                host=connection.host,
                port=connection.port,
                database=connection.database,
                username=connection.user,
                password=password,
                secure=connection.secure is not False,
                connect_timeout=CONNECT_TIMEOUT,
                send_receive_timeout=QUERY_TIMEOUT,
                autogenerate_session_id=False,
            )
        except ClickHouseError as error:
            raise self._error(error, "SOURCE_CONNECTION_FAILED") from None
        readonly = self._client.server_settings.get("readonly")
        # Fixed by the user's profile: only a fixed `readonly = 1` is read-only for data and
        # settings; `0` (read-write) or `2` (settings may change) is refused.
        fixed_read_only = readonly is not None and bool(readonly.readonly)
        if readonly is not None and fixed_read_only and str(readonly.value) != "1":
            self._client.close()
            raise SourceError(
                "SOURCE_NOT_READ_ONLY",
                f"ClickHouse connection {name}: the user's profile fixes readonly = "
                f"{readonly.value}, so its queries cannot be made read-only; use a user whose "
                "profile does not fix readonly (only SELECT grants).",
            )
        # Decision #114 (option C): the reader must accept the validation limits, and SQL run
        # by that reader must not be able to lift them, so ClickHouse itself stops any read (a
        # view, a subquery) after the first block. The server is asked once: a query with the
        # limits must run, and, for each limit, a query lifting it must fail as a read-only
        # violation (below).
        refusal = SourceError(
            "SOURCE_LIMITS_REFUSED",
            f"ClickHouse connection {name}: the user must accept, and must not be able to change, "
            f"the limits of validation queries ({', '.join(key for key, _ in VALIDATION_LIMITS)}); "
            "use a user with only SELECT grants, without a settings profile or role that fixes "
            "readonly or makes these settings changeable in read-only mode.",
        )
        try:
            self._client.query("SELECT 1", settings=self.settings())
        except ClickHouseError as error:
            self._client.close()
            if isinstance(error, OperationalError):
                raise self._error(error, "SOURCE_CONNECTION_FAILED") from None
            raise refusal from None
        # Each limit, lifted by the SQL itself, must be refused as a read-only violation: any
        # other outcome (it runs, or a constraint refuses only that value) means SQL, or a view's
        # definition, could lift it.
        for setting, lifted in _LIFTED_LIMITS:
            try:
                self._client.query(
                    f"SELECT 1 SETTINGS {setting} = {lifted}", settings=self.settings()
                )
            except ClickHouseError as error:
                if isinstance(error, OperationalError):
                    self._client.close()
                    raise self._error(error, "SOURCE_CONNECTION_FAILED") from None
                if getattr(error, "code", None) == _READONLY:
                    continue
            self._client.close()
            raise refusal

    def _error(self, error: BaseException, code: str) -> SourceError:
        """The failure as the user reads it: network and server-unavailable failures are the
        connection's (`SOURCE_CONNECTION_FAILED`); a query ClickHouse rejects is `code`."""
        if isinstance(error, OperationalError):
            code = "SOURCE_CONNECTION_FAILED"
        if getattr(error, "code", None) == _TOO_MANY_ROWS:
            return SourceError("INVALID_MODEL_SQL", self._rows_read_message())
        text = " ".join(_clean(str(error), self._password).split())
        return SourceError(code, f"ClickHouse connection {self.name}: {text}")

    @property
    def server_zone(self) -> str:
        """The server's time zone: the zone of `DateTime` values and parameters that declare
        none."""
        return str(self._client.server_tz)

    def settings(self) -> dict[str, object]:
        """Settings for each validation query: the limits, then `readonly = 1`. They are sent with
        the query, never kept in a session; ClickHouse checks them together before applying them,
        so the limits are accepted although `readonly = 1` forbids later changes (the connection
        check has made sure the user's profile allows exactly that)."""
        settings: dict[str, object] = dict(VALIDATION_LIMITS)
        settings["readonly"] = 1
        return settings

    def _parameters(self, since: SinceType | None) -> dict[str, str] | None:
        return {SINCE: since.lowest()} if since is not None else None

    def _rows(self, sql: str, since: SinceType | None) -> list[tuple[Any, ...]]:
        try:
            result = self._client.query(
                sql, parameters=self._parameters(since), settings=self.settings()
            )
        except ClickHouseError as error:
            raise self._error(error, "INVALID_MODEL_SQL") from None
        return [tuple(row) for row in result.result_rows]

    def check_select(self, text: str, since: SinceType | None) -> None:
        """ClickHouse parses the SQL (nothing runs); `SourceError` with `INVALID_MODEL_SQL` when it
        does not parse or is not a SELECT."""
        # Sent as is: a `FORMAT` clause added after it would belong to the explained query.
        try:
            answer = self._client.raw_query(
                f"EXPLAIN AST\n{text}\n",
                parameters=self._parameters(since),
                settings=self.settings(),
            )
        except ClickHouseError as error:
            raise self._error(error, "INVALID_MODEL_SQL") from None
        tree = answer.decode("utf-8", "replace").strip("\n")
        first = tree.lstrip().split("\n", 1)[0].strip()
        if not first.startswith("SelectWithUnionQuery"):
            kind = first.split(" ", 1)[0] or "nothing"
            raise SourceError(
                "INVALID_MODEL_SQL",
                f"an extraction model is one SELECT; ClickHouse reads {kind}.",
            )
        if sets_settings(tree):
            raise SourceError(
                "INVALID_MODEL_SQL",
                f"ClickHouse connection {self.name}: an extraction model cannot set ClickHouse "
                "settings (SETTINGS …); the engine sets the limits of its queries itself.",
            )
        if evaluated_while_describing(tree):
            raise SourceError("INVALID_MODEL_SQL", self._rows_read_message())

    def _rows_read_message(self) -> str:
        return (
            f"ClickHouse connection {self.name}: validation would read rows of the source "
            "(ClickHouse evaluates scalar and EXISTS subqueries, including those inside a view "
            "the SQL reads, while it describes a query); move a subquery into a JOIN or a "
            "WITH … AS (SELECT …) table."
        )

    def describe(self, text: str, since: SinceType | None) -> Described:
        """The output columns (`DESCRIBE`, nothing executed) and the server's time zone."""
        rows = self._rows(f"DESCRIBE TABLE (\n{text}\n)", since)
        columns: list[Column] = []
        unsupported: list[str] = []
        for row in rows:
            name, kind = str(row[0]), str(row[1])
            mapped = map_type(kind)
            if mapped is None:
                unsupported.append(f"{name} ({kind})")
                continue
            columns.append(Column(name, kind, mapped))
        if unsupported:
            raise SourceError(
                "UNSUPPORTED_SOURCE_TYPE",
                f"no exact DuckDB type for {', '.join(unsupported)}; cast it in the SQL "
                "(for example toDecimal128(x, 10) or toString(x)).",
            )
        return Described(tuple(columns), str(self._client.server_tz))

    def check_columns(
        self, text: str, since: SinceType | None, expected: tuple[Column, ...]
    ) -> None:
        """The model's output, described again when it runs, must be what validation mapped: a
        source that changed type since then fails before anything is read (`SCHEMA_CHANGED`)."""
        now = self.describe(text, since).columns
        if [(c.name, c.clickhouse) for c in now] != [(c.name, c.clickhouse) for c in expected]:
            listed = ", ".join(f"{c.name} {c.clickhouse}" for c in now)
            raise SourceError(
                "SCHEMA_CHANGED",
                f"ClickHouse connection {self.name}: the model's columns changed since it was "
                f"validated (now {listed}); nothing is written.",
            )

    def stream(
        self, text: str, columns: tuple[Column, ...], since: str | None, seconds: int
    ) -> Stream:
        """Run the model's SQL for its rows: read-only, `since` bound server-side, stopped by
        ClickHouse after `seconds` and when the response is closed. The result is an Arrow
        stream of bounded record batches; no row is decoded in Python."""
        settings: dict[str, object] = {
            "readonly": 1,
            "max_execution_time": max(1, seconds),
            "cancel_http_readonly_queries_on_client_close": 1,
            "max_block_size": BLOCK_SIZE,
            "preferred_block_size_bytes": BLOCK_BYTES,
            "output_format_arrow_string_as_string": 1,
            # A user's profile must never shorten a run silently: a limit it sets fails the
            # query instead of truncating it, and result-trimming settings are off.
            **dict.fromkeys(OVERFLOW_MODES, "throw"),
            "limit": 0,
            "offset": 0,
            # Row filters a profile can add to every table or to the result.
            "additional_table_filters": {},
            "additional_result_filter": "",
        }
        # Its own client: the receive timeout covers the run (a query may scan for minutes
        # before its next rows), not the validation queries' 30 s.
        try:
            client: Client = clickhouse_connect.get_client(
                host=self._connection.host,
                port=self._connection.port,
                database=self._connection.database,
                username=self._connection.user,
                password=self._password,
                secure=self._connection.secure is not False,
                connect_timeout=CONNECT_TIMEOUT,
                send_receive_timeout=max(1, seconds) + RECEIVE_MARGIN,
                autogenerate_session_id=False,
            )
        except ClickHouseError as error:
            raise self._error(error, "SOURCE_CONNECTION_FAILED") from None
        # A setting the user's profile fixes to another value: the driver would refuse it before
        # sending (or ClickHouse would, below); either way the run cannot get its guarantees.
        fixed = sorted(
            key
            for key, value in settings.items()
            if (found := client.server_settings.get(key)) is not None
            and found.readonly
            and str(found.value) != str(value)
        )
        if fixed:
            client.close()
            raise self._limits_refused(fixed)
        try:
            response = client.raw_stream(
                extraction_query(text, columns),
                parameters={SINCE: since} if since is not None else None,
                settings=settings,
                fmt="ArrowStream",
            )
        except ClickHouseError as error:
            client.close()
            if getattr(error, "code", None) in _SETTING_REFUSED:
                raise self._limits_refused([]) from None
            raise self.failure(error) from None
        assert isinstance(response, io.IOBase)  # `raw_stream` returns the HTTP response
        return Stream(self, response, client, columns)

    def _limits_refused(self, fixed: list[str]) -> SourceError:
        named = f" ({', '.join(fixed)})" if fixed else ""
        return SourceError(
            "SOURCE_LIMITS_REFUSED",
            f"ClickHouse connection {self.name}: the user's profile does not let a run set the "
            f"settings it needs{named}: read-only, its time limit, its block size, every "
            "overflow mode 'throw' (a limit fails the run, never shortens it) and limit = 0, "
            "offset = 0; remove those constraints from the profile.",
        )

    def failure(self, error: BaseException) -> SourceError:
        """A failure of a run query, worded like every other source error, with ClickHouse's
        own code when its message gives one."""
        failure = self._error(error, "SOURCE_QUERY_FAILED")
        found = _EXCEPTION_CODE.search(failure.message)
        if found is None or failure.code != "SOURCE_QUERY_FAILED":
            return failure
        return SourceError(failure.code, f"{failure.message} (ClickHouse code {found.group(1)})")

    def close(self) -> None:
        self._client.close()


_TAIL_BYTES = 65_536  # the end of the response kept, to find an exception written into it
_DRAIN_BYTES = 1_048_576  # read after a failure, at most, to reach that exception
_EXCEPTION_CODE = re.compile(r"Code: ([0-9]+)\.")


def _partial_bytes(error: BaseException) -> bytes:
    """The bytes a truncated HTTP read did receive (`http.client.IncompleteRead.partial`),
    found in the error or the errors it wraps."""
    seen: set[int] = set()
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        partial = getattr(current, "partial", None)
        if isinstance(partial, bytes) and partial:
            return partial
        pending += [item for item in current.args if isinstance(item, BaseException)]
        pending += [item for item in (current.__cause__, current.__context__) if item is not None]
    return b""


class _Recorded(io.RawIOBase):
    """The HTTP response as a file for Arrow, keeping its last bytes."""

    def __init__(self, response: io.IOBase) -> None:
        self._response = response
        self.tail = bytearray()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        try:
            data = self._response.read(len(buffer))
        except Exception as error:
            # ClickHouse ends a failed response with a broken chunk; its last bytes (where the
            # exception is written) travel in the transport error.
            self._keep(_partial_bytes(error))
            raise
        size = len(data)
        buffer[:size] = data
        self.tail += data
        del self.tail[:-_TAIL_BYTES]
        return size

    def _keep(self, data: bytes) -> None:
        self.tail += data
        del self.tail[:-_TAIL_BYTES]

    def drain(self) -> None:
        """Read what is left of the response (bounded), so an exception at its end is seen."""
        with contextlib.suppress(Exception):
            left = _DRAIN_BYTES
            while left > 0:
                try:
                    data = self._response.read(min(left, 65_536))
                except Exception as error:  # noqa: BLE001 (the broken last chunk)
                    self._keep(_partial_bytes(error))
                    break
                if not data:
                    break
                left -= len(data)
                self._keep(data)


class Stream:
    """A running extraction query: its Arrow record batches (`reader`), the rows read so far,
    and `close`, callable from any thread, which stops the query.

    Closing shuts the connection's socket down first: a plain close would wait for a read that
    is blocked while ClickHouse scans without sending rows, and the query would go on. ClickHouse
    then cancels the query (`cancel_http_readonly_queries_on_client_close`). After a close, the
    stream never ends as if it were complete.

    An error after the first bytes cannot change the HTTP status any more: ClickHouse writes it
    at the end of the body between `__exception__` markers with the response's exception tag.
    It is read from there, so a failure mid-stream reports ClickHouse's own code and message.
    Before the first batch, the stream's Arrow types are compared with the validated columns
    (`SCHEMA_CHANGED`), so a source changed after `check_columns` is never cast silently.
    """

    def __init__(
        self,
        source: Source,
        response: io.IOBase,
        client: Client | None = None,
        columns: tuple[Column, ...] = (),
    ) -> None:
        self._source = source
        self._response = response
        self._client = client
        headers = getattr(response, "headers", None)
        self._tag = headers.get("X-ClickHouse-Exception-Tag") if headers is not None else None
        self._file = _Recorded(response)
        self.rows = 0
        self._closed = threading.Event()
        try:
            self._batches = pa_ipc.open_stream(io.BufferedReader(self._file))
        except Exception as error:  # noqa: BLE001 (a ClickHouse error, or a broken connection)
            failure = self._failure(error)
            self.close()
            raise failure from None
        changed = _schema_problem(self._batches.schema, columns) if columns else None
        if changed is not None:
            self.close()
            raise SourceError(
                "SCHEMA_CHANGED",
                f"ClickHouse connection {source.name}: the model's columns changed while it ran "
                f"({changed}); nothing is written.",
            )
        self.reader = pa.RecordBatchReader.from_batches(self._batches.schema, self._read())

    def _read(self) -> Iterator[pa.RecordBatch]:
        try:
            for batch in self._batches:
                if self._closed.is_set():
                    break
                self.rows += batch.num_rows
                yield batch
        except Exception as error:  # noqa: BLE001 (network or ClickHouse, mid-stream)
            if self._closed.is_set():
                raise _stopped() from None
            raise self._failure(error) from None
        if self._closed.is_set():
            # The end of a closed response is not the end of the result.
            raise _stopped()

    def _failure(self, error: BaseException) -> SourceError:
        """ClickHouse's own error when the body ends with one, else the transport failure."""
        self._file.drain()
        message = self._exception()
        return self._source.failure(error if message is None else RuntimeError(message))

    def _exception(self) -> str | None:
        if not self._tag:
            return None
        text = bytes(self._file.tail).decode("utf-8", "replace")
        marker = f"__exception__\r\n{self._tag}\r\n"
        start = text.rfind(marker)
        if start < 0:
            return None
        body = text[start + len(marker) :]
        end = body.rfind(f" {self._tag}\r\n__exception__")
        if end >= 0:
            # The message, then a line with its length and the tag.
            body = body[:end]
            message, _, length = body.rstrip().rpartition("\n")
            if length.strip().isdigit():
                body = message
        return " ".join(body.split()) or None

    def close(self) -> None:
        self._closed.set()
        connection = getattr(self._response, "connection", None)
        sock = getattr(connection, "sock", None)
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            self._response.close()
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()


def _stopped() -> SourceError:
    return SourceError("SOURCE_QUERY_FAILED", "the ClickHouse query was stopped before its end.")


def _expected_arrow(column: Column) -> Callable[[pa.DataType], bool]:
    """Whether an Arrow type is what the projection sends for this column."""
    duck = column.mapped.duckdb
    family = _base(column.clickhouse)
    if family in _AS_TEXT or duck in {"VARCHAR", "HUGEINT", "UHUGEINT"}:
        return lambda kind: pa.types.is_string(kind) or pa.types.is_large_string(kind)
    if duck == "TIMESTAMPTZ":
        return lambda kind: pa.types.is_timestamp(kind) and kind.tz is not None
    if duck == "DATE":
        return lambda kind: pa.types.is_date32(kind) or pa.types.is_date64(kind)
    if duck.startswith("DECIMAL("):
        precision, scale = (int(part) for part in duck[8:-1].split(","))
        return lambda kind: (
            pa.types.is_decimal(kind) and (kind.precision, kind.scale) == (precision, scale)
        )
    simple = {
        "BOOLEAN": pa.bool_(),
        "FLOAT": pa.float32(),
        "DOUBLE": pa.float64(),
        "TINYINT": pa.int8(),
        "SMALLINT": pa.int16(),
        "INTEGER": pa.int32(),
        "BIGINT": pa.int64(),
        "UTINYINT": pa.uint8(),
        "USMALLINT": pa.uint16(),
        "UINTEGER": pa.uint32(),
        "UBIGINT": pa.uint64(),
    }
    if duck in simple:
        expected = simple[duck]
        return lambda kind: kind == expected
    if duck == "UUID":
        return lambda kind: (
            str(kind).startswith("extension<arrow.uuid")
            or (pa.types.is_fixed_size_binary(kind) and kind.byte_width == 16)
        )
    return lambda _: False


def _schema_problem(schema: pa.Schema, columns: tuple[Column, ...]) -> str | None:
    """Why the stream's schema is not the validated one, or None."""
    names = [column.name for column in columns]
    if schema.names[: len(names)] != names:
        return "columns " + ", ".join(schema.names)
    for column, field in zip(columns, schema, strict=False):
        if not _expected_arrow(column)(field.type):
            return f"{column.name} arrives as {field.type}, validated as {column.clickhouse}"
    return None


def describe_model(
    model: Model, connection: ClickhouseConnection, password: str
) -> Described | ModelError:
    """Validate an extraction model on its ClickHouse connection and describe its output, or
    the first problem found. No row is read."""
    file = model.sql_file
    assert model.config.connection is not None
    found = scan(model.sql)
    if found.unsafe_name or found.statements != 1:  # `since_problems` reports why
        return problem("INVALID_MODEL_SQL", f"{model.name}: not one plain SELECT.", file)
    since = declared_since(model)
    try:
        source = Source(model.config.connection, connection, password)
    except SourceError as error:
        return problem(error.code, error.message, file, model=model.name)
    try:
        source.check_select(found.text, since)
        return source.describe(found.text, since)
    except SourceError as error:
        return problem(error.code, f"{model.name}: {error.message}", file, model=model.name)
    finally:
        source.close()


def read_password(connection: ClickhouseConnection) -> str | None:
    """The connection's password from its environment variable, or None when it is unset."""
    return os.environ.get(password_variable(connection))


# --- running an extraction model (P1-17, #98) -----------------------------------------------

# Rows per Arrow record batch: ClickHouse's block size, so memory does not grow with the result.
BLOCK_SIZE = 65_536
BLOCK_BYTES = 16 * 1024 * 1024  # ClickHouse's preferred block size in bytes, for wide rows
RECEIVE_MARGIN = 30  # seconds the run's receive timeout exceeds its time limit
# Every setting that decides what happens when a limit is reached: always `throw`, never a
# partial result (`break`, `any`).
OVERFLOW_MODES = (
    "distinct_overflow_mode",
    "group_by_overflow_mode",
    "join_overflow_mode",
    "read_overflow_mode",
    "read_overflow_mode_leaf",
    "result_overflow_mode",
    "set_overflow_mode",
    "sort_overflow_mode",
    "timeout_overflow_mode",
    "timeout_overflow_mode_leaf",
    "transfer_overflow_mode",
)
# A setting the user's profile does not let the query change: READONLY (164),
# SETTING_CONSTRAINT_VIOLATION (452).
_SETTING_REFUSED = frozenset({164, 452})
ROW = "__row"  # helper column: the row's position in the stream (0-based)
INVALID = "__invalid"  # helper column: the first column whose text is not UTF-8, if any
_TEXT_FAMILIES = frozenset({"String", "FixedString"})
_AS_TEXT = frozenset({"Int128", "UInt128", "Enum8", "Enum16", "FixedString"})


def _quoted(name: str) -> str:
    """A ClickHouse identifier in backticks (backslash and backtick escaped)."""
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


def _base(clickhouse: str) -> str:
    """The family of a column type, without `Nullable`/`LowCardinality` wrappers."""
    parsed = _parse_type(clickhouse)
    while parsed.name in {"Nullable", "LowCardinality"} and parsed.arguments:
        inner = parsed.arguments[0]
        if not isinstance(inner, _Type):
            break
        parsed = inner
    return parsed.name


def helper_names(columns: tuple[Column, ...]) -> tuple[str, str]:
    """Names for the two helper columns that no output column uses (compared without case)."""
    taken = {column.name.lower() for column in columns}
    row, invalid = ROW, INVALID
    while row.lower() in taken:
        row += "_"
    while invalid.lower() in taken or invalid.lower() == row.lower():
        invalid += "_"
    return row, invalid


def extraction_query(text: str, columns: tuple[Column, ...]) -> str:
    """The query sent to ClickHouse: the model's SQL unchanged, as a subquery, under a projection
    that only makes each value travel exactly as Arrow.

    - `Int128`, `UInt128`, `Enum8`/`Enum16` and `FixedString` become text (Arrow has no exact
      type for them; DuckDB casts the text back exactly), `DateTime` becomes `DateTime64(0)`
      with its zone (Arrow would carry bare seconds).
    - Text that is not valid UTF-8 is never sent: it becomes NULL, and the helper column names
      the first such column of the row; the run then fails (`UNSUPPORTED_SOURCE_VALUE`).
    - A helper column gives each row's position in the stream.
    Source columns are qualified (`__source`.`name`), so no projected alias can shadow them.
    """
    row, invalid = helper_names(columns)
    selected: list[str] = []
    checks: list[str] = []
    for column in columns:
        source = f"`__source`.{_quoted(column.name)}"
        family = _base(column.clickhouse)
        value = f"toString({source})" if family in _AS_TEXT else source
        if family == "DateTime":
            value = f"toDateTime64({source}, 0)"
        if family in _TEXT_FAMILIES:
            checks.append(f"ifNull(NOT isValidUTF8({value}), 0), {_literal(column.name)}")
            value = f"if(isValidUTF8({value}), {value}, NULL)"
        selected.append(f"{value} AS {_quoted(column.name)}")
    flagged = f"multiIf({', '.join(checks)}, NULL)" if checks else "CAST(NULL AS Nullable(String))"
    selected.append(f"rowNumberInAllBlocks() AS {_quoted(row)}")
    selected.append(f"{flagged} AS {_quoted(invalid)}")
    return f"SELECT {', '.join(selected)}\nFROM (\n{text}\n) AS `__source`"


def _literal(text: str) -> str:
    """A ClickHouse string literal (backslash and quote escaped)."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def load_columns(columns: tuple[Column, ...]) -> list[tuple[str, str]]:
    """The DuckDB columns of the extraction stream: each output column with its mapped type,
    then the two helper columns."""
    row, invalid = helper_names(columns)
    return [(column.name, column.mapped.duckdb) for column in columns] + [
        (row, "UBIGINT"),
        (invalid, "VARCHAR"),
    ]


# The lowest instant `$since` can take for each family, as a naive local date and time.
_LOWEST = {
    "Date": dt.datetime(1970, 1, 1),
    "Date32": dt.datetime(1900, 1, 1),
    "DateTime": dt.datetime(1970, 1, 1),
    "DateTime64": dt.datetime(1900, 1, 1),
}


def date_zone(columns: tuple[Column, ...], time_column: str | None) -> str | None:
    """The zone ClickHouse uses to compare a `{since:Date}` or `{since:Date32}` value with the
    time column: a `DateTime` column's own zone (midnight there, measured on 26.9); None (the
    server's) for a column without one or a `Date` column."""
    if time_column is None:
        return None
    for column in columns:
        if column.name.lower() == time_column.lower():
            return column.mapped.zone
    return None


def _since_zone(since: SinceType, server: str, dates: str | None) -> ZoneInfo:
    """The zone of a `{since:…}` value: a date is midnight in `dates` (the time column's zone,
    see `date_zone`), a time is in the zone the type declares, else the server's."""
    if since.family in {"Date", "Date32"}:
        return ZoneInfo(dates or server)
    return ZoneInfo(since.zone or server)


def since_bound(
    since: SinceType, text: str, server: str, dates: str | None = None
) -> tuple[str, str]:
    """The boundary that `{since:…}` = `text` puts on the rows, for DuckDB: the instant (ISO 8601,
    in UTC) and the date, `text` being in the parameter's zone (the server's when it declares
    none). A date is midnight. ClickHouse reads an ambiguous local time as the earlier instant
    (measured on 26.9), as `fold=0` does here.

    A run's own checks compare with this value, not with the stored watermark: `since_value`
    truncates to the declared type, so rows between the two are read again (#98)."""
    local = dt.datetime.fromisoformat(text)
    instant = local.replace(tzinfo=_since_zone(since, server, dates)).astimezone(dt.UTC)
    return instant.isoformat(), local.date().isoformat()


def since_value(
    since: SinceType,
    watermark: str | None,
    lookback: str | None,
    server: str,
    dates: str | None = None,
) -> str:
    """`{since:…}` for an incremental run, as ClickHouse reads a parameter of type `since`.

    `watermark` is the model's previous watermark as stored (DuckDB text: `YYYY-MM-DD` for a
    `Date` time column, an ISO instant with offset for a `DateTime` one; None on a first run or
    a full refresh, which bind the lowest value of the type). `lookback` is subtracted from the
    instant; the result is written in the parameter's zone (the server's when it declares none)
    or, for `Date`/`Date32`, in `dates`, the zone ClickHouse compares a date in (`date_zone`):
    a date for `Date`/`Date32`, seconds for `DateTime`, `p` fraction digits for
    `DateTime64(p)`. Every step truncates, never rounds up, so no row newer than the watermark
    minus lookback is skipped. A value below the type's range is its lowest value.
    """
    if watermark is None or watermark == "-infinity":
        return since.lowest()
    if watermark == "infinity":
        raise SourceError(
            "UNSUPPORTED_SOURCE_VALUE",
            "the stored watermark is infinity; run with --full-refresh to reload the model.",
        )
    zone = _since_zone(since, server, dates)
    back = dt.timedelta(microseconds=lookback_microseconds(lookback))
    if "T" not in watermark and " " not in watermark:
        # A date: midnight of that day in the parameter's zone.
        moment = dt.datetime.combine(dt.date.fromisoformat(watermark), dt.time(), zone)
    else:
        moment = dt.datetime.fromisoformat(watermark).astimezone(zone)
    # The lookback is subtracted from the instant (UTC), never from wall-clock time, so a
    # daylight-saving change inside it cannot shorten it.
    local = (moment.astimezone(dt.UTC) - back).astimezone(zone).replace(tzinfo=None)
    if local < _LOWEST[since.family]:
        return since.lowest()
    if since.family in {"Date", "Date32"}:
        return local.date().isoformat()
    text = local.strftime("%Y-%m-%d %H:%M:%S")
    if since.family == "DateTime64" and since.precision:
        text += "." + f"{local.microsecond:06d}"[: since.precision].ljust(since.precision, "0")
    return text
