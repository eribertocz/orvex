"""ClickHouse Direct Query: one verified, read-only connection (P2-31 PR 1, #225; ADR-0028 §3).

`store` owns execution on every engine (ADR-0027 §8). This module never generates SQL from a
plan; it runs the statement the semantic adapter wrote, over ClickHouse's HTTP interface:

- **Verified at connection, failing closed before any data is read:** a tested server version
  (`ENGINE_VERSION_UNTESTED`), a read-only profile that cannot change settings (`readonly = 1`,
  else `SOURCE_NOT_READ_ONLY`), the settings that decide exact meaning and server-side limits
  within the caller's budget (`ENGINE_SETTING_REQUIRED`, naming the setting).
- **Bound values only:** every value is a typed server-side parameter (`{name:Type}`), never
  text in the statement.
- **Exact types:** the result comes in ClickHouse's `ArrowStream` format and is normalized,
  column by column with Arrow casts, to the store contract's Arrow types; anything else fails.
- **Budgets:** the server's limits are the ceiling; a tighter time budget is enforced here by
  closing the connection, which cancels the query on the server
  (`cancel_http_readonly_queries_on_client_close = 1`, verified).
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.ipc
from duckdb.sqltypes import DuckDBPyType
from orvex.contracts.store.v1 import Parameter
from orvex.store.errors import StoreError, register_secret
from orvex.store.nested_types import refuse_nested_uhugeint

# Server versions tested (major.minor): the current LTS and the latest stable (ADR-0022).
TESTED_VERSIONS = ("26.8", "26.9")

# Settings that decide what a statement means; each must have exactly this value. `readonly` is
# checked first, with its own code. The adapter relies on every one of them.
REQUIRED_SETTINGS: Mapping[str, str] = {
    "join_use_nulls": "1",  # an outer join gives NULL, never a type's default value
    "group_by_use_nulls": "1",  # a subtotal's key is NULL, never a default value
    "transform_null_in": "0",  # NULL membership is written out by the adapter
    "count_distinct_implementation": "uniqExact",  # an exact distinct count
    "cast_keep_nullable": "1",  # CAST keeps NULL
    "aggregate_functions_null_for_empty": "0",  # empty-set results are written out by the adapter
    "cancel_http_readonly_queries_on_client_close": "1",  # closing the connection cancels
}
LIMIT_SETTINGS = ("max_execution_time", "max_memory_usage", "max_result_rows")
# Settings a profile may also change: none of them changes a result. Any other changed setting
# is refused, so no setting can silently cut, filter, cache or reshape a result
# (`result_overflow_mode`, `additional_table_filters`, `use_query_cache`, `limit`, ...).
HARMLESS_SETTINGS = frozenset({"database", "max_threads", "log_queries", "log_comment", "priority"})

_TIMEOUT_CODES = frozenset({"159"})  # TIMEOUT_EXCEEDED
# One left-to-right scan of a statement: at each position the first alternative that matches
# wins, so a quote inside a comment, a comment marker inside a string, or a `{` inside either is
# never read as anything else. ClickHouse's comments are `--`, `#` (and `#!`) to the end of the
# line, and `/* */`; its strings take `\\` escapes and doubled quotes.
_SCAN = re.compile(
    r"(?P<comment>--[^\n]*|#[^\n]*|/\*.*?\*/)"
    r"|(?P<string>'(?:[^'\\]|\\.|'')*'|\$(?P<tag>\w*)\$.*?\$(?P=tag)\$)"
    r"|(?P<name>`(?:[^`\\]|\\.|``)*`|\"(?:[^\"\\]|\\.|\"\")*\")"
    r"|\{(?P<parameter>[A-Za-z_][A-Za-z0-9_]*):(?P<type>[^{}]+)\}"
    r"|(?P<open>/\*|['`\"]|\$\w*\$)",  # a comment, quote or heredoc never closed
    re.S,
)
_FORBIDDEN_WORDS = re.compile(r"\b(SETTINGS|FORMAT|INTO\s+OUTFILE)\b", re.I)
_DECIMAL = re.compile(r"DECIMAL\((\d+),(\d+)\)")

# Parameter types: the store contract's DuckDB type, and the ClickHouse type it binds as.
_PARAMETER_TYPES: Mapping[str, str] = {
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
    "DOUBLE": "Float64",
    "VARCHAR": "String",
    "DATE": "Date32",
    "TIMESTAMPTZ": "DateTime64(6, 'UTC')",
}

# Result types: the store contract's Arrow type for each DuckDB type (`contracts/store/v1`).
_ARROW: Mapping[str, pa.DataType] = {
    "BOOLEAN": pa.bool_(),
    "TINYINT": pa.int8(),
    "SMALLINT": pa.int16(),
    "INTEGER": pa.int32(),
    "BIGINT": pa.int64(),
    "HUGEINT": pa.decimal128(38, 0),
    "UTINYINT": pa.uint8(),
    "USMALLINT": pa.uint16(),
    "UINTEGER": pa.uint32(),
    "UBIGINT": pa.uint64(),
    "UHUGEINT": pa.utf8(),
    "FLOAT": pa.float32(),
    "DOUBLE": pa.float64(),
    "VARCHAR": pa.utf8(),
    "UUID": pa.utf8(),
    "DATE": pa.date32(),
    "TIMESTAMPTZ": pa.timestamp("us", tz="UTC"),
}


@dataclass(frozen=True, slots=True)
class ClickHouseTarget:
    """Where the server is and who connects; the password only as an environment variable."""

    host: str
    port: int
    database: str
    user: str
    password_env: str
    tls: bool = True
    ca_file: str | None = None  # absent: the operating system's authorities


@dataclass(frozen=True, slots=True)
class Budget:
    """The most one query may use: the server's limits may be tighter, never looser."""

    timeout_ms: int
    memory_bytes: int
    result_rows: int


@dataclass(frozen=True, slots=True)
class SourceColumn:
    name: str
    engine_type: str  # as ClickHouse names it
    type: str  # the DuckDB type it maps to


def _local(host: str) -> bool:
    return host.strip().lower() in {"localhost", "127.0.0.1", "::1"}


def duckdb_type(engine_type: str) -> str | None:
    """The DuckDB type a ClickHouse column maps to exactly, or None when it has none."""
    inner = engine_type.strip()
    # `Nullable(T)`, `LowCardinality(T)` and `LowCardinality(Nullable(T))` read as T.
    for wrapper in ("LowCardinality(", "Nullable("):
        if inner.startswith(wrapper) and inner.endswith(")"):
            inner = inner[len(wrapper) : -1].strip()
    simple = {
        "Bool": "BOOLEAN",
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
        "Float32": "FLOAT",
        "Float64": "DOUBLE",
        "String": "VARCHAR",
        "UUID": "UUID",
        "Date": "DATE",
        "Date32": "DATE",
        "DateTime": "TIMESTAMPTZ",
    }
    if inner in simple:
        return simple[inner]
    if re.fullmatch(r"DateTime\('[A-Za-z0-9_+\-/]+'\)", inner):
        return "TIMESTAMPTZ"
    if found := re.fullmatch(r"DateTime64\((\d)(?:,\s*'[A-Za-z0-9_+\-/]+')?\)", inner):
        return "TIMESTAMPTZ" if int(found.group(1)) <= 6 else None  # finer than µs loses digits
    if found := re.fullmatch(r"Decimal\((\d+),\s*(\d+)\)", inner):
        precision, scale = int(found.group(1)), int(found.group(2))
        return f"DECIMAL({precision},{scale})" if precision <= 38 else None
    if found := re.fullmatch(r"Decimal(32|64|128)\((\d+)\)", inner):
        precision = {"32": 9, "64": 18, "128": 38}[found.group(1)]
        return f"DECIMAL({precision},{found.group(2)})"
    return None


def _engine_error(body: bytes) -> tuple[str, str]:
    """ClickHouse's error code and message from a response body."""
    text = body.decode("utf-8", "replace").strip()
    found = re.match(r"Code: (\d+)\. ", text)
    return (found.group(1) if found else ""), text[:2000]


class ClickHouse:
    """One verified connection to one ClickHouse database. Each call opens its own HTTP
    connection, so a read can be cancelled by closing it without affecting another."""

    def __init__(self, target: ClickHouseTarget, budget: Budget) -> None:
        if not target.tls and not _local(target.host):
            raise StoreError(
                "STORE_TLS_REQUIRED",
                "A ClickHouse server on another machine is reached only over verified TLS.",
                {"host": target.host},
            )
        password = os.environ.get(target.password_env)
        if password is None:
            raise StoreError(
                "STORE_SECRET_MISSING",
                f"The environment variable {target.password_env} is not set.",
                {"variable": target.password_env},
            )
        register_secret(password)
        self._target = target
        self._password = password
        self._budget = budget
        self.version = ""

    @classmethod
    def connect(cls, target: ClickHouseTarget, budget: Budget) -> ClickHouse:
        """Open and verify the connection: nothing is read unless every check passes."""
        engine = cls(target, budget)
        engine._verify()
        return engine

    # --- HTTP ----------------------------------------------------------------------------------

    def _http(self, timeout_s: float) -> http.client.HTTPConnection:
        target = self._target
        if target.tls:
            context = ssl.create_default_context(cafile=target.ca_file)
            return http.client.HTTPSConnection(
                target.host, target.port, timeout=timeout_s, context=context
            )
        return http.client.HTTPConnection(target.host, target.port, timeout=timeout_s)

    def _send(
        self, sql: str, values: Mapping[str, str], timeout_s: float
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        query = {"database": self._target.database, **values}
        connection = self._http(timeout_s)
        try:
            connection.request(
                "POST",
                "/?" + urlencode(query),
                body=sql.encode("utf-8"),
                headers={
                    "X-ClickHouse-User": self._target.user,
                    "X-ClickHouse-Key": self._password,
                    "Content-Type": "text/plain; charset=utf-8",
                },
            )
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            if isinstance(error, (TimeoutError, socket.timeout)):
                raise StoreError(
                    "STORE_TIMEOUT", "The ClickHouse statement exceeded its time limit.", {}
                ) from None
            raise StoreError(
                "STORE_CONNECTION_FAILED",
                f"The ClickHouse server cannot be reached: {type(error).__name__}.",
                {"host": self._target.host},
            ) from None
        if response.status != 200:
            body = response.read()
            connection.close()
            code, message = _engine_error(body)
            if code in _TIMEOUT_CODES:
                raise StoreError(
                    "STORE_TIMEOUT", "The ClickHouse statement exceeded its time limit.", {}
                )
            if code in {"516", "192", "194", "195"}:  # authentication and unknown user
                raise StoreError(
                    "STORE_CONNECTION_FAILED",
                    "ClickHouse refused the user or the password.",
                    {"host": self._target.host, "engine_code": code},
                )
            raise StoreError("STORE_QUERY_FAILED", message, {"engine_code": code} if code else {})
        return connection, response

    def _text(self, sql: str, values: Mapping[str, str] | None = None) -> list[list[str]]:
        """A small catalog query in TabSeparated text: schema facts, never data rows."""
        connection, response = self._send(
            sql + " FORMAT TabSeparated", dict(values or {}), self._budget.timeout_ms / 1000
        )
        try:
            body = response.read().decode("utf-8")
        finally:
            connection.close()
        return [line.split("\t") for line in body.splitlines() if line]

    def _json(self, sql: str, values: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
        """A small catalog query in JSON lines: schema and settings facts, never data rows."""
        connection, response = self._send(
            sql + " FORMAT JSONEachRow", dict(values or {}), self._budget.timeout_ms / 1000
        )
        try:
            body = response.read().decode("utf-8")
        finally:
            connection.close()
        try:
            rows = [json.loads(line) for line in body.splitlines() if line]
        except ValueError:
            rows = None
        if rows is None or not all(isinstance(row, dict) for row in rows):
            # Only a changed output-format setting reshapes JSONEachRow.
            raise _setting("output_format_*", "the defaults", "a changed output format")
        return rows

    # --- verification --------------------------------------------------------------------------

    def _verify(self) -> None:
        # One aggregated row, so no setting that cuts or skips rows can hide one: with
        # `offset` set it is skipped, and that alone refuses the connection.
        try:
            rows = self._json(
                "SELECT version() AS version, groupArray((name, value)) AS settings "
                "FROM system.settings WHERE changed"
            )
        except StoreError as error:
            # The query cache refuses this query (a system table, a non-deterministic function).
            if error.details.get("engine_code") in {"704", "719"}:
                raise _setting("use_query_cache", "0", "1") from None
            raise
        if len(rows) != 1:
            raise _setting("offset", "0", "a value that skips rows")
        version = str(rows[0]["version"])
        family = ".".join(version.split(".")[:2])
        if family not in TESTED_VERSIONS:
            raise StoreError(
                "ENGINE_VERSION_UNTESTED",
                f"ClickHouse {version} is not a tested version ({', '.join(TESTED_VERSIONS)}).",
                {"version": version},
            )
        self.version = version
        found = {str(name): str(value) for name, value in rows[0]["settings"]}
        if found.get("readonly") != "1":
            raise StoreError(
                "SOURCE_NOT_READ_ONLY",
                "The ClickHouse user's profile must set readonly = 1: read-only, and unable to "
                "change its own settings.",
                {"setting": "readonly", "expected": "1", "found": found.get("readonly", "0")},
            )
        allowed = {"readonly", *REQUIRED_SETTINGS, *LIMIT_SETTINGS, *HARMLESS_SETTINGS}
        if others := sorted(set(found) - allowed):
            raise _setting(",".join(others), "its default", ",".join(found[n] for n in others))
        defaults = self._json(
            "SELECT groupArray((name, value)) AS defaults FROM system.settings "
            "WHERE name IN {names:Array(String)}",
            {"param_names": _array([*REQUIRED_SETTINGS, *LIMIT_SETTINGS])},
        )
        current = {str(name): str(value) for name, value in defaults[0]["defaults"]}
        for name, expected in REQUIRED_SETTINGS.items():
            if current.get(name) != expected:
                raise _setting(name, expected, current.get(name, ""))
        limits = {
            "max_execution_time": self._budget.timeout_ms / 1000,
            "max_memory_usage": self._budget.memory_bytes,
            "max_result_rows": self._budget.result_rows,
        }
        for name, most in limits.items():
            value = Decimal(current.get(name, "0") or "0")
            if value <= 0 or value > Decimal(str(most)):
                raise _setting(name, f"more than 0 and at most {most}", current.get(name, ""))

    # --- catalog and clock ---------------------------------------------------------------------

    def columns(self, table: str) -> tuple[SourceColumn, ...]:
        """A table's columns with their exact DuckDB types; an unmappable one is refused."""
        rows = self._text(
            "SELECT name, type FROM system.columns WHERE database = {database:String} "
            "AND table = {table:String} ORDER BY position",
            {"param_database": _escape(self._target.database), "param_table": _escape(table)},
        )
        if not rows:
            raise StoreError(
                "SOURCE_NOT_FOUND",
                f"ClickHouse has no table {self._target.database}.{table} this user can read.",
                {"table": table},
            )
        found = []
        for name, engine_type in rows:
            mapped = duckdb_type(engine_type)
            if mapped is None:
                raise StoreError(
                    "UNSUPPORTED_SOURCE_TYPE",
                    f"Column {table}.{name} has type {engine_type}, which has no exact type.",
                    {"table": table, "column": name, "type": engine_type},
                )
            found.append(SourceColumn(name, engine_type, mapped))
        return tuple(found)

    def now(self) -> datetime:
        """The server's clock: a live read's instant (ADR-0027 §5)."""
        ((text,),) = self._text("SELECT now64(6, 'UTC')")
        return datetime.fromisoformat(text).replace(tzinfo=UTC)

    # --- reads ---------------------------------------------------------------------------------

    def read(
        self,
        sql: str,
        parameters: Sequence[Parameter],
        columns: Sequence[tuple[str, str]],
        timeout_ms: int | None = None,
    ) -> pa.RecordBatchReader:
        """Run one SELECT and return its result as Arrow in the store contract's types.

        `columns` are the result's (name, DuckDB type), in order. `HUGEINT`, `UHUGEINT` and
        `UUID` columns are returned by the statement as text (Arrow has no exact 128-bit
        integer); every other column in its own type.
        """
        refuse_nested_uhugeint([name for name, _ in columns], [_parsed(k) for _, k in columns])
        values = _bound(sql, parameters)
        timeout = min(timeout_ms or self._budget.timeout_ms, self._budget.timeout_ms) / 1000
        deadline = time.monotonic() + timeout
        values["query_id"] = uuid.uuid4().hex
        connection, response = self._send(sql + "\nFORMAT ArrowStream", values, timeout)
        schema = pa.schema(
            [pa.field(name, _arrow(kind), metadata={"duckdb_type": kind}) for name, kind in columns]
        )
        try:
            reader = pyarrow.ipc.open_stream(response)
        except (pa.ArrowInvalid, OSError) as error:
            connection.close()
            raise _broken(error, deadline) from None
        return pa.RecordBatchReader.from_batches(
            schema, _normalized(reader, schema, connection, deadline)
        )


def _normalized(
    reader: pa.RecordBatchReader,
    schema: pa.Schema,
    connection: http.client.HTTPConnection,
    deadline: float,
) -> Iterator[pa.RecordBatch]:
    """Each batch cast to the contract's types; closing the connection ends the query."""
    try:
        if len(reader.schema) != len(schema):
            raise StoreError(
                "STORE_QUERY_FAILED",
                f"ClickHouse returned {len(reader.schema)} columns, not {len(schema)}.",
                {},
            )
        while True:
            try:
                batch = reader.read_next_batch()
            except StopIteration:
                return
            except (pa.ArrowInvalid, OSError) as error:
                raise _broken(error, deadline) from None
            if time.monotonic() > deadline:
                raise StoreError(
                    "STORE_TIMEOUT", "The ClickHouse statement exceeded its time limit.", {}
                )
            yield pa.RecordBatch.from_arrays(
                [
                    _column(batch.column(i), batch.schema.field(i), field)
                    for i, field in enumerate(schema)
                ],
                schema=schema,
            )
    finally:
        connection.close()  # an unfinished query is cancelled on the server


def _column(values: pa.Array, got: pa.Field, want: pa.Field) -> pa.Array:
    kind = (want.metadata or {}).get(b"duckdb_type", b"").decode()
    target = want.type
    source = got.type
    try:
        if kind == "HUGEINT" and pa.types.is_string(source):
            return pc.cast(values, target)
        if pa.types.is_dictionary(source):  # LowCardinality
            values = values.dictionary_decode()
            source = values.type
        if kind in {"VARCHAR", "UHUGEINT", "UUID"} and pa.types.is_large_string(source):
            values = pc.cast(values, pa.utf8())
            source = values.type
        if pa.types.is_string(source) and source == target:
            values.validate(full=True)  # valid UTF-8 only
            digits = pc.match_substring_regex(values, r"^[0-9]{1,39}$")
            # One Arrow scalar for the whole column, compared in Arrow: no value becomes Python.
            if kind == "UHUGEINT" and pc.any(pc.invert(digits)).equals(pa.scalar(True)):
                raise pa.ArrowInvalid("a value is not unsigned decimal digits")
            return values
        if pa.types.is_timestamp(target) and pa.types.is_timestamp(source):
            if source.unit == "us" and source.tz is not None:
                return values.cast(target)  # the same instants; only the zone label changes
        elif pa.types.is_decimal(target) and pa.types.is_decimal(source):
            if source.scale == target.scale and source.precision >= target.precision:
                return pc.cast(values, target, safe=True)  # fails on a value that overflows
        elif source == target:
            return values
    except (pa.ArrowInvalid, pa.ArrowTypeError) as error:
        raise StoreError(
            "UNSUPPORTED_SOURCE_TYPE",
            f"Column {want.name} holds a value its type {kind} cannot carry exactly: {error}.",
            {"column": want.name, "type": kind},
        ) from None
    raise StoreError(
        "UNSUPPORTED_SOURCE_TYPE",
        f"Column {want.name} came back as {source}, not as the exact form of {kind}.",
        {"column": want.name, "type": kind, "arrow_type": str(source)},
    )


def _broken(error: Exception, deadline: float) -> StoreError:
    if time.monotonic() > deadline or isinstance(error, (TimeoutError, socket.timeout)):
        return StoreError("STORE_TIMEOUT", "The ClickHouse statement exceeded its time limit.", {})
    return StoreError(
        "STORE_QUERY_FAILED", f"The ClickHouse result could not be read: {error}.", {}
    )


def _parsed(kind: str) -> DuckDBPyType:
    try:
        return DuckDBPyType(kind)
    except duckdb.Error:
        raise StoreError(
            "UNSUPPORTED_SOURCE_TYPE", f"{kind} has no ClickHouse result form.", {"type": kind}
        ) from None


def _arrow(kind: str) -> pa.DataType:
    if found := _DECIMAL.fullmatch(kind):
        return pa.decimal128(int(found.group(1)), int(found.group(2)))
    if kind in _ARROW:
        return _ARROW[kind]
    raise StoreError(
        "UNSUPPORTED_SOURCE_TYPE", f"{kind} has no ClickHouse result form.", {"type": kind}
    )


def _setting(name: str, expected: str, found: str) -> StoreError:
    return StoreError(
        "ENGINE_SETTING_REQUIRED",
        f"The ClickHouse user's profile must set {name} = {expected} (it is '{found}').",
        {"setting": name, "expected": expected, "found": found},
    )


# --- parameters --------------------------------------------------------------------------------


def _escape(text: str) -> str:
    """A value in ClickHouse's escaped text format, as HTTP parameters are read."""
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _array(items: Sequence[str]) -> str:
    quoted = ("'" + item.replace("\\", "\\\\").replace("'", "\\'") + "'" for item in items)
    return _escape("[" + ",".join(quoted) + "]")


def _engine_parameter_type(kind: str) -> str:
    if found := _DECIMAL.fullmatch(kind):
        return f"Decimal({found.group(1)}, {found.group(2)})"
    if kind in _PARAMETER_TYPES:
        return _PARAMETER_TYPES[kind]
    raise StoreError(
        "STORE_PARAMETER_INVALID",
        f"A {kind} value cannot be bound on ClickHouse.",
        {"type": kind},
    )


def _value(parameter: Parameter) -> str:
    kind = parameter.type.root
    value = parameter.value
    if value is None:
        return "\\N"
    if kind == "TIMESTAMPTZ":
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise StoreError(
                "STORE_PARAMETER_INVALID",
                f"Parameter {parameter.name} is not an ISO 8601 instant.",
                {"parameter": parameter.name},
            ) from None
        if instant.tzinfo is None:
            raise StoreError(
                "STORE_PARAMETER_INVALID",
                f"Parameter {parameter.name} has no offset: an instant needs one.",
                {"parameter": parameter.name},
            )
        return instant.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    if kind == "BOOLEAN":
        if value not in {"true", "false"}:
            raise StoreError(
                "STORE_PARAMETER_INVALID",
                f"Parameter {parameter.name} is not true or false.",
                {"parameter": parameter.name},
            )
        return value
    return _escape(value)


def _bound(sql: str, parameters: Sequence[Parameter]) -> dict[str, str]:
    """The HTTP parameters for `sql`: each placeholder's type must be its parameter's type."""
    given = {parameter.name: parameter for parameter in parameters}
    if len(given) != len(parameters):
        raise StoreError("STORE_PARAMETER_INVALID", "A parameter is given twice.", {})
    used: dict[str, str] = {}
    plain, placeholders = _scan(sql)
    _check_statement(plain)
    for name, written in placeholders:
        used.setdefault(name, written)
        if used[name] != written:
            raise StoreError(
                "STORE_PARAMETER_INVALID",
                f"Parameter {name} is written with two types.",
                {"parameter": name},
            )
    missing, extra = sorted(set(used) - set(given)), sorted(set(given) - set(used))
    if missing or extra:
        raise StoreError(
            "STORE_PARAMETER_INVALID",
            "Every placeholder needs one parameter and every parameter one placeholder.",
            {"missing": ",".join(missing), "extra": ",".join(extra)},
        )
    values: dict[str, str] = {}
    for name, parameter in given.items():
        engine = _engine_parameter_type(parameter.type.root)
        written = used[name]
        if written not in {engine, f"Nullable({engine})"}:
            raise StoreError(
                "STORE_PARAMETER_INVALID",
                f"Parameter {name} is {parameter.type.root}, so it binds as {engine}, "
                f"not {written}.",
                {"parameter": name},
            )
        if parameter.value is None and not written.startswith("Nullable("):
            raise StoreError(
                "STORE_PARAMETER_INVALID",
                f"Parameter {name} is NULL, so its placeholder must be Nullable({engine}).",
                {"parameter": name},
            )
        values[f"param_{name}"] = _value(parameter)
    return values


def _scan(sql: str) -> tuple[str, list[tuple[str, str]]]:
    """The statement with every comment, string, quoted name and placeholder blanked, and its
    placeholders in order. An unclosed comment or quote is refused."""
    plain: list[str] = []
    placeholders: list[tuple[str, str]] = []
    last = 0
    for found in _SCAN.finditer(sql):
        if found.group("open"):
            raise StoreError(
                "STORE_STATEMENT_INVALID", "A ClickHouse read has an unclosed quote or comment.", {}
            )
        plain.append(sql[last : found.start()])
        if found.group("parameter"):
            placeholders.append((found.group("parameter"), found.group("type").strip()))
            plain.append(" 0 ")
        elif found.group("comment"):
            plain.append(" ")
        else:
            plain.append(" '' " if found.group("string") else " x ")
        last = found.end()
    plain.append(sql[last:])
    return "".join(plain), placeholders


def _check_statement(plain: str) -> None:
    """One SELECT, with no clause that changes settings or the output format; `plain` is the
    statement with its comments, strings, names and placeholders blanked."""
    plain = plain.strip()
    if ";" in plain or not re.match(r"(?is)^\(*\s*(SELECT|WITH)\b", plain):
        raise StoreError(
            "STORE_STATEMENT_INVALID", "A ClickHouse read must be one SELECT statement.", {}
        )
    if found := _FORBIDDEN_WORDS.search(plain):
        raise StoreError(
            "STORE_STATEMENT_INVALID",
            f"A ClickHouse read cannot contain {found.group(1).upper()}: the format and the "
            "settings are fixed by the connection.",
            {"clause": found.group(1).upper()},
        )


def describe(target: ClickHouseTarget) -> Mapping[str, Any]:
    """What identifies the engine (ADR-0027 §3: same environment, same engine); no secret."""
    return {
        "kind": "clickhouse",
        "host": target.host.lower(),
        "port": target.port,
        "database": target.database,
    }
