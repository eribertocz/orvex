"""The source worker's own side (#372): the process `source_worker.start` runs as
`python -I -m orvex.store.source_reader`, which opens one DuckDB instance with one database
source attached read-only, configured and locked before any of the source's SQL runs, and
answers one statement as an Arrow IPC stream on its standard output (a refusal as one JSON line
on its standard error).
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

import duckdb
import pyarrow as pa
from orvex.contracts.store.v1 import (
    FileSourceAttachment,
    Parameter,
    PostgresSourceAttachment,
    SqliteSourceAttachment,
)
from orvex.store.errors import StoreError, _source_not_found, register_secret
from orvex.store.locations import POSTGRES_CONNECT_TIMEOUT_S
from orvex.store.nested_types import refuse_nested_uhugeint
from orvex.store.parameters import _bound, _typed
from orvex.store.source_exactness import Inexact, _inexact_column_read, postgres_inexact_sql
from orvex.store.source_worker import TYPES_KEY, DatabaseSource
from orvex.store.sql_guard import SHARED_HARMLESS_FUNCTIONS, TABLE_FUNCTIONS_SQL, _single_statement
from orvex.store.sql_parser import _plain
from orvex.store.sql_text import identifier as _identifier
from orvex.store.sql_text import literal as _quote
from orvex.store.sqlite_copy import _sqlite_copy


def _source_of(document: Mapping[str, Any]) -> DatabaseSource:
    kind = document.get("type")
    if kind == "sqlite":
        return SqliteSourceAttachment.model_validate(document)
    if kind == "postgres":
        return PostgresSourceAttachment.model_validate(document)
    return FileSourceAttachment.model_validate(document)


def _open(
    source: DatabaseSource,
    password: str,
    memory_limit: str,
    threads: int,
    folder: Path,
    tls: str = "",
) -> tuple[duckdb.DuckDBPyConnection, frozenset[tuple[str, str, str, str]], list[str]]:
    """The worker's one DuckDB instance, in this order (#372 §1), each step before any SQL of the
    source runs: limits and a private spill folder; UTC and lossless Arrow; the source's own
    extension only, then no loading at all; the source attached read-only; the sandbox
    (only the source's file), then the configuration locked. Returns the connection, the
    inexact columns `(schema, table, column, label)` and the table functions to refuse."""
    spill = folder / "spill"
    spill.mkdir()
    connection = duckdb.connect(
        config={
            "memory_limit": memory_limit,
            "threads": int(threads),
            "allow_persistent_secrets": False,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        }
    )
    connection.execute(f"SET temp_directory = {_quote(spill.as_posix())}")
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("SET arrow_lossless_conversion = true")
    inexact: set[tuple[str, str, str, str]] = set()
    readable: list[str] = []
    name = _identifier(source.name)
    if isinstance(source, SqliteSourceAttachment):
        copy = folder / "source.sqlite"
        for table, column, kind in _sqlite_copy(source, copy):
            if re.search(r"DEC|NUM", kind, re.IGNORECASE):
                inexact.add(("", table.lower(), column.lower(), f"{table}.{column}"))
        _load(connection, "sqlite")
        _attach(
            source,
            connection,
            f"ATTACH {_quote(copy.as_posix())} AS {name} (TYPE sqlite, READ_ONLY)",
        )
        readable.append(copy.as_posix())
    elif isinstance(source, PostgresSourceAttachment):
        _load(connection, "postgres")
        secret = _identifier(f"source_{source.name}")
        _connect(
            source,
            connection,
            f"CREATE TEMPORARY SECRET {secret} (TYPE postgres, HOST {_quote(source.host)}, "
            f"PORT {int(source.port)}, DATABASE {_quote(source.database)}, "
            f"USER {_quote(source.user)}, PASSWORD {_quote(password)})",
        )
        conninfo = (
            f"connect_timeout={POSTGRES_CONNECT_TIMEOUT_S} "
            "options='-c default_transaction_read_only=on'" + tls
        )
        options = ["TYPE postgres", f"SECRET {secret}", "READ_ONLY"]
        if source.schema_:
            options.append(f"SCHEMA {_quote(source.schema_)}")
        _connect(source, connection, f"ATTACH {_quote(conninfo)} AS {name} ({', '.join(options)})")
        inexact |= _postgres_inexact(source, connection)
    else:
        path = Path(source.path)
        if not path.is_file():
            raise _source_not_found(source)
        _attach(source, connection, f"ATTACH {_quote(source.path)} AS {name} (READ_ONLY)")
        readable.append(path.as_posix())
    connection.execute(f"USE {name}")
    row = connection.execute(TABLE_FUNCTIONS_SQL).fetchone()
    text = str(row[0]) if row and row[0] is not None else ""
    table_functions = frozenset(text.split("|")) - {""} - SHARED_HARMLESS_FUNCTIONS
    connection.execute(f"SET allowed_directories = [{_quote(spill.as_posix() + '/')}]")
    if readable:
        connection.execute(f"SET allowed_paths = [{', '.join(map(_quote, readable))}]")
    connection.execute("SET enable_external_access = false")
    connection.execute("SET lock_configuration = true")
    return connection, frozenset(inexact), sorted(table_functions)


def _load(connection: duckdb.DuckDBPyConnection, extension: str) -> None:
    try:
        connection.install_extension(extension)
        connection.load_extension(extension)
    except duckdb.Error as error:
        raise StoreError(
            "STORE_EXTENSION_UNAVAILABLE",
            f"The DuckDB extension {extension} could not be installed or loaded.",
            {"extension": extension, "duckdb": str(error)},
        ) from None


def _attach(source: DatabaseSource, connection: duckdb.DuckDBPyConnection, sql: str) -> None:
    try:
        connection.execute(sql)
    except duckdb.Error as error:
        raise StoreError(
            "STORE_SOURCE_INVALID",
            f"Source {source.name} could not be attached.",
            {"source": source.name, "duckdb": str(error)},
        ) from None


def _connect(
    source: PostgresSourceAttachment, connection: duckdb.DuckDBPyConnection, sql: str
) -> None:
    try:
        connection.execute(sql)
    except duckdb.Error as error:
        raise StoreError(
            "STORE_CONNECTION_FAILED",
            f"Source {source.name} could not be reached or refused the credentials.",
            {"source": source.name, "duckdb": str(error)},
        ) from None


def _postgres_inexact(
    source: PostgresSourceAttachment, connection: duckdb.DuckDBPyConnection
) -> set[tuple[str, str, str, str]]:
    scope = "" if not source.schema_ else f" AND n.nspname = {_quote(source.schema_)}"
    inner = postgres_inexact_sql(scope)
    try:
        row = connection.execute(
            "SELECT list([table_schema, table_name, column_name]) FROM postgres_query($name, $sql)",
            {"name": source.name, "sql": inner},
        ).fetchone()
    except duckdb.Error as error:
        raise StoreError(
            "STORE_CONNECTION_FAILED",
            f"Source {source.name} could not be inspected.",
            {"source": source.name, "duckdb": str(error)},
        ) from None
    found: set[tuple[str, str, str, str]] = set()
    for schema, table, column in row[0] if row and isinstance(row[0], list) else []:
        label = f"{table}.{column}" if source.schema_ else f"{schema}.{table}.{column}"
        found.add((str(schema).lower(), str(table).lower(), str(column).lower(), label))
    return found


def _run(request: Mapping[str, Any], out: IO[bytes]) -> None:
    source = _source_of(request["source"])
    password = str(request.get("password") or "")
    register_secret(password)
    folder = Path(str(request["folder"]))
    try:
        connection, inexact, table_functions = _open(
            source,
            password,
            str(request["memory_limit"]),
            int(request["threads"]),
            folder,
            str(request.get("tls") or ""),
        )
        parameters = [Parameter.model_validate(p) for p in request["parameters"]]
        sql = _plain(str(request["sql"]))
        statement = _single_statement(sql, frozenset(table_functions))
        if statement.type != duckdb.StatementType.SELECT:
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A source is read with one SELECT.",
                {"statement": statement.type.name},
            )
        bound = _bound(statement, parameters)
        typed = _typed(sql, parameters)
        entries = frozenset(
            Inexact(source.name, schema, table, column, label)
            for schema, table, column, label in inexact
        )
        try:
            column = _inexact_column_read(connection, typed, bound, entries)
            if column is not None:
                raise StoreError(
                    "UNSUPPORTED_SOURCE_TYPE",
                    f"Column {column} has no exact type in DuckDB (law 11); cast it at the "
                    "source or leave it out.",
                    {"column": column},
                )
            relation = connection.sql(typed, params=bound)
            names = list(relation.columns)
            refuse_nested_uhugeint(names, relation.types)
            kinds = [str(kind) for kind in relation.types]
            if request.get("describe"):
                relation = connection.sql(f"SELECT * FROM ({typed}) LIMIT 0", params=bound)
            reader = relation.to_arrow_reader(int(request["batch_size"]))
        except duckdb.Error as error:
            raise StoreError(
                "STORE_QUERY_FAILED",
                f"Source {source.name} could not run the statement.",
                {"source": source.name, "duckdb": str(error)},
            ) from None
        metadata = {TYPES_KEY: json.dumps(list(zip(names, kinds, strict=True))).encode()}
        schema = reader.schema.with_metadata({**(reader.schema.metadata or {}), **metadata})
        with pa.ipc.new_stream(out, schema) as writer:
            try:
                for batch in reader:
                    writer.write_batch(batch)
            except duckdb.Error as error:
                raise StoreError(
                    "STORE_QUERY_FAILED",
                    f"Source {source.name} failed while it was read.",
                    {"source": source.name, "duckdb": str(error)},
                ) from None
        out.flush()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main() -> int:
    request = json.loads(sys.stdin.buffer.read() or b"{}")
    try:
        _run(request, sys.stdout.buffer)
    except StoreError as error:
        refusal = {"code": error.code, "message": error.message, "details": error.details}
        sys.stderr.write(json.dumps(refusal) + "\n")
        sys.stderr.flush()
        return 2
    except MemoryError:
        sys.stderr.write(
            json.dumps(
                {
                    "code": "STORE_SOURCE_FAILED",
                    "message": "Reading the source needed more memory than allowed.",
                    "details": {},
                }
            )
            + "\n"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
