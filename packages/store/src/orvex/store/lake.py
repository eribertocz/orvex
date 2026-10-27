"""DuckLake lake with a local DuckDB-file catalog (contract `store` v1, P1-01).

Writes run inside DuckDB in explicit transactions; each read is pinned to a snapshot by adding
`AT (VERSION => n)` to every lake table it references (rewritten with DuckDB's own parser) and is
read once, as an Arrow record-batch stream (ADR-0025). No row passes through Python.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
from orvex.contracts.store.v1 import (
    CommitResult,
    LakeLocation,
    LocalLakeLocation,
    Parameter,
    PostgresLakeLocation,
    ReadRequest,
    Snapshot,
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
        "query",
        "query_table",
    }
)
_SUSPICIOUS = re.compile(r"[./\\*?:]")
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


class Guard:
    """Interrupts a DuckDB cursor at a deadline or on request, and remembers why."""

    def __init__(self, cursor: duckdb.DuckDBPyConnection, deadline: float) -> None:
        self._cursor = cursor
        self.reason: str | None = None
        self.deadline = deadline
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self) -> None:
        self._interrupt("STORE_TIMEOUT")

    def cancel(self) -> None:
        self._interrupt("STORE_CANCELLED")

    def _interrupt(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason
            self._cursor.interrupt()

    def stop(self) -> None:
        self._timer.cancel()

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
    for index, (position, kind) in enumerate(tokens):
        if kind not in {duckdb.token_type.identifier, duckdb.token_type.keyword}:
            continue
        end = next(later for later in positions if later > position)
        name = sql[position:end].strip().strip('"').lower()
        following = tokens[index + 1][0] if index + 1 < len(tokens) else len(sql)
        if (
            sql[following : following + 1] == "("
            and name in table_functions
            and name not in READ_TABLE_FUNCTIONS
        ):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "Only the table functions range, generate_series and unnest are allowed.",
                {"function": name},
            )


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
        self, location: LakeLocation | LocalLakeLocation | PostgresLakeLocation, limits: Limits
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
            self._table_functions = self._names_of_table_functions()
            self._sandbox(location)
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

    def _sandbox(self, location: LocalLakeLocation | PostgresLakeLocation) -> None:
        """Limit file access to the lake's own folders, for good (DuckDB cannot undo it)."""
        folders = [self._spill]
        if isinstance(location, LocalLakeLocation):
            folders.append(Path(location.path).resolve().as_posix())
        elif "://" not in location.data_path:
            folders.append(Path(location.data_path).resolve().as_posix())
        listed = ", ".join(_quote(folder.rstrip("/") + "/") for folder in folders)
        self._connection.sql(f"SET temp_directory = {_quote(self._spill)}")
        self._connection.sql(f"SET allowed_directories = [{listed}]")
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
        if row is None:
            return None
        # Microseconds since the epoch, so no time-zone library is needed to read the instant.
        committed = dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(
            microseconds=int(str(row[1]))
        )
        return Snapshot(kind="snapshot", id=int(str(row[0])), committed_at=committed)

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
            raise StoreError(
                "STORE_SNAPSHOT_NOT_FOUND",
                f"Snapshot {request.snapshot} does not exist in the lake.",
                {"snapshot": str(request.snapshot)},
            )
        sql = self._pinned(_typed(request.sql, request.parameters), request.snapshot)
        deadline = time.monotonic() + self._timeout_ms(request.timeout_ms) / 1000
        self._admission.acquire(deadline)
        try:
            cursor = self._cursor()
        except BaseException:
            self._admission.release()
            raise
        guard = Guard(cursor, deadline)

        def finish() -> None:
            guard.stop()
            cursor.close()
            self._admission.release()

        try:
            relation = cursor.sql(sql, params=parameters)
            names = list(relation.columns)
            types = [str(kind) for kind in relation.types]
            exact = exact_select(sql, names, types)
            if exact is not None:
                relation = cursor.sql(exact, params=parameters)
            source = relation.to_arrow_reader(request.batch_size)
        except duckdb.Error as error:
            finish()
            if isinstance(error, duckdb.InterruptException) or guard.reason:
                raise guard.error(error) from error
            raise StoreError(
                "STORE_QUERY_FAILED", "DuckDB could not run the read.", {"duckdb": str(error)}
            ) from error

        schema = _with_duckdb_types(_utc_schema(source.schema), types)

        def batches() -> Iterator[pa.RecordBatch]:
            # The cursor and the admission slot live as long as the stream.
            try:
                for batch in source:
                    if guard.reason is not None:
                        raise guard.error()
                    yield batch if batch.schema == schema else batch.cast(schema)
            except (OSError, pa.ArrowException, duckdb.Error) as error:
                if guard.reason is not None or "INTERRUPT" in str(error):
                    raise guard.error(error) from error
                raise StoreError(
                    "STORE_QUERY_FAILED",
                    "DuckDB failed while streaming the read.",
                    {"duckdb": str(error)},
                ) from error
            finally:
                finish()

        reader = pa.RecordBatchReader.from_batches(schema, batches())
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
        self._admission.acquire(time.monotonic() + self._timeout_ms(None) / 1000)
        try:
            yield from self._transaction()
        finally:
            self._admission.release()

    def _transaction(self) -> Iterator[Transaction]:
        with self._write_lock:
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
                handle = Transaction(cursor, self._table_functions, self._timeout_ms)
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
    ) -> None:
        self._cursor = cursor
        self._table_functions = table_functions
        self._timeout_ms = timeout_ms
        self._guard: Guard | None = None
        self.cancelled = False
        self.result: CommitResult | None = None

    def cancel(self) -> None:
        """Cancel from any thread: a running statement is interrupted, and the block rolls back."""
        self.cancelled = True
        guard = self._guard
        if guard is not None:
            guard.cancel()

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
        if self.cancelled:
            raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
        deadline = time.monotonic() + self._timeout_ms(request.timeout_ms) / 1000
        guard = Guard(self._cursor, deadline)
        self._guard = guard
        try:
            self._cursor.sql(_typed(request.sql, request.parameters), params=parameters)
        except duckdb.Error as error:
            raise guard.error(error) from error
        finally:
            guard.stop()
            self._guard = None


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


def open_lake(
    location: LakeLocation | LocalLakeLocation | PostgresLakeLocation, limits: Limits
) -> Lake:
    return Lake(location, limits)
