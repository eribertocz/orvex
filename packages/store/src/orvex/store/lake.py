"""DuckLake lake with a local DuckDB-file catalog (contract `store` v1, P1-01).

Writes run inside DuckDB in explicit transactions; each read is pinned to a snapshot by adding
`AT (VERSION => n)` to every lake table it references (rewritten with DuckDB's own parser) and is
read once, as an Arrow record-batch stream (ADR-0025). No row passes through Python.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
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
EXTENSIONS = ("ducklake",)
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


def _converted(parameter: Parameter) -> object:
    """The Python value DuckDB binds exactly for the declared type, from its text."""
    value = parameter.value
    kind = parameter.type.root
    if value is None:
        return None
    try:
        if kind.startswith("DECIMAL"):
            return decimal.Decimal(value)
        if kind in INTEGER_RANGES:
            number = int(value, 10)
            low, high = INTEGER_RANGES[kind]
            if not low <= number <= high:
                raise ValueError(f"out of range for {kind}")
            return number
        if kind == "BOOLEAN":
            if value not in {"true", "false"}:
                raise ValueError("expected true or false")
            return value == "true"
        if kind == "DOUBLE":
            return float(value)
        if kind == "DATE":
            return dt.date.fromisoformat(value)
        if kind == "TIMESTAMP":
            moment = dt.datetime.fromisoformat(value)
            if moment.tzinfo is not None:
                raise ValueError("TIMESTAMP takes no time zone")
            return moment
        if kind == "TIMESTAMPTZ":
            moment = dt.datetime.fromisoformat(value)
            if moment.tzinfo is None:
                raise ValueError("TIMESTAMPTZ needs a time zone offset")
            return moment
        return value
    except (ValueError, decimal.InvalidOperation) as error:
        raise StoreError(
            "STORE_PARAMETER_INVALID",
            f"Parameter {parameter.name} is not a valid {kind}.",
            {"parameter": parameter.name, "reason": str(error)},
        ) from error


def _bound(statement: duckdb.Statement, parameters: list[Parameter]) -> dict[str, object]:
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
    for parameter in parameters:
        if parameter.type.root.startswith("DECIMAL") and parameter.value is not None:
            precision, scale = (int(part) for part in parameter.type.root[8:-1].split(","))
            if scale > precision:
                raise StoreError(
                    "STORE_PARAMETER_INVALID",
                    f"Parameter {parameter.name}: scale exceeds precision.",
                    {"parameter": parameter.name},
                )
    return {parameter.name: _converted(parameter) for parameter in parameters}


class Lake:
    """One open lake. Thread-safe: each call uses its own cursor on a shared database."""

    def __init__(self, location: LakeLocation, limits: Limits) -> None:
        if not limits.memory_limit or not limits.threads:
            raise StoreError(
                "STORE_LIMITS_REQUIRED",
                "The lake needs an explicit memory limit and thread count.",
                {},
            )
        folder = Path(location.path)
        (folder / "data").mkdir(parents=True, exist_ok=True)
        self._connection = duckdb.connect(
            config={"memory_limit": limits.memory_limit, "threads": int(limits.threads)}
        )
        self._lock = threading.Lock()
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
        catalog = (folder / "catalog.ducklake").as_posix()
        data = (folder / "data").as_posix() + "/"
        # Paths come from the project configuration, not from query input; quoted as literals.
        self._connection.sql(
            f"ATTACH {_quote('ducklake:' + catalog)} AS {LAKE} (DATA_PATH {_quote(data)})"
        )
        self._connection.sql(f"USE {LAKE}")

    def close(self) -> None:
        self._connection.close()

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        cursor = self._connection.cursor()
        cursor.sql(f"USE {LAKE}")
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

    def _pinned(self, sql: str, snapshot: int) -> str:
        """The same SELECT with `AT (VERSION => snapshot)` on every lake table it references."""
        serialized = self._one("SELECT json_serialize_sql($sql)", {"sql": sql})
        tree = json.loads(str(serialized[0])) if serialized else {"error": True}
        if tree.get("error"):
            raise StoreError(
                "STORE_QUERY_FAILED",
                "The SQL could not be parsed.",
                {"duckdb": str(tree.get("error_message", ""))},
            )
        names = _cte_names(tree)
        for table in _base_tables(tree):
            qualified = table.get("qualified_name")
            raw = qualified.get("path") if isinstance(qualified, dict) else None
            path = (
                [str(part) for part in raw] if isinstance(raw, list) else [str(table["table_name"])]
            )
            if len(path) == 1 and path[0] in names:
                continue
            if len(path) == 3 and path[0] != LAKE:
                raise StoreError(
                    "STORE_STATEMENT_INVALID",
                    "A read may reference only lake tables.",
                    {"table": ".".join(path)},
                )
            table["at_clause"] = {
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
        sql = self._pinned(request.sql, request.snapshot)
        cursor = self._cursor()
        try:
            source = cursor.sql(sql, params=parameters).to_arrow_reader(request.batch_size)
        except duckdb.Error as error:
            cursor.close()
            raise StoreError(
                "STORE_QUERY_FAILED", "DuckDB could not run the read.", {"duckdb": str(error)}
            ) from error

        def batches() -> Iterator[pa.RecordBatch]:
            # The cursor lives as long as the stream; it is closed when the stream ends.
            try:
                yield from source
            finally:
                cursor.close()

        return pa.RecordBatchReader.from_batches(source.schema, batches())

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """Statements inside the block commit together to one snapshot, or roll back on error."""
        cursor = self._cursor()
        before = self._snapshot(None)
        cursor.begin()
        handle = Transaction(cursor)
        try:
            yield handle
        except BaseException:
            cursor.rollback()
            cursor.close()
            raise
        try:
            cursor.commit()
        except duckdb.Error as error:
            cursor.rollback()
            cursor.close()
            raise StoreError(
                "STORE_QUERY_FAILED", "The transaction could not commit.", {"duckdb": str(error)}
            ) from error
        after = self._snapshot(None)
        cursor.close()
        changed = after is not None and (before is None or after.id != before.id)
        handle.result = CommitResult(kind="commit_result", snapshot=after if changed else None)


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
            self._cursor.sql(request.sql, params=parameters)
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


def _base_tables(tree: JsonTree) -> list[dict[str, object]]:
    return [node for node in _walk(tree) if node.get("type") == "BASE_TABLE"]


def _cte_names(tree: JsonTree) -> set[str]:
    names: set[str] = set()
    for node in _walk(tree):
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            for entry in cte_map.get("map", []):
                if isinstance(entry, dict) and isinstance(entry.get("key"), str):
                    names.add(entry["key"])
    return names


def open_lake(location: LakeLocation, limits: Limits) -> Lake:
    return Lake(location, limits)
