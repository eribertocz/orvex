"""DuckLake lake with a local DuckDB-file catalog (contract `store` v1, P1-01).

Writes run inside DuckDB in explicit transactions; each read is pinned to a snapshot by adding
`AT (VERSION => n)` to every lake table it references (rewritten with DuckDB's own parser) and is
read once, as an Arrow record-batch stream (ADR-0025). No row passes through Python.
"""

from __future__ import annotations

import collections
import contextlib
import datetime as dt
import glob as globlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import weakref
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import duckdb
import pyarrow as pa
from orvex.contracts.store.v1 import (
    CommitResult,
    FileSourceAttachment,
    LakeLocation,
    LocalLakeLocation,
    MaintenanceReport,
    Parameter,
    PostgresLakeLocation,
    PostgresSourceAttachment,
    ReadRequest,
    Snapshot,
    SourceAttachment,
    SqliteSourceAttachment,
    StatementRequest,
)
from orvex.store.errors import StoreError, register_secret

LAKE = "lake"
MAX_TIMEOUT_MS = 3_600_000
METADATA_PREFIX = "__ducklake_metadata"
METADATA = f"{METADATA_PREFIX}_{LAKE}"
# Any password key (password=, sslpassword=, ?password=) and any URI form (user:pw@host) is
# refused: the password may only come from `password_env`.
_INLINE_SECRET = re.compile(r"password|://", re.IGNORECASE)
_SCHEMA_NAME = re.compile(r"[a-z_][a-z0-9_]*")
# Functions that run SQL or open connections outside the lake; never allowed in a statement.
_EXTERNAL_SQL_FUNCTIONS = frozenset(
    {
        "postgres_query",
        "postgres_execute",
        "postgres_scan",
        "postgres_scan_pushdown",
        "postgres_attach",
        "sqlite_query",
        "sqlite_attach",
        "sqlite_scan",
        "query",
        "query_table",
    }
)
_SUSPICIOUS = re.compile(r"[./\\*?:]")
POSTGRES_CONNECT_TIMEOUT_S = 10
RESERVED_SOURCE_NAMES = frozenset({LAKE, "main", "memory", "system", "temp"})
_GLOB = re.compile(r"[*?\[]")
EXTENSIONS = ("icu", "ducklake")
WRITE_STATEMENTS = frozenset(
    {
        duckdb.StatementType.CREATE,
        duckdb.StatementType.INSERT,
        duckdb.StatementType.UPDATE,
        duckdb.StatementType.DELETE,
        duckdb.StatementType.DROP,
        duckdb.StatementType.ALTER,
        duckdb.StatementType.MERGE_INTO,
    }
)


INTEGER_RANGES = {
    "TINYINT": (-(2**7), 2**7 - 1),
    "SMALLINT": (-(2**15), 2**15 - 1),
    "INTEGER": (-(2**31), 2**31 - 1),
    "BIGINT": (-(2**63), 2**63 - 1),
    "HUGEINT": (-(2**127), 2**127 - 1),
    "UTINYINT": (0, 2**8 - 1),
    "USMALLINT": (0, 2**16 - 1),
    "UINTEGER": (0, 2**32 - 1),
    "UBIGINT": (0, 2**64 - 1),
    "UHUGEINT": (0, 2**128 - 1),
}


@dataclass(frozen=True, slots=True)
class Limits:
    """Resource limits of a lake; every field is required (law 7).

    `max_concurrent` statements (reads in progress and open transactions) run at once; up to
    `queue_size` more wait in arrival order; `default_timeout_ms` applies when a request sets no
    `timeout_ms`.
    """

    memory_limit: str | None
    threads: int | None
    max_concurrent: int | None = None
    queue_size: int | None = None
    default_timeout_ms: int | None = None


class Admission:
    """At most `capacity` holders at once, a bounded first-come first-served queue behind them."""

    def __init__(self, capacity: int, queue_size: int) -> None:
        self._capacity = capacity
        self._queue_size = queue_size
        self._running = 0
        self._waiting: collections.deque[object] = collections.deque()
        self._condition = threading.Condition()

    def acquire(self, deadline: float) -> None:
        """Wait for a slot until `deadline` (time.monotonic); STORE_BUSY or STORE_TIMEOUT."""
        with self._condition:
            if self._running < self._capacity and not self._waiting:
                self._running += 1
                return
            if len(self._waiting) >= self._queue_size:
                raise StoreError(
                    "STORE_BUSY",
                    "The lake is at capacity and its queue is full; try again shortly.",
                    {"running": str(self._running), "queued": str(len(self._waiting))},
                )
            ticket = object()
            self._waiting.append(ticket)
            try:
                while not (self._waiting[0] is ticket and self._running < self._capacity):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StoreError(
                            "STORE_TIMEOUT",
                            "The request timed out while waiting for a free slot.",
                            {},
                        )
                    self._condition.wait(remaining)
                self._running += 1
            finally:
                self._waiting.remove(ticket)
                self._condition.notify_all()

    def release(self) -> None:
        with self._condition:
            self._running -= 1
            self._condition.notify_all()


INTERRUPT_REPEAT_SECONDS = 0.05


class Guard:
    """Interrupts a DuckDB cursor at a deadline or on request, and remembers why."""

    def __init__(self, cursor: duckdb.DuckDBPyConnection, deadline: float) -> None:
        self._cursor = cursor
        self.reason: str | None = None
        self.deadline = deadline
        # Called after an interruption, for an owner that must also release what is idle.
        self.on_fire: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._repeat: threading.Thread | None = None
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self) -> None:
        self._interrupt("STORE_TIMEOUT")

    def cancel(self) -> None:
        self._interrupt("STORE_CANCELLED")

    def _interrupt(self, reason: str) -> None:
        with self._lock:
            if self.reason is not None:
                return
            self.reason = reason
        if self.on_fire is not None:
            self.on_fire()
        # An interrupt sent while the cursor is between two pieces of work can be lost, so it is
        # repeated until the owner stops the guard.
        with self._lock:
            if self._stopped.is_set():
                return
            self._repeat = threading.Thread(target=self._keep_interrupting, daemon=True)
            self._repeat.start()

    def _keep_interrupting(self) -> None:
        while True:
            # A statement that already ended has a closed cursor: there is nothing to stop.
            with contextlib.suppress(duckdb.Error):
                self._cursor.interrupt()
            if self._stopped.wait(INTERRUPT_REPEAT_SECONDS):
                return

    def stop(self) -> None:
        """No interrupt reaches the cursor once this returns."""
        with self._lock:
            self._stopped.set()
            repeat = self._repeat
        self._timer.cancel()
        if repeat is not None:
            repeat.join()

    def error(self, cause: Exception | None = None) -> StoreError:
        """The store error for an interruption (or `cause` when it was not interrupted)."""
        if self.reason == "STORE_TIMEOUT" or (
            self.reason is None and time.monotonic() >= self.deadline
        ):
            return StoreError("STORE_TIMEOUT", "The statement exceeded its time limit.", {})
        if self.reason == "STORE_CANCELLED":
            return StoreError("STORE_CANCELLED", "The statement was cancelled.", {})
        return StoreError(
            "STORE_QUERY_FAILED", "DuckDB could not run the statement.", {"duckdb": str(cause)}
        )


@dataclass(slots=True)
class ReadHandle:
    """A running read: its Arrow stream and a way to cancel it from another thread."""

    reader: pa.RecordBatchReader
    _guard: Guard

    def cancel(self) -> None:
        self._guard.cancel()


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _postgres_password(location: PostgresLakeLocation) -> str:
    """Refuse an unsafe PostgreSQL location, then read and register its password."""
    if _INLINE_SECRET.search(location.connection):
        raise StoreError(
            "STORE_SECRET_INLINE",
            "The connection must be key=value pairs without any password; use password_env.",
            {},
        )
    if location.metadata_schema is not None and not _SCHEMA_NAME.fullmatch(
        location.metadata_schema
    ):
        raise StoreError(
            "STORE_STATEMENT_INVALID",
            "metadata_schema must match [a-z_][a-z0-9_]*.",
            {"metadata_schema": location.metadata_schema},
        )
    password = os.environ.get(location.password_env)
    if not password:
        raise StoreError(
            "STORE_SECRET_MISSING",
            f"The environment variable {location.password_env} is not set.",
            {"variable": location.password_env},
        )
    register_secret(password)
    return password


def _guarded(sql: str, table_functions: frozenset[str]) -> None:
    """Refuse SQL that names the catalog database or calls a table function other than generators.

    Table functions read files, run SQL or open connections (`read_text`, `query`,
    `postgres_execute`, …); only `range`, `generate_series` and `unnest` are allowed. Comments
    between a name and its `(` are skipped. The catalog database (in PostgreSQL the whole
    database, other lakes included) is only ever touched by DuckLake itself. The connection is
    also sandboxed to the lake's folders (`Lake._sandbox`), so these checks are a first layer.
    """
    if METADATA_PREFIX in sql.lower():
        raise StoreError(
            "STORE_STATEMENT_INVALID",
            "Statements may not reference the lake's catalog database.",
            {},
        )
    tokens = [token for token in duckdb.tokenize(sql) if token[1] != duckdb.token_type.comment]
    positions = [position for position, _ in duckdb.tokenize(sql)] + [len(sql)]
    _refuse_file_names(sql, tokens, positions)
    for index, (position, kind) in enumerate(tokens):
        if kind not in {duckdb.token_type.identifier, duckdb.token_type.keyword}:
            continue
        end = next(later for later in positions if later > position)
        name = sql[position:end].strip().strip('"').lower()
        following = tokens[index + 1][0] if index + 1 < len(tokens) else len(sql)
        if (
            sql[following : following + 1] == "("
            and (name in table_functions or name in _EXTERNAL_SQL_FUNCTIONS)
            and name not in READ_TABLE_FUNCTIONS
        ):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "Only the table functions range, generate_series and unnest are allowed.",
                {"function": name},
            )


def _refuse_file_names(
    sql: str, tokens: list[tuple[int, duckdb.token_type]], positions: list[int]
) -> None:
    """Refuse a quoted string or quoted name that names an existing file or matches files.

    DuckDB reads such a string as a file in many places (`FROM '…'`, `MERGE … USING '…'`,
    `PIVOT '…'`, `SUMMARIZE '…'`, …); a model reaches a file only through its source name. The
    file sandbox, which allows exactly the declared files, is the boundary behind this check.
    """
    for position, kind in tokens:
        end = next(later for later in positions if later > position)
        text = sql[position:end].strip()
        quoted = kind == duckdb.token_type.string_const or text.startswith('"')
        if quoted and _names_a_file(text):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A file cannot be named in a statement; declare it as a source and use its name.",
                {},
            )


def _names_a_file(token: str) -> bool:
    quote = token[-1:]
    start = token.find(quote)
    content = token[start + 1 : -1].replace(quote * 2, quote) if start >= 0 else token
    candidates = {content, content.removeprefix("file://")}
    for candidate in candidates:
        if not candidate:
            continue
        path = os.path.expanduser(candidate)
        if os.path.isfile(path):
            return True
        looks_like_path = any(mark in candidate for mark in ("/", "\\", "."))
        if (
            looks_like_path
            and _GLOB.search(candidate)
            and any(os.path.isfile(match) for match in globlib.iglob(path))
        ):
            return True
    return False


def _single_statement(sql: str, table_functions: frozenset[str]) -> duckdb.Statement:
    _guarded(sql, table_functions)
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as error:
        raise StoreError(
            "STORE_QUERY_FAILED", "The SQL could not be parsed.", {"duckdb": str(error)}
        ) from error
    if len(statements) != 1:
        raise StoreError(
            "STORE_STATEMENT_INVALID",
            "Exactly one SQL statement is required.",
            {"statements": str(len(statements))},
        )
    return statements[0]


_INTEGER = re.compile(r"-?[0-9]+")
_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MOMENT = r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?"
_TIMESTAMP = re.compile(_MOMENT)
_TIMESTAMPTZ = re.compile(_MOMENT + r"(Z|[+-][0-9]{2}:[0-9]{2})")
_DOUBLE = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")


def _invalid(parameter: Parameter, reason: str) -> StoreError:
    return StoreError(
        "STORE_PARAMETER_INVALID",
        f"Parameter {parameter.name} is not a valid {parameter.type.root}.",
        {"parameter": parameter.name, "reason": reason},
    )


def _converted(parameter: Parameter) -> str | None:
    """The parameter's text after a strict check against its type.

    The text is bound as is and converted by `CAST(... AS <type>)` inside DuckDB (`_typed`), so
    no value passes through a Python float or a time-zone library.
    """
    value = parameter.value
    kind = parameter.type.root
    if value is None:
        return None
    if kind.startswith("DECIMAL"):
        precision, scale = (int(part) for part in kind[8:-1].split(","))
        if not 1 <= precision <= 38 or scale > precision:
            raise _invalid(parameter, "precision must be 1-38 and scale at most the precision")
        if not _DECIMAL.fullmatch(value):
            raise _invalid(parameter, "expected digits with an optional fraction")
        fraction = len(value.partition(".")[2])
        whole = len(value.lstrip("-").partition(".")[0].lstrip("0"))
        if fraction > scale or whole > precision - scale:
            raise _invalid(parameter, f"does not fit DECIMAL({precision},{scale})")
        return value
    if kind in INTEGER_RANGES:
        if not _INTEGER.fullmatch(value):
            raise _invalid(parameter, "expected digits")
        number = int(value, 10)
        low, high = INTEGER_RANGES[kind]
        if not low <= number <= high:
            raise _invalid(parameter, f"out of range for {kind}")
        return value
    if kind == "BOOLEAN":
        if value not in {"true", "false"}:
            raise _invalid(parameter, "expected true or false")
        return value
    if kind == "DOUBLE":
        if not _DOUBLE.fullmatch(value):
            raise _invalid(parameter, "expected a finite decimal number")
        if not math.isfinite(float(value)):
            raise _invalid(parameter, "out of range for DOUBLE")
        return value
    if kind in {"DATE", "TIMESTAMP", "TIMESTAMPTZ"} and value in {"infinity", "-infinity"}:
        return value  # DuckDB's own infinite dates and times (the lowest value is `-infinity`)
    try:
        if kind == "DATE":
            if not _DATE.fullmatch(value):
                raise ValueError("expected YYYY-MM-DD")
            dt.date.fromisoformat(value)
            return value
        if kind == "TIMESTAMP":
            if not _TIMESTAMP.fullmatch(value):
                raise ValueError("expected YYYY-MM-DDTHH:MM:SS[.ffffff] without a zone")
            dt.datetime.fromisoformat(value)
            return value
        if kind == "TIMESTAMPTZ":
            if not _TIMESTAMPTZ.fullmatch(value):
                raise ValueError("expected YYYY-MM-DDTHH:MM:SS[.ffffff] with Z or an offset")
            dt.datetime.fromisoformat(value)
            return value
    except ValueError as error:
        raise _invalid(parameter, str(error)) from error
    return value


def _bound(statement: duckdb.Statement, parameters: list[Parameter]) -> dict[str, str | None]:
    names = [parameter.name for parameter in parameters]
    expected = set(statement.named_parameters)
    if len(names) != len(set(names)) or set(names) != expected:
        raise StoreError(
            "STORE_PARAMETER_INVALID",
            "The parameters must match the $names used in the SQL exactly once.",
            {
                "missing": ",".join(sorted(expected - set(names))),
                "extra": ",".join(sorted(set(names) - expected)),
            },
        )
    return {parameter.name: _converted(parameter) for parameter in parameters}


def _typed(sql: str, parameters: list[Parameter]) -> str:
    """The SQL with every `$name` wrapped in `CAST($name AS <declared type>)`.

    DuckDB's tokenizer finds the parameters, so text inside strings and comments is untouched.
    The declared type is validated by the contract pattern before it reaches the SQL.
    """
    types = {parameter.name: parameter.type.root for parameter in parameters}
    tokens = duckdb.tokenize(sql)
    edits: list[tuple[int, int, str]] = []
    for index, (position, kind) in enumerate(tokens[:-1]):
        following, following_kind = tokens[index + 1]
        if (
            kind == duckdb.token_type.operator
            and sql[position] == "$"
            and following == position + 1
            and following_kind == duckdb.token_type.identifier
        ):
            end = tokens[index + 2][0] if index + 2 < len(tokens) else len(sql)
            name = sql[following:end].rstrip()
            if name in types:
                stop = following + len(name)
                edits.append((position, stop, f"CAST(${name} AS {types[name]})"))
    wrapped = {sql[start + 1 : stop] for start, stop, _ in edits}
    if wrapped != set(types):
        raise StoreError(
            "STORE_PARAMETER_INVALID",
            "Every parameter must appear in the SQL as $name.",
            {"unplaced": ",".join(sorted(set(types) - wrapped))},
        )
    for start, stop, replacement in reversed(edits):
        sql = sql[:start] + replacement + sql[stop:]
    return sql


READ_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest"})


class Lake:
    """One open lake. Reads may run concurrently; write transactions run one at a time."""

    def __init__(
        self,
        location: LakeLocation | LocalLakeLocation | PostgresLakeLocation,
        limits: Limits,
        sources: Sequence[AnySource] = (),
    ) -> None:
        if isinstance(location, LakeLocation):
            location = location.root
        # Location checks come first: nothing is opened for a location that must be refused.
        password = (
            _postgres_password(location) if isinstance(location, PostgresLakeLocation) else ""
        )
        if (
            not limits.memory_limit
            or not limits.threads
            or limits.max_concurrent is None
            or limits.max_concurrent < 1
            or limits.queue_size is None
            or limits.queue_size < 0
            or limits.default_timeout_ms is None
            or not 1 <= limits.default_timeout_ms <= MAX_TIMEOUT_MS
        ):
            raise StoreError(
                "STORE_LIMITS_REQUIRED",
                "The lake needs a memory limit, a thread count, a concurrency limit, a queue "
                "size and a default timeout.",
                {},
            )
        attachments: list[FileSourceAttachment | SqliteSourceAttachment | PostgresSourceAttachment]
        attachments = [
            source.root if isinstance(source, SourceAttachment) else source for source in sources
        ]
        # Passwords are read and registered before anything is opened, so no error can show one.
        passwords = {
            source.name: _source_password(source)
            for source in attachments
            if isinstance(source, PostgresSourceAttachment)
        }
        names = [source.name for source in attachments]
        reserved = sorted(set(names) & RESERVED_SOURCE_NAMES)
        if reserved:
            # The contract excludes these names too; generated models do not check `not`.
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Source name {reserved[0]} is reserved.",
                {"source": reserved[0]},
            )
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Source {duplicates[0]} is declared more than once.",
                {"source": duplicates[0]},
            )
        self._limits = limits
        self._admission = Admission(limits.max_concurrent, limits.queue_size)
        try:
            self._connection = duckdb.connect(
                config={
                    "memory_limit": limits.memory_limit,
                    "threads": int(limits.threads),
                    # Secrets saved on this machine are never loaded into a lake.
                    "allow_persistent_secrets": False,
                }
            )
        except (duckdb.Error, ValueError) as error:
            raise StoreError(
                "STORE_LIMITS_REQUIRED",
                "The memory limit or the thread count is not valid.",
                {"duckdb": str(error)},
            ) from error
        self._write_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        # DuckDB spills to this folder when a query exceeds the memory limit.
        self._spill = tempfile.mkdtemp(prefix="lake-spill-")
        self._local = threading.local()
        try:
            self._load_extensions()
            # Catalog tables: `<metadata database>[.<schema>]`; the schema name is validated by
            # the contract pattern ([a-z_][a-z0-9_]*) before it reaches SQL.
            self._metadata = METADATA
            if isinstance(location, PostgresLakeLocation) and location.metadata_schema:
                self._metadata = f"{METADATA}.{location.metadata_schema}"
            if isinstance(location, LocalLakeLocation):
                self._attach_local(Path(location.path))
            else:
                self._attach_postgres(location, password)
            # Nothing may load code or reach data the lake did not ask for after this point.
            self._connection.sql("SET autoinstall_known_extensions = false")
            self._connection.sql("SET autoload_known_extensions = false")
            # (source, schema or "", table, column, label) of every column DuckDB cannot carry
            # exactly; the label is how errors name it.
            self._inexact: set[Inexact] = set()
            readable: list[str] = []
            for source in attachments:
                if isinstance(source, SqliteSourceAttachment):
                    readable += self._attach_sqlite(source)
                elif isinstance(source, PostgresSourceAttachment):
                    self._attach_postgres_source(source, passwords[source.name])
                else:
                    readable += self._attach_source(source)
            # After the sources: their extensions add table functions (sqlite_scan, …) to refuse.
            self._table_functions = self._names_of_table_functions()
            self._sandbox(location, readable)
        except BaseException:
            self._connection.close()
            shutil.rmtree(self._spill, ignore_errors=True)
            raise

    def _names_of_table_functions(self) -> frozenset[str]:
        row = self._one(
            # Names that are also scalar or aggregate functions (repeat, version, histogram, …)
            # are generators or harmless in their table form, and their scalar form is common.
            "SELECT string_agg(DISTINCT lower(function_name), '|') FROM duckdb_functions() "
            "WHERE function_type IN ('table', 'table_macro', 'pragma') "
            "AND lower(function_name) NOT IN (SELECT lower(function_name) FROM duckdb_functions() "
            "WHERE function_type IN ('scalar', 'aggregate', 'macro'))",
            {},
        )
        text = str(row[0]) if row and row[0] is not None else ""
        return frozenset(text.split("|")) - {""}

    def _attach_sqlite(self, source: SqliteSourceAttachment) -> list[str]:
        """Attach a private copy of a SQLite file, read-only.

        The copy (SQLite's backup, consistent even while the file is being written, WAL
        included) is what gets inspected and attached, so a change to the original after the
        lake opens can neither add a view nor an inexact column. Its DECIMAL/NUMERIC columns
        are recorded as inexact.
        """
        path = Path(source.path)
        if not path.is_file():
            raise _source_not_found(source)  # SQLite would create a missing file
        folder = Path(self._spill) / "sources"
        folder.mkdir(exist_ok=True)
        copy = folder / f"{source.name}.sqlite"
        declared = _sqlite_snapshot(source, copy)
        self._load_extension("sqlite")
        self._source_sql(
            source,
            f"ATTACH {_quote(copy.as_posix())} AS {_identifier(source.name)} "
            "(TYPE sqlite, READ_ONLY)",
        )
        for table, column, kind in declared:
            # DuckDB reads every declared type containing DEC or NUM (DECIMAL, NUMERIC,
            # NUMBER, NUM) as floating point, and SQLite stores such values as REAL anyway.
            if re.search(r"DEC|NUM", kind, re.IGNORECASE):
                self._inexact.add(
                    Inexact(source.name, "", table.lower(), column.lower(), f"{table}.{column}")
                )
        return []  # the copy lives in the lake's temporary folder, already allowed

    def _attach_postgres_source(self, source: PostgresSourceAttachment, password: str) -> None:
        """Attach a PostgreSQL database read-only, twice over: DuckDB's READ_ONLY and a
        read-only session. Inexact columns (numeric without precision, money) are recorded."""
        self._load_extension("postgres")
        secret = _identifier(f"source_{source.name}")
        self._source_connect(
            source,
            f"CREATE TEMPORARY SECRET {secret} (TYPE postgres, HOST {_quote(source.host)}, "
            f"PORT {int(source.port)}, DATABASE {_quote(source.database)}, "
            f"USER {_quote(source.user)}, PASSWORD {_quote(password)})",
        )
        connection = (
            f"connect_timeout={POSTGRES_CONNECT_TIMEOUT_S} "
            "options='-c default_transaction_read_only=on'"
        )
        options = ["TYPE postgres", f"SECRET {secret}", "READ_ONLY"]
        if source.schema_:
            options.append(f"SCHEMA {_quote(source.schema_)}")
        self._source_connect(
            source,
            f"ATTACH {_quote(connection)} AS {_identifier(source.name)} ({', '.join(options)})",
        )
        scope = "" if not source.schema_ else f" AND n.nspname = {_quote(source.schema_)}"
        # Every relation DuckDB can scan (tables, views, materialized views, foreign and
        # partitioned tables). Inexact: numeric without precision or above 38 digits (DuckDB
        # reads both as DOUBLE), money, and arrays of either.
        inner = (
            "SELECT n.nspname AS table_schema, c.relname AS table_name, a.attname AS column_name "
            "FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'v', 'm', 'f', 'p') AND a.attnum > 0 "
            "AND NOT a.attisdropped "
            "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' AND ("
            "(a.atttypid = 'numeric'::regtype "
            # typmod - 4 = precision << 16 | scale (11 bits; PostgreSQL 15+ allows a negative
            # scale or one above the precision, which DuckDB cannot represent either).
            "AND (a.atttypmod < 0 OR ((a.atttypmod - 4) >> 16) > 38 "
            "OR ((a.atttypmod - 4) & 2047) > ((a.atttypmod - 4) >> 16))) "
            "OR a.atttypid IN ('money'::regtype, '_money'::regtype, '_numeric'::regtype))" + scope
        )
        cursor = self._connection.cursor()
        try:
            # One metadata row: the inexact columns as a list (no row data reaches Python).
            row = cursor.execute(
                "SELECT list([table_schema, table_name, column_name]) "
                "FROM postgres_query($name, $sql)",
                {"name": source.name, "sql": inner},
            ).fetchone()
        except duckdb.Error as error:
            raise StoreError(
                "STORE_CONNECTION_FAILED",
                f"Source {source.name} could not be inspected.",
                {"source": source.name, "duckdb": str(error)},
            ) from None
        finally:
            cursor.close()
        found = row[0] if row and isinstance(row[0], list) else []
        for schema, table, column in found:
            label = f"{table}.{column}" if source.schema_ else f"{schema}.{table}.{column}"
            self._inexact.add(
                Inexact(
                    source.name, str(schema).lower(), str(table).lower(), str(column).lower(), label
                )
            )

    def _source_connect(self, source: PostgresSourceAttachment, sql: str) -> None:
        try:
            self._connection.sql(sql)
        except duckdb.Error as error:
            # `from None`: the DuckDB error is not chained; the message is redacted.
            raise StoreError(
                "STORE_CONNECTION_FAILED",
                f"Source {source.name} could not be reached or refused the credentials.",
                {"source": source.name, "duckdb": str(error)},
            ) from None

    def _attach_source(self, source: FileSourceAttachment) -> list[str]:
        """Attach one file source read-only; returns the files the sandbox must allow.

        For a glob, exactly the files it matches now: a file added later to the same folder,
        or any other file there, stays unreadable.
        """
        path = source.path
        if source.type == "duckdb":
            if not Path(path).is_file():
                raise _source_not_found(source)
            self._source_sql(
                source, f"ATTACH {_quote(path)} AS {_identifier(source.name)} (READ_ONLY)"
            )
            macros = self._one(
                "SELECT (SELECT count(*) FROM duckdb_functions() WHERE database_name = $name "
                "AND NOT internal AND function_type IN ('macro', 'table_macro')) "
                "+ (SELECT count(*) FROM duckdb_views() WHERE database_name = $name "
                "AND NOT internal)",
                {"name": source.name},
            )
            if macros and int(str(macros[0])):
                # A view or macro runs with the lake's connection: it could read lake tables or
                # the lake's catalog database, which a source must never reach.
                raise StoreError(
                    "STORE_SOURCE_INVALID",
                    f"Source {source.name} defines views or macros, which a lake cannot attach.",
                    {"source": source.name},
                )
            return [Path(path).as_posix()]
        matched = self._one("SELECT list(file ORDER BY file) FROM glob($path)", {"path": path})
        matched_files = matched[0] if matched else None
        if not isinstance(matched_files, list) or not matched_files:
            raise _source_not_found(source)
        # The view lives in its own small database file, attached read-only: nothing can be
        # created in, or dropped from, a source catalog.
        folder = Path(self._spill) / "sources"
        folder.mkdir(exist_ok=True)
        catalog = (folder / f"{source.name}.duckdb").as_posix()
        reader = "read_parquet" if source.type == "parquet" else "read_csv"
        builder = duckdb.connect(catalog)
        try:
            # The files matched now, listed one by one: the sandbox allows exactly these.
            listed = ", ".join(_quote(str(file)) for file in matched_files)
            builder.sql(
                f"CREATE VIEW data AS SELECT * FROM {reader}([{listed}], union_by_name = true)"
            )
        except duckdb.Error as error:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Source {source.name} cannot be read as {source.type}.",
                {"source": source.name, "duckdb": str(error)},
            ) from None
        finally:
            builder.close()
        self._source_sql(
            source, f"ATTACH {_quote(catalog)} AS {_identifier(source.name)} (READ_ONLY)"
        )
        return [str(file) for file in matched_files]

    def _source_sql(self, source: FileSourceAttachment | SqliteSourceAttachment, sql: str) -> None:
        try:
            self._connection.sql(sql)
        except duckdb.Error as error:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Source {source.name} could not be attached.",
                {"source": source.name, "duckdb": str(error)},
            ) from None

    def _sandbox(
        self,
        location: LocalLakeLocation | PostgresLakeLocation,
        readable: Sequence[str] = (),
    ) -> None:
        """Limit file access to the lake's own folders and the declared source files, for good
        (DuckDB cannot undo it)."""
        folders = [self._spill]
        if isinstance(location, LocalLakeLocation):
            folders.append(Path(location.path).resolve().as_posix())
        elif "://" not in location.data_path:
            folders.append(Path(location.data_path).resolve().as_posix())
        files = list(readable)
        listed = ", ".join(_quote(folder.rstrip("/") + "/") for folder in folders)
        self._connection.sql(f"SET temp_directory = {_quote(self._spill)}")
        self._connection.sql(f"SET allowed_directories = [{listed}]")
        if files:
            self._connection.sql(f"SET allowed_paths = [{', '.join(map(_quote, files))}]")
        self._connection.sql("SET enable_external_access = false")

    def _load_extensions(self) -> None:
        for extension in EXTENSIONS:
            self._load_extension(extension)

    def _attach_local(self, folder: Path) -> None:
        (folder / "data").mkdir(parents=True, exist_ok=True)
        catalog = (folder / "catalog.ducklake").as_posix()
        data = (folder / "data").as_posix() + "/"
        # Paths come from the project configuration, not from query input; quoted as literals.
        self._connection.sql(
            f"ATTACH {_quote('ducklake:' + catalog)} AS {LAKE} (DATA_PATH {_quote(data)})"
        )

    def _attach_postgres(self, location: PostgresLakeLocation, password: str) -> None:
        """Attach a lake whose catalog is in PostgreSQL; the password comes from the environment."""
        if "://" not in location.data_path:
            Path(location.data_path).mkdir(parents=True, exist_ok=True)
        self._load_extension("postgres")
        try:
            # A temporary default secret supplies the password to every PostgreSQL connection of
            # this database; DuckDB never shows secret values (duckdb_secrets() redacts them).
            self._connection.sql(
                f"CREATE TEMPORARY SECRET (TYPE postgres, PASSWORD {_quote(password)})"
            )
            options = [f"DATA_PATH {_quote(location.data_path)}"]
            if location.metadata_schema:
                options.append(f"METADATA_SCHEMA {_quote(location.metadata_schema)}")
            self._connection.sql(
                f"ATTACH {_quote('ducklake:postgres:' + location.connection)} AS {LAKE} "
                f"({', '.join(options)})"
            )
        except duckdb.Error as error:
            # `from None`: the DuckDB exception is not chained, so a logged traceback cannot show
            # anything the redacted message does not.
            raise StoreError(
                "STORE_CATALOG_UNAVAILABLE",
                "The PostgreSQL catalog could not be reached or refused the credentials.",
                {"duckdb": str(error)},
            ) from None

    def _load_extension(self, extension: str) -> None:
        try:
            self._connection.install_extension(extension)
            self._connection.load_extension(extension)
        except duckdb.Error as error:
            raise StoreError(
                "STORE_EXTENSION_UNAVAILABLE",
                f"The DuckDB extension {extension} could not be installed or loaded.",
                {"extension": extension, "duckdb": str(error)},
            ) from error

    def close(self) -> None:
        self._connection.close()
        shutil.rmtree(self._spill, ignore_errors=True)

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        cursor = self._connection.cursor()
        cursor.sql(f"USE {LAKE}")
        # TIMESTAMPTZ values reach Arrow as instants in UTC whatever the machine's zone;
        # presenting them in a local zone is a later, explicit step.
        cursor.sql("SET TimeZone = 'UTC'")
        # A name in FROM must never resolve to a Python object in scope.
        cursor.sql("SET python_enable_replacements = false")
        return cursor

    def _one(self, sql: str, parameters: dict[str, object]) -> tuple[object, ...] | None:
        """One metadata row on a short-lived cursor, so no unfinished result is left behind."""
        cursor = self._cursor()
        try:
            row: tuple[object, ...] | None = cursor.execute(sql, parameters).fetchone()
        finally:
            cursor.close()
        return row

    def _snapshot(self, snapshot_id: int | None) -> Snapshot | None:
        where = "" if snapshot_id is None else "WHERE snapshot_id = $id"
        parameters = {} if snapshot_id is None else {"id": snapshot_id}
        row = self._one(
            "SELECT snapshot_id, epoch_us(snapshot_time) FROM ducklake_snapshots($lake) "
            f"{where} ORDER BY snapshot_id DESC LIMIT 1",
            {"lake": LAKE, **parameters},
        )
        return None if row is None else _snapshot_of(row)

    def _oldest_snapshot(self) -> Snapshot | None:
        row = self._one(
            "SELECT snapshot_id, epoch_us(snapshot_time) FROM ducklake_snapshots($lake) "
            "ORDER BY snapshot_id LIMIT 1",
            {"lake": LAKE},
        )
        return None if row is None else _snapshot_of(row)

    def _missing_snapshot(self, snapshot_id: int) -> StoreError:
        """Why a snapshot cannot be read: expired by maintenance (ids are never reused and every
        id up to the current one was committed, so a missing one among them expired), or never
        existed."""
        oldest = self._oldest_snapshot()
        current = self._snapshot(None)
        if oldest is not None and current is not None and 0 <= snapshot_id <= current.id:
            return StoreError(
                "STORE_SNAPSHOT_EXPIRED",
                f"Snapshot {snapshot_id} has expired: the oldest snapshot kept is {oldest.id} "
                f"({oldest.committed_at.isoformat()}); the lake's snapshot retention decides how "
                "long snapshots stay readable.",
                {"snapshot": str(snapshot_id), "oldest": str(oldest.id)},
            )
        return StoreError(
            "STORE_SNAPSHOT_NOT_FOUND",
            f"Snapshot {snapshot_id} does not exist in the lake.",
            {"snapshot": str(snapshot_id)},
        )

    def maintain(
        self, expire_before: dt.datetime, orphans_before: dt.datetime
    ) -> MaintenanceReport:
        """Expire the snapshots committed before `expire_before`, merge small files, rewrite
        files with many deletes, and delete the files no remaining snapshot uses, and orphaned
        files older than `orphans_before` (a writer may still be creating a newer one).

        DuckLake's own `CHECKPOINT` order, step by step so each one is counted: flush inlined
        data, expire, merge, rewrite, expire again (the snapshot merging replaced, when it is
        older than the cutoff), clean up, delete orphans. The current snapshot is never expired.
        Runs alone: one maintenance at a time, after every read and write of this lake ends
        (every admission slot, then the writer lock). Counts are read in SQL; no row reaches
        Python.
        """
        for instant in (expire_before, orphans_before):
            if instant.tzinfo is None:
                raise StoreError(
                    "STORE_PARAMETER_INVALID", "Maintenance cutoffs must be aware instants.", {}
                )
        cutoff = expire_before.astimezone(dt.UTC).isoformat()
        orphans = orphans_before.astimezone(dt.UTC).isoformat()
        deadline = time.monotonic() + self._timeout_ms(None) / 1000
        # One maintenance at a time (two taking part of the slots each would wait for each
        # other), then every admission slot, then the writer lock: no read or write of this lake
        # runs during maintenance (a read pinned to a snapshot being expired would lose its files).
        if not self._maintenance_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise StoreError("STORE_TIMEOUT", "Another maintenance of this lake is running.", {})
        taken = 0
        try:
            for _ in range(int(self._limits.max_concurrent or 1)):
                self._admission.acquire(deadline)
                taken += 1
            if not self._write_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise StoreError(
                    "STORE_TIMEOUT", "Maintenance waited too long for the writer ahead.", {}
                )
            try:
                cursor = self._cursor()
                try:
                    report = self._maintain(cursor, cutoff, orphans)
                finally:
                    cursor.close()
            finally:
                self._write_lock.release()
        finally:
            for _ in range(taken):
                self._admission.release()
            self._maintenance_lock.release()
        return report

    def _maintain(
        self, cursor: duckdb.DuckDBPyConnection, cutoff: str, orphans: str
    ) -> MaintenanceReport:
        lake: dict[str, object] = {"lake": LAKE}
        expire = (
            "SELECT count(*) FROM ducklake_expire_snapshots("
            "$lake, older_than => CAST($cutoff AS TIMESTAMPTZ))"
        )
        compact = (
            "SELECT coalesce(sum(files_processed), 0) || ',' || coalesce(sum(files_created), 0) "
            "FROM {function}($lake)"
        )
        try:
            _single_value(cursor, "SELECT count(*) FROM ducklake_flush_inlined_data($lake)", lake)
            expired = int(_single_value(cursor, expire, {**lake, "cutoff": cutoff}))
            merged, created = _single_value(
                cursor, compact.format(function="ducklake_merge_adjacent_files"), lake
            ).split(",")
            rewritten, rewritten_into = _single_value(
                cursor, compact.format(function="ducklake_rewrite_data_files"), lake
            ).split(",")
            expired += int(_single_value(cursor, expire, {**lake, "cutoff": cutoff}))
            deleted = _single_value(
                cursor,
                "SELECT count(*) FROM ducklake_cleanup_old_files($lake, cleanup_all => true)",
                lake,
            )
            orphaned = _single_value(
                cursor,
                "SELECT count(*) FROM ducklake_delete_orphaned_files("
                "$lake, older_than => CAST($orphans AS TIMESTAMPTZ))",
                {**lake, "orphans": orphans},
            )
            oldest = _snapshot_of(
                _single_value(
                    cursor,
                    "SELECT snapshot_id || ',' || epoch_us(snapshot_time) "
                    "FROM ducklake_snapshots($lake) ORDER BY snapshot_id LIMIT 1",
                    lake,
                ).split(",")
            )
        except duckdb.Error as error:
            raise StoreError(
                "STORE_QUERY_FAILED", "Lake maintenance failed.", {"duckdb": str(error)}
            ) from error
        return MaintenanceReport(
            kind="maintenance_report",
            snapshots_expired=expired,
            files_merged=int(merged),
            files_created=int(created) + int(rewritten_into),
            files_rewritten=int(rewritten),
            files_deleted=int(deleted),
            orphaned_files_deleted=int(orphaned),
            oldest_snapshot=oldest,
        )

    def current_snapshot(self) -> Snapshot:
        snapshot = self._snapshot(None)
        if snapshot is None:
            raise StoreError("STORE_SNAPSHOT_NOT_FOUND", "The lake has no snapshot.", {})
        return snapshot

    def _tables_at(self, snapshot: int) -> set[tuple[str, str]]:
        """(schema, table) of every lake table alive at `snapshot`, lower-cased."""
        row = self._one(
            f"SELECT string_agg(lower(s.schema_name) || '.' || lower(t.table_name), '|') "
            f"FROM {self._metadata}.ducklake_table AS t "
            f"JOIN {self._metadata}.ducklake_schema AS s USING (schema_id) "
            "WHERE t.begin_snapshot <= $n AND (t.end_snapshot IS NULL OR t.end_snapshot > $n) "
            "AND s.begin_snapshot <= $n AND (s.end_snapshot IS NULL OR s.end_snapshot > $n)",
            {"n": snapshot},
        )
        text = str(row[0]) if row and row[0] is not None else ""
        found: set[tuple[str, str]] = set()
        for item in text.split("|"):
            schema, _, table = item.partition(".")
            if table:
                found.add((schema, table))
        return found

    def _pinned(self, sql: str, snapshot: int) -> str:
        """The same SELECT with `AT (VERSION => snapshot)` on every table it references.

        Every base table is pinned, including CTE references (DuckDB binds a CTE with an AT
        clause unchanged). Table functions other than generators are refused, because they could
        read the lake or its files without a snapshot.
        """
        serialized = self._one("SELECT json_serialize_sql($sql)", {"sql": sql})
        tree = json.loads(str(serialized[0])) if serialized else {"error": True}
        if tree.get("error"):
            raise StoreError(
                "STORE_QUERY_FAILED",
                "The SQL could not be parsed.",
                {"duckdb": str(tree.get("error_message", ""))},
            )
        tables = self._tables_at(snapshot)
        schemas = {schema for schema, _ in tables}
        ctes = _cte_names(tree)
        for node in _walk(tree):
            if node.get("type") == "TABLE_FUNCTION":
                function = node.get("function")
                name = function.get("function_name") if isinstance(function, dict) else None
                if str(name).lower() not in READ_TABLE_FUNCTIONS:
                    raise StoreError(
                        "STORE_STATEMENT_INVALID",
                        "A read may use only the table functions range, generate_series "
                        "and unnest.",
                        {"function": str(name)},
                    )
            if node.get("type") != "BASE_TABLE":
                continue
            qualified = node.get("qualified_name")
            raw = qualified.get("path") if isinstance(qualified, dict) else None
            path = (
                [str(part) for part in raw] if isinstance(raw, list) else [str(node["table_name"])]
            )
            if not _is_lake_table(path, tables, schemas, ctes):
                raise StoreError(
                    "STORE_STATEMENT_INVALID",
                    "A read may reference only tables of the lake (and its own CTEs).",
                    {"table": ".".join(path)},
                )
            node["at_clause"] = {
                "unit": "VERSION",
                "expr": {
                    "class": "CONSTANT",
                    "type": "VALUE_CONSTANT",
                    "alias": "",
                    "query_location": 0,
                    "query_location_length": 0,
                    "literal": {"kind": "INTEGER", "text": str(int(snapshot))},
                },
            }
        rewritten = self._one("SELECT json_deserialize_sql($tree)", {"tree": json.dumps(tree)})
        return str(rewritten[0]) if rewritten else sql

    def describe(self, sql: str, parameters: Sequence[Parameter] = ()) -> list[tuple[str, str]]:
        """(name, DuckDB type) of each output column of one SELECT, from the binder only.

        Nothing is executed. Sources may be named (writes read them); the statement checks of
        writes apply (no file readers, no quoted file names, no catalog database). Parameters
        are bound as for a write (typically with a `null` value: only their type matters).
        """
        statement = _single_statement(sql, self._table_functions)
        if statement.type != duckdb.StatementType.SELECT:
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "Only a SELECT can be described.",
                {"statement": statement.type.name},
            )
        bound = _bound(statement, list(parameters))
        typed = _typed(sql, list(parameters))
        self._admission.acquire(time.monotonic() + self._timeout_ms(None) / 1000)
        cursor = None
        try:
            cursor = self._cursor()
            relation = cursor.sql(typed, params=bound) if bound else cursor.sql(typed)
            return [
                (str(name), str(kind))
                for name, kind in zip(relation.columns, relation.types, strict=True)
            ]
        except duckdb.Error as error:
            raise StoreError(
                "STORE_QUERY_FAILED", "DuckDB could not bind the query.", {"duckdb": str(error)}
            ) from None
        finally:
            if cursor is not None:
                cursor.close()
            self._admission.release()

    def read(self, request: ReadRequest) -> pa.RecordBatchReader:
        """Run one SELECT as of `request.snapshot` and return it once, as Arrow batches."""
        return self.start_read(request).reader

    def _timeout_ms(self, requested: int | None) -> int:
        assert self._limits.default_timeout_ms is not None
        return requested if requested is not None else self._limits.default_timeout_ms

    def start_read(self, request: ReadRequest) -> ReadHandle:
        """Like `read`, with a handle that can cancel the read from another thread.

        The read waits for an admission slot, holds it until its stream ends, and is interrupted
        when its timeout (counted from the call, waiting included) expires.
        """
        statement = _single_statement(request.sql, self._table_functions)
        if statement.type != duckdb.StatementType.SELECT:
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A read must be one SELECT statement.",
                {"statement": statement.type.name},
            )
        parameters = _bound(statement, request.parameters)
        if self._snapshot(request.snapshot) is None:
            raise self._missing_snapshot(request.snapshot)
        sql = self._pinned(_typed(request.sql, request.parameters), request.snapshot)
        deadline = time.monotonic() + self._timeout_ms(request.timeout_ms) / 1000
        self._admission.acquire(deadline)
        try:
            # Checked again once admitted: maintenance may have expired the snapshot while this
            # read waited behind it (maintenance runs alone, so it cannot expire it from now on).
            if self._snapshot(request.snapshot) is None:
                raise self._missing_snapshot(request.snapshot)
            cursor = self._cursor()
        except BaseException:
            self._admission.release()
            raise
        guard = Guard(cursor, deadline)
        # Who owns the cursor: the calling thread while it sets the read up, nobody while the
        # stream waits for its first read, the reading thread while it streams. Only an idle
        # read is closed by the timer (or by collection of a dropped reader); the owner closes
        # it otherwise. The slot is released exactly once.
        state = threading.Lock()
        phase = "setup"

        def finish() -> None:
            nonlocal phase
            with state:
                if phase == "finished":
                    return
                phase = "finished"
            try:
                guard.stop()
                cursor.close()
            finally:
                self._admission.release()

        def release_if_idle() -> None:
            with state:
                idle = phase == "idle"
            if idle:
                finish()

        guard.on_fire = release_if_idle

        try:
            # Binding is quick and returns before any row is computed; the work runs on the first
            # read of the stream, where a timeout or a cancel can still interrupt it.
            relation = cursor.sql(sql, params=parameters)
            names = list(relation.columns)
            types = [str(kind) for kind in relation.types]
            exact = exact_select(sql, names, types)
            if exact is not None:
                relation = cursor.sql(exact, params=parameters)
            empty = relation.limit(0).to_arrow_table()
            schema = _with_duckdb_types(_utc_schema(empty.schema), types)
        except duckdb.Error as error:
            finish()
            if isinstance(error, duckdb.InterruptException) or guard.reason:
                raise guard.error(error) from error
            raise StoreError(
                "STORE_QUERY_FAILED", "DuckDB could not run the read.", {"duckdb": str(error)}
            ) from error
        except BaseException:
            finish()
            raise
        if guard.reason is not None:
            # The limit expired or a cancel came while the read was being set up.
            finish()
            raise guard.error()
        with state:
            phase = "idle"
            fired = guard.reason is not None
        if fired:
            # It fired between the check above and now, and found the read still being set up.
            release_if_idle()

        def batches() -> Iterator[pa.RecordBatch]:
            # The cursor and the admission slot live as long as the stream.
            nonlocal phase
            with state:
                if phase == "finished":
                    raise guard.error()
                phase = "streaming"
            try:
                if guard.reason is not None:
                    raise guard.error()
                for batch in relation.to_arrow_reader(request.batch_size):
                    if guard.reason is not None:
                        raise guard.error()
                    yield batch if batch.schema == schema else batch.cast(schema)
            except (OSError, pa.ArrowException, duckdb.Error) as error:
                if guard.reason is not None or isinstance(error, duckdb.InterruptException):
                    raise guard.error(error) from error
                raise StoreError(
                    "STORE_QUERY_FAILED",
                    "DuckDB failed while streaming the read.",
                    {"duckdb": str(error)},
                ) from error
            finally:
                finish()

        reader = pa.RecordBatchReader.from_batches(schema, batches())
        # A stream dropped before its first batch never runs its generator, so its slot is
        # released when the reader is collected (a started stream releases it when closed).
        weakref.finalize(reader, release_if_idle)
        return ReadHandle(reader=reader, _guard=guard)

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """Statements inside the block commit together to one snapshot, or roll back on error.

        Write transactions in this process run one at a time, so the snapshot reported in the
        result is the one this transaction created.
        """
        if getattr(self._local, "writing", False):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A transaction cannot be opened inside another one in the same thread.",
                {},
            )
        deadline = time.monotonic() + self._timeout_ms(None) / 1000
        self._admission.acquire(deadline)
        try:
            yield from self._transaction(deadline)
        finally:
            self._admission.release()

    def _transaction(self, deadline: float) -> Iterator[Transaction]:
        # Waiting for the writer ahead counts against the same time limit as the admission queue.
        if not self._write_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise StoreError(
                "STORE_TIMEOUT", "The transaction waited too long for the writer ahead.", {}
            )
        try:
            self._local.writing = True
            try:
                cursor = self._cursor()
            except BaseException:
                self._local.writing = False
                raise
            try:
                before = self._snapshot(None)
                outside = _outside_inventory(cursor)
                cursor.begin()
                handle = Transaction(
                    cursor, self._table_functions, self._timeout_ms, frozenset(self._inexact)
                )
                try:
                    yield handle
                    if handle.cancelled:
                        raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
                    if _outside_inventory(cursor) != outside:
                        raise StoreError(
                            "STORE_STATEMENT_INVALID",
                            "A transaction may change only tables and schemas of the lake.",
                            {},
                        )
                    if _unpinnable_objects(cursor):
                        raise StoreError(
                            "STORE_STATEMENT_INVALID",
                            "Views and macros are not allowed: a read could not pin the "
                            "tables they use to a snapshot.",
                            {},
                        )
                except BaseException:
                    cursor.rollback()
                    raise
                try:
                    cursor.commit()
                except duckdb.TransactionException as error:
                    raise StoreError(
                        "STORE_TRANSACTION_CONFLICT",
                        "Another writer changed the same data first; nothing was written.",
                        {"duckdb": str(error)},
                    ) from error
                except duckdb.Error as error:
                    raise StoreError(
                        "STORE_QUERY_FAILED",
                        "The transaction could not commit.",
                        {"duckdb": str(error)},
                    ) from error
                after = self._snapshot(None)
            finally:
                cursor.close()
                self._local.writing = False
            changed = after is not None and (before is None or after.id != before.id)
            handle.result = CommitResult(kind="commit_result", snapshot=after if changed else None)
        finally:
            self._write_lock.release()


def _snapshot_of(row: Sequence[object]) -> Snapshot:
    """A snapshot from (id, microseconds since the epoch): no time-zone library is needed."""
    committed = dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(microseconds=int(str(row[1])))
    return Snapshot(kind="snapshot", id=int(str(row[0])), committed_at=committed)


def _unpinnable_objects(cursor: duckdb.DuckDBPyConnection) -> int:
    """Views in the lake and user macros anywhere, read inside the open transaction.

    Both can read lake tables from inside a function call or a view body, where a read's
    snapshot rewrite cannot reach them, so they are not allowed to exist.
    """
    result = cursor.execute(
        "SELECT (SELECT count(*) FROM duckdb_views() "
        "        WHERE lower(database_name) = $lake AND NOT internal) "
        "     + (SELECT count(*) FROM duckdb_functions() "
        "        WHERE NOT internal AND function_type IN ('macro', 'table_macro'))",
        {"lake": LAKE},
    )
    row = result.fetchone()
    result.fetchone()  # reaches the end of the one-row result, so the cursor is free again
    return int(str(row[0])) if row else 0


def _single_value(
    cursor: duckdb.DuckDBPyConnection, sql: str, parameters: dict[str, object]
) -> str:
    """One value of a one-row query on `cursor`, leaving the cursor free for the next statement."""
    result = cursor.execute(sql, parameters)
    row = result.fetchone()
    result.fetchone()  # reaches the end of the one-row result
    return "" if not row or row[0] is None else str(row[0])


def _outside_inventory(cursor: duckdb.DuckDBPyConnection) -> str:
    """Every user object outside the lake: tables, views, sequences, types, macros, secrets."""
    return _single_value(
        cursor,
        "SELECT string_agg(item, '|' ORDER BY item) FROM ("
        " SELECT 'table:' || database_name || '.' || schema_name || '.' || table_name AS item"
        " FROM duckdb_tables() WHERE lower(database_name) NOT IN ($lake, $metadata)"
        " UNION ALL SELECT 'view:' || database_name || '.' || schema_name || '.' || view_name"
        " FROM duckdb_views() WHERE NOT internal AND lower(database_name) NOT IN ($lake, $metadata)"
        " UNION ALL SELECT 'sequence:' || database_name || '.' || schema_name || '.'"
        " || sequence_name"
        " FROM duckdb_sequences() WHERE lower(database_name) NOT IN ($lake, $metadata)"
        " UNION ALL SELECT 'type:' || database_name || '.' || schema_name || '.' || type_name"
        " FROM duckdb_types() WHERE NOT internal AND lower(database_name) NOT IN ($lake, $metadata)"
        " UNION ALL SELECT 'secret:' || name FROM duckdb_secrets()"
        ")",
        {"lake": LAKE, "metadata": METADATA},
    )


def _created_kind_allowed(sql: str, statement: duckdb.Statement) -> bool:
    """CREATE, DROP and ALTER may only target tables and schemas (decided from the tokens)."""
    if statement.type not in {
        duckdb.StatementType.CREATE,
        duckdb.StatementType.DROP,
        duckdb.StatementType.ALTER,
    }:
        return True
    tokens = duckdb.tokenize(sql)
    ends = [position for position, _ in tokens[1:]] + [len(sql)]
    words = [
        sql[position:end].strip().upper()
        for (position, kind), end in zip(tokens, ends, strict=True)
        if kind == duckdb.token_type.keyword
    ][:6]
    modifiers = {"OR", "REPLACE", "TEMP", "TEMPORARY", "PERSISTENT", "IF", "NOT", "EXISTS"}
    target = next((word for word in words[1:] if word not in modifiers), "")
    return target in {"TABLE", "SCHEMA"}


class Transaction:
    """A write transaction; `result` is set when the block commits."""

    def __init__(
        self,
        cursor: duckdb.DuckDBPyConnection,
        table_functions: frozenset[str],
        timeout_ms: Callable[[int | None], int],
        inexact: frozenset[Inexact] = frozenset(),
    ) -> None:
        self._cursor = cursor
        self._table_functions = table_functions
        self._inexact = inexact
        self._timeout_ms = timeout_ms
        self._guard: Guard | None = None
        self._lock = threading.Lock()
        self.cancelled = False
        self.result: CommitResult | None = None

    def cancel(self) -> None:
        """Cancel from any thread: a running statement is interrupted, and the block rolls back."""
        with self._lock:
            self.cancelled = True
            guard = self._guard
        if guard is not None:
            guard.cancel()

    def value(self, request: StatementRequest) -> str | None:
        """The first column of the first row of one SELECT run inside this transaction, as text.

        For small checks between statements (counts, existence): it sees the transaction's
        uncommitted changes. `None` when there is no row or the value is NULL.
        """
        statement = _single_statement(request.sql, self._table_functions)
        if statement.type != duckdb.StatementType.SELECT:
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A transaction value must come from one SELECT.",
                {"statement": statement.type.name},
            )
        parameters = _bound(statement, request.parameters)
        deadline = time.monotonic() + self._timeout_ms(request.timeout_ms) / 1000
        guard = Guard(self._cursor, deadline)
        with self._lock:
            if self.cancelled:
                guard.stop()
                raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
            self._guard = guard
        typed = _typed(request.sql, request.parameters)
        try:
            inexact = _inexact_column_read(self._cursor, typed, parameters, self._inexact)
            if inexact is not None:
                raise StoreError(
                    "UNSUPPORTED_SOURCE_TYPE",
                    f"Column {inexact} has no exact type in DuckDB (law 11); cast it at the "
                    "source or leave it out.",
                    {"column": inexact},
                )
            result = self._cursor.execute(
                f"SELECT CAST(#1 AS VARCHAR) FROM ({typed}) LIMIT 1", parameters
            )
            row = result.fetchone()
            result.fetchone()  # reaches the end of the result, so the cursor is free again
        except duckdb.Error as error:
            raise guard.error(error) from error
        finally:
            guard.stop()
            self._guard = None
        return None if not row or row[0] is None else str(row[0])

    def execute(self, request: StatementRequest) -> None:
        statement = _single_statement(request.sql, self._table_functions)
        if statement.type not in WRITE_STATEMENTS or not _created_kind_allowed(
            request.sql, statement
        ):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "This statement kind is not allowed in a transaction.",
                {"statement": statement.type.name},
            )
        parameters = _bound(statement, request.parameters)
        deadline = time.monotonic() + self._timeout_ms(request.timeout_ms) / 1000
        guard = Guard(self._cursor, deadline)
        # Checked and published together, so a cancel either sees this guard or is seen here.
        with self._lock:
            if self.cancelled:
                guard.stop()
                raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
            self._guard = guard
        typed = _typed(request.sql, request.parameters)
        try:
            inexact = _inexact_column_read(self._cursor, typed, parameters, self._inexact)
            if inexact is not None:
                raise StoreError(
                    "UNSUPPORTED_SOURCE_TYPE",
                    f"Column {inexact} has no exact type in DuckDB (law 11); cast it at the "
                    "source or leave it out.",
                    {"column": inexact},
                )
            self._cursor.sql(typed, params=parameters)
        except duckdb.Error as error:
            raise guard.error(error) from error
        finally:
            guard.stop()
            self._guard = None


def _inexact_column_read(
    cursor: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: dict[str, str | None],
    inexact: frozenset[Inexact],
) -> str | None:
    """`source.table.column` of an inexact source column the statement reads, or None.

    The columns a statement reads are those DuckDB's plan scans (projections and pushed-down
    filters). A scan is matched by its table name and, when the plan names one, its schema or
    source; when two candidates share a name, any of them counts (refusing beats a lost digit).
    A statement that names none of these sources is not planned twice.
    """
    lowered = sql.lower()
    # Every inexact column of a source the statement names: a SQLite view is expanded by
    # DuckDB, so its scan names the underlying table, not the view.
    candidates = [entry for entry in inexact if entry.source in lowered]
    if not candidates:
        return None
    result = cursor.execute(f"EXPLAIN (FORMAT json) {sql}", parameters)
    row = result.fetchone()
    result.fetchone()  # reaches the end of the one-row result, so the cursor is free again
    plan = json.loads(str(row[1])) if row else []
    for node in _walk(plan):
        info = node.get("extra_info")
        table = info.get("Table") if isinstance(info, dict) else None
        if not isinstance(info, dict) or not isinstance(table, str):
            continue
        scanned = table.lower().replace('"', "").split(".")
        for entry in candidates:
            if scanned[-1] != entry.table:
                continue
            qualifiers = scanned[:-1]
            if qualifiers and not ({entry.schema, entry.source} & set(qualifiers)) and entry.schema:
                continue
            if "Projections" not in info:
                return f"{entry.source}.{entry.label}"  # columns unknown: assume all are read
            used = f"{info.get('Projections')} {info.get('Filters', '')}".lower()
            if re.search(rf"(?<![a-z0-9_]){re.escape(entry.column)}(?![a-z0-9_])", used):
                return f"{entry.source}.{entry.label}"
    return None


type JsonTree = dict[str, object]


def _walk(node: object) -> Iterator[dict[str, object]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _cte_names(tree: JsonTree) -> set[str]:
    names: set[str] = set()
    for node in _walk(tree):
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map", []):
                if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                    names.add(entry["key"].lower())
    return names


def _is_lake_table(
    path: list[str], tables: set[tuple[str, str]], schemas: set[str], ctes: set[str]
) -> bool:
    """Whether a FROM name is a lake table alive at the snapshot, or one of the query's CTEs.

    Names that look like files or URLs are refused outright: DuckDB would otherwise fall back to
    reading them as files when no table matches.
    """
    parts = [part.lower() for part in path]
    if any(_SUSPICIOUS.search(part) for part in parts):
        return False
    if len(parts) == 1:
        return parts[0] in ctes or ("main", parts[0]) in tables
    if len(parts) == 2:
        if parts[0] in schemas:
            return (parts[0], parts[1]) in tables
        return parts[0] == LAKE and ("main", parts[1]) in tables
    return len(parts) == 3 and parts[0] == LAKE and (parts[1], parts[2]) in tables


def exact_select(sql: str, names: list[str], types: list[str]) -> str | None:
    """`sql` with every UHUGEINT column cast to decimal text, or None when there is none.

    Arrow has no exact 128-bit unsigned type: DuckDB would wrap large values into
    decimal128(38,0) (the maximum becomes -1). Columns are addressed by position (`#n`), so
    duplicate names are kept as they are and nothing else changes.
    """
    if "UHUGEINT" not in types:
        return None
    columns = ", ".join(
        (f"CAST(#{index} AS VARCHAR)" if kind == "UHUGEINT" else f"#{index}")
        + f" AS {_identifier(name)}"
        for index, (name, kind) in enumerate(zip(names, types, strict=True), start=1)
    )
    return f"SELECT {columns} FROM ({sql})"


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _with_duckdb_types(schema: pa.Schema, types: list[str]) -> pa.Schema:
    """Record each column's DuckDB type in its field metadata (`duckdb_type`).

    Arrow alone cannot tell `HUGEINT` from `DECIMAL(38,0)`; consumers (P1-03) need the type.
    """
    fields = [
        field.with_metadata({**(field.metadata or {}), b"duckdb_type": kind.encode()})
        for field, kind in zip(schema, types, strict=True)
    ]
    return pa.schema(fields, metadata=schema.metadata)


def _utc_schema(schema: pa.Schema) -> pa.Schema:
    """Name every zoned timestamp's zone `UTC` (DuckDB may say `Etc/UTC`); values are unchanged."""
    fields = [
        field.with_type(pa.timestamp(field.type.unit, tz="UTC"))
        if pa.types.is_timestamp(field.type) and field.type.tz is not None
        else field
        for field in schema
    ]
    return pa.schema(fields, metadata=schema.metadata)


class Inexact(NamedTuple):
    """A source column DuckDB cannot carry exactly."""

    source: str
    schema: str  # "" for SQLite
    table: str
    column: str
    label: str  # how errors name it, after the source name


type AnySource = (
    SourceAttachment | FileSourceAttachment | SqliteSourceAttachment | PostgresSourceAttachment
)


def _source_password(source: PostgresSourceAttachment) -> str:
    password = os.environ.get(source.password_env, "")
    if not password:
        raise StoreError(
            "STORE_SECRET_MISSING",
            f"The environment variable {source.password_env} is not set.",
            {"variable": source.password_env, "source": source.name},
        )
    register_secret(password)
    return password


def _sqlite_snapshot(source: SqliteSourceAttachment, copy: Path) -> list[tuple[str, str, str]]:
    """Copy the SQLite file to `copy` and return (table, column, declared type) of the copy.

    A file with views is refused: DuckDB turns a SQLite view's SQL into a DuckDB view that runs
    with the lake's connection, where it could call functions the statement guard refuses (and
    run SQL on other sources). Triggers never fire on a read-only attachment.
    """
    uri = Path(source.path).absolute().as_uri() + "?mode=ro"
    failure: StoreError | None = None
    found: list[tuple[str, str, str]] = []
    try:
        original = sqlite3.connect(uri, uri=True)
        target = sqlite3.connect(copy)
        try:
            original.backup(target)
        finally:
            original.close()
        try:
            views = target.execute("SELECT count(*) FROM sqlite_master WHERE type = 'view'")
            row = views.fetchone()
            if row and int(row[0]):
                failure = StoreError(
                    "STORE_SOURCE_INVALID",
                    f"Source {source.name} defines views, which a lake cannot attach.",
                    {"source": source.name},
                )
            tables = target.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
            for (table,) in list(tables):
                for name, kind in target.execute(
                    "SELECT name, type FROM pragma_table_info(?)", (table,)
                ):
                    found.append((str(table), str(name), str(kind or "")))
        finally:
            target.close()
    except sqlite3.Error as error:
        failure = StoreError(
            "STORE_SOURCE_INVALID",
            f"Source {source.name} is not a readable SQLite database.",
            {"source": source.name, "sqlite": str(error)},
        )
    if failure is not None:
        raise failure
    return found


def _source_not_found(source: FileSourceAttachment | SqliteSourceAttachment) -> StoreError:
    return StoreError(
        "SOURCE_NOT_FOUND",
        f"No file matches the path of source {source.name}.",
        {"source": source.name},
    )


def open_lake(
    location: LakeLocation | LocalLakeLocation | PostgresLakeLocation,
    limits: Limits,
    sources: Sequence[AnySource] = (),
) -> Lake:
    return Lake(location, limits, sources)
