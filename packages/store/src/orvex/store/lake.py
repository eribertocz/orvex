"""DuckLake lake with a local DuckDB-file catalog (contract `store` v1, P1-01).

Writes run inside DuckDB in explicit transactions; each read is pinned to a snapshot by adding
`AT (VERSION => n)` to every lake table it references (rewritten with DuckDB's own parser) and is
read once, as an Arrow record-batch stream (ADR-0025). No row passes through Python.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pyarrow as pa
from orvex.contracts.store.v1 import (
    CommitResult,
    LakeLocation,
    Parameter,
    ReadRequest,
    Snapshot,
    StatementRequest,
)
from orvex.store.errors import StoreError

LAKE = "lake"
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
    """DuckDB resource limits; both are required (law 7)."""

    memory_limit: str | None
    threads: int | None


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _single_statement(sql: str) -> duckdb.Statement:
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

    def __init__(self, location: LakeLocation, limits: Limits) -> None:
        if not limits.memory_limit or not limits.threads:
            raise StoreError(
                "STORE_LIMITS_REQUIRED",
                "The lake needs an explicit memory limit and thread count.",
                {},
            )
        try:
            self._connection = duckdb.connect(
                config={"memory_limit": limits.memory_limit, "threads": int(limits.threads)}
            )
        except (duckdb.Error, ValueError) as error:
            raise StoreError(
                "STORE_LIMITS_REQUIRED",
                "The memory limit or the thread count is not valid.",
                {"duckdb": str(error)},
            ) from error
        self._write_lock = threading.Lock()
        self._local = threading.local()
        try:
            self._prepare(Path(location.path))
        except BaseException:
            self._connection.close()
            raise

    def _prepare(self, folder: Path) -> None:
        for extension in EXTENSIONS:
            try:
                self._connection.install_extension(extension)
                self._connection.load_extension(extension)
            except duckdb.Error as error:
                raise StoreError(
                    "STORE_EXTENSION_UNAVAILABLE",
                    f"The DuckDB extension {extension} could not be installed or loaded.",
                    {"extension": extension, "duckdb": str(error)},
                ) from error
        (folder / "data").mkdir(parents=True, exist_ok=True)
        catalog = (folder / "catalog.ducklake").as_posix()
        data = (folder / "data").as_posix() + "/"
        # Paths come from the project configuration, not from query input; quoted as literals.
        self._connection.sql(
            f"ATTACH {_quote('ducklake:' + catalog)} AS {LAKE} (DATA_PATH {_quote(data)})"
        )

    def close(self) -> None:
        self._connection.close()

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        cursor = self._connection.cursor()
        cursor.sql(f"USE {LAKE}")
        # TIMESTAMPTZ values reach Arrow as instants in UTC whatever the machine's zone;
        # presenting them in a local zone is a later, explicit step.
        cursor.sql("SET TimeZone = 'UTC'")
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

    def _other_catalogs(self) -> set[str]:
        row = self._one(
            "SELECT string_agg(lower(database_name), '|') FROM duckdb_databases() "
            "WHERE lower(database_name) <> $lake",
            {"lake": LAKE},
        )
        text = str(row[0]) if row and row[0] is not None else ""
        return set(text.split("|")) - {""}

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
        others = self._other_catalogs()
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
            if (len(path) == 3 and path[0].lower() != LAKE) or (
                len(path) == 2 and path[0].lower() in others
            ):
                raise StoreError(
                    "STORE_STATEMENT_INVALID",
                    "A read may reference only lake tables.",
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
        statement = _single_statement(request.sql)
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
        cursor = self._cursor()
        try:
            source = cursor.sql(sql, params=parameters).to_arrow_reader(request.batch_size)
        except duckdb.Error as error:
            cursor.close()
            raise StoreError(
                "STORE_QUERY_FAILED", "DuckDB could not run the read.", {"duckdb": str(error)}
            ) from error

        schema = _utc_schema(source.schema)

        def batches() -> Iterator[pa.RecordBatch]:
            # The cursor lives as long as the stream; it is closed when the stream ends.
            try:
                for batch in source:
                    yield batch if batch.schema == schema else batch.cast(schema)
            except (OSError, pa.ArrowException, duckdb.Error) as error:
                raise StoreError(
                    "STORE_QUERY_FAILED",
                    "DuckDB failed while streaming the read.",
                    {"duckdb": str(error)},
                ) from error
            finally:
                cursor.close()

        return pa.RecordBatchReader.from_batches(schema, batches())

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
        with self._write_lock:
            self._local.writing = True
            cursor = self._cursor()
            try:
                before = self._snapshot(None)
                cursor.begin()
                handle = Transaction(cursor)
                try:
                    yield handle
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


class Transaction:
    """A write transaction; `result` is set when the block commits."""

    def __init__(self, cursor: duckdb.DuckDBPyConnection) -> None:
        self._cursor = cursor
        self.result: CommitResult | None = None

    def execute(self, request: StatementRequest) -> None:
        statement = _single_statement(request.sql)
        if statement.type not in WRITE_STATEMENTS:
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "This statement kind is not allowed in a transaction.",
                {"statement": statement.type.name},
            )
        parameters = _bound(statement, request.parameters)
        try:
            self._cursor.sql(_typed(request.sql, request.parameters), params=parameters)
        except duckdb.Error as error:
            raise StoreError(
                "STORE_QUERY_FAILED", "DuckDB could not run the statement.", {"duckdb": str(error)}
            ) from error


type JsonTree = dict[str, object]


def _walk(node: object) -> Iterator[dict[str, object]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _utc_schema(schema: pa.Schema) -> pa.Schema:
    """Name every zoned timestamp's zone `UTC` (DuckDB may say `Etc/UTC`); values are unchanged."""
    fields = [
        field.with_type(pa.timestamp(field.type.unit, tz="UTC"))
        if pa.types.is_timestamp(field.type) and field.type.tz is not None
        else field
        for field in schema
    ]
    return pa.schema(fields, metadata=schema.metadata)


def open_lake(location: LakeLocation, limits: Limits) -> Lake:
    return Lake(location, limits)
