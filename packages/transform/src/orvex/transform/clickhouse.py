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
reader must not be able to lift it (decision #114). ClickHouse does not apply that limit to
`Memory`, `Log` and `TinyLog` tables, nor inside views whose own definition sets it: those reads
are not limited (documented in `contracts/model-config/v1`).

`clickhouse-connect` is ClickHouse, Inc.'s official client (Apache-2.0, ADR-0002).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError, OperationalError
from orvex.contracts.model_config.v1 import ModelError
from orvex.contracts.project.v1 import ClickhouseConnection
from orvex.transform.models import Model, problem

SINCE = "since"
CONNECT_TIMEOUT = 10  # seconds
QUERY_TIMEOUT = 30  # seconds, for validation queries (EXPLAIN, DESCRIBE)
# ClickHouse evaluates scalar subqueries while it describes a query: reading is capped, and a
# query that would read rows fails (TOO_MANY_ROWS) after at most one block (not for Memory, Log
# or TinyLog tables, which ClickHouse does not limit).
VALIDATION_LIMITS: tuple[tuple[str, object], ...] = (
    ("max_execution_time", QUERY_TIMEOUT),
    ("max_rows_to_read", 1),
    ("read_overflow_mode", "throw"),
)
_TOO_MANY_ROWS = 158
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
    """A read-only client of a ClickHouse connection: every query the engine sends carries
    `readonly = 1` (unless the user's profile already enforces it) and a time limit; connect and
    receive timeouts are explicit, and no session state is kept. On connecting, the client
    itself reads the server's version, time zone and settings (system tables, no user data)."""

    def __init__(self, name: str, connection: ClickhouseConnection, password: str) -> None:
        self.name = name
        self._password = password
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
        self._fixed_read_only = readonly is not None and bool(readonly.readonly)
        if readonly is not None and self._fixed_read_only and str(readonly.value) != "1":
            self._client.close()
            raise SourceError(
                "SOURCE_NOT_READ_ONLY",
                f"ClickHouse connection {name}: the user's profile fixes readonly = "
                f"{readonly.value}, so its queries cannot be made read-only; use a user whose "
                "profile allows readonly = 1 (or fixes it to 1).",
            )
        # Decision #114 (option C): the reader must accept the validation limits, and SQL run
        # by that reader must not be able to lift them, so ClickHouse itself stops any read (a
        # view, a subquery) after the first block. Both are asked of the server once: a query
        # with the limits must run, and one that lifts them must be refused.
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
        try:
            self._client.query("SELECT 1 SETTINGS max_rows_to_read = 0", settings=self.settings())
        except ClickHouseError as error:
            if isinstance(error, OperationalError):
                self._client.close()
                raise self._error(error, "SOURCE_CONNECTION_FAILED") from None
        else:
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

    def settings(self) -> dict[str, object]:
        """Settings for each validation query: the limits, then `readonly = 1` (not sent when the
        profile already fixes it). They are sent with the query, never kept in a session;
        ClickHouse checks them together before applying them, so the limits are accepted
        although `readonly = 1` forbids later changes."""
        settings: dict[str, object] = dict(VALIDATION_LIMITS)
        if not self._fixed_read_only:
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

    def close(self) -> None:
        self._client.close()


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
