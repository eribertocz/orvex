"""The source isolation boundary (#372, decision #259): a database source read in a process of
its own.

A DuckDB file, a SQLite file or a PostgreSQL database can carry SQL of its own: views, macros
and generated columns. That SQL must never reach the lake, so no database source is attached
to the lake's connection. Each read starts a fresh worker process (`spawn`, never a fork of the
lake's), which opens one DuckDB instance with that one source attached, read-only, and nothing
else: no lake, no catalog, no secret but the source's own. The worker is configured and locked
before any of the source's SQL runs (`_open`, in a fixed order). It answers with one Arrow IPC
stream, whose schema carries the statement's DuckDB types (`orvex.types`, exported losslessly
so that `HUGEINT`, `UHUGEINT`, `UUID` and `BIT` keep their type and value).

The parent (`start`) sends the request on the worker's standard input (the password too, never
on the command line or in the environment), reads the stream from its standard output, and a
refusal as one JSON line from its standard error. It kills the worker when the time limit
passes or the read is closed. A worker that dies, hangs or sends a malformed stream is
`STORE_SOURCE_FAILED`; nothing it sent is kept.

What this does not promise: protection from arbitrary native code run through a DuckDB or
SQLite parser bug by a process of the same OS user (the threat model of #372).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
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
from orvex.store.errors import StoreError, register_secret
from orvex.store.locations import POSTGRES_CONNECT_TIMEOUT_S
from orvex.store.nested_types import refuse_nested_uhugeint
from orvex.store.sql_text import identifier as _identifier
from orvex.store.sql_text import literal as _quote

DatabaseSource = FileSourceAttachment | SqliteSourceAttachment | PostgresSourceAttachment
TYPES_KEY = b"source_types"  # the statement's DuckDB types, in the stream's schema metadata
# The one conversion a source's type gets on the way to the lake, declared (#372 §3): an
# ENUM's values arrive as text.
ENUM_PREFIX = "ENUM("
WORKER_KILL_GRACE_S = 5.0


def memory_bytes(memory_limit: str) -> int:
    """A DuckDB memory limit in bytes, as DuckDB itself parses it."""
    connection = duckdb.connect()
    try:
        connection.execute(f"SET memory_limit = {_quote(memory_limit)}")
        row = connection.execute("SELECT current_setting('memory_limit')").fetchone()
    finally:
        connection.close()
    size = _size_of(str(row[0]) if row else "")
    if size is None:
        raise ValueError(f"memory limit {memory_limit!r}")
    return size


def split_limits(memory_limit: str, threads: int) -> tuple[str, int, str, int]:
    """The lake's and one worker's shares of a lake's limits (#372): half of the memory and of
    the threads each (at least one thread), so a worker and the lake together never use more
    than the lake was given. Returns (lake memory, lake threads, worker memory, worker threads).
    One worker runs at a time per transaction (a land is one statement)."""
    total = memory_bytes(memory_limit)
    worker = total // 2
    worker_threads = max(1, threads // 2)
    return f"{total - worker}B", max(1, threads - worker_threads), f"{worker}B", worker_threads


def is_database(source: object) -> bool:
    """Whether a source carries SQL of its own, and so is read only in a worker."""
    if isinstance(source, SqliteSourceAttachment | PostgresSourceAttachment):
        return True
    return isinstance(source, FileSourceAttachment) and source.type == "duckdb"


def landed_type(kind: str) -> str:
    """The type a source column has in the lake: its own, or VARCHAR for an ENUM."""
    return "VARCHAR" if kind.startswith(ENUM_PREFIX) else kind


# --- the parent ---------------------------------------------------------------------------------


class SourceStream:
    """One worker's answer: `open()` waits for its column names and DuckDB types, then its
    batches are read once. `close()` ends the worker in every case, from any thread, also while
    `open()` waits; using the stream after it fails."""

    def __init__(
        self, process: subprocess.Popen[bytes], timer: threading.Timer, folder: Path
    ) -> None:
        self._process = process
        self._timer = timer
        self.folder = folder  # the worker's folder, the parent's to remove in every outcome
        self._errors = _Drain(process.stderr)
        self.on_close: Callable[[], None] | None = None  # once, when the worker is gone
        self._reader: pa.RecordBatchReader | None = None
        self.timed_out = False
        self.failed: StoreError | None = None  # why the worker's stream ended early
        self.types: list[tuple[str, str]] = []

    def open(self) -> SourceStream:
        """The worker's schema, once it is ready: the statement is bound and running."""
        if self._reader is not None:
            return self
        if self._process.stdout is None:
            raise self._failure()
        try:
            self._reader = pa.ipc.open_stream(self._process.stdout)
        except pa.ArrowException, OSError, ValueError:
            raise self._failure() from None
        raw = (self._reader.schema.metadata or {}).get(TYPES_KEY, b"[]")
        self.types = [(str(n), str(t)) for n, t in json.loads(raw)]
        return self

    @property
    def schema(self) -> pa.Schema:
        if self._reader is None:
            raise self._failure()
        return self._reader.schema

    def batches(self) -> Iterator[pa.RecordBatch]:
        if self._reader is None:
            raise self._failure()
        try:
            yield from self._reader
        except pa.ArrowException, OSError, ValueError:
            raise self._failure() from None
        self._finish()

    def reader(self) -> pa.RecordBatchReader:
        return pa.RecordBatchReader.from_batches(self.schema, self.batches())

    def _finish(self) -> None:
        code = self._process.wait()
        self._timer.cancel()
        if code != 0:
            raise self._failure()
        self.close()

    def _failure(self) -> StoreError:
        if self.failed is None:
            _kill(self._process)
            self._timer.cancel()
            self.failed = _worker_error(self._errors.text(), self.timed_out)
            self.close()
        return self.failed

    def close(self) -> None:
        """End the worker and remove its folder (a killed worker cleans nothing itself)."""
        self._timer.cancel()
        _kill(self._process)
        self._errors.join()
        shutil.rmtree(self.folder, ignore_errors=True)
        released, self.on_close = self.on_close, None
        if released is not None:
            released()


def _kill(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=WORKER_KILL_GRACE_S)
    for stream in (process.stdout, process.stdin):
        if stream is not None and not stream.closed:
            stream.close()


class _Drain:
    """The worker's standard error, read as it comes in a thread of its own (so a chatty
    worker never blocks on a full pipe), keeping only its last bytes."""

    KEPT = 64 * 1024

    def __init__(self, stream: IO[bytes] | None) -> None:
        self._kept = bytearray()
        self._thread: threading.Thread | None = None
        if stream is not None:
            self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
            self._thread.start()

    def _read(self, stream: IO[bytes]) -> None:
        with contextlib.suppress(OSError, ValueError):
            for chunk in iter(lambda: stream.read1(4096), b""):  # type: ignore[attr-defined]
                self._kept += chunk
                del self._kept[: max(0, len(self._kept) - self.KEPT)]
        with contextlib.suppress(OSError):
            stream.close()

    def join(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=WORKER_KILL_GRACE_S)

    def text(self) -> str:
        self.join()
        return bytes(self._kept).decode("utf-8", "replace")


def _worker_error(text: str, timed_out: bool) -> StoreError:
    """The worker's own refusal, when it sent one; otherwise `STORE_TIMEOUT` (killed at the
    limit) or `STORE_SOURCE_FAILED` (it died, or its stream is malformed)."""
    if timed_out:
        return StoreError("STORE_TIMEOUT", "Reading the source took longer than allowed.", {})
    for line in reversed(text.splitlines()):
        try:
            found = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(found, dict) and isinstance(found.get("code"), str):
            raw = found.get("details")
            details: dict[str, object] = raw if isinstance(raw, dict) else {}
            return StoreError(
                found["code"],
                str(found.get("message", "")),
                {str(k): str(v) for k, v in details.items()},
            )
    return StoreError(
        "STORE_SOURCE_FAILED",
        "The process reading the source failed before it finished; nothing was kept.",
        {},
    )


def start(
    source: DatabaseSource,
    password: str,
    sql: str,
    parameters: Sequence[Parameter],
    *,
    memory_limit: str,
    threads: int,
    timeout_ms: int,
    batch_size: int,
    folder: Path,
    describe: bool = False,
) -> SourceStream:
    """A worker started on reading `sql` from `source`; `open()` the stream for its schema.
    `folder`: where the worker's own folder is made (the lake's private spill folder), removed
    by the parent in every outcome. `describe`: only the columns and types, no row."""
    own = Path(tempfile.mkdtemp(prefix="source-", dir=folder))  # 0700, the parent's
    request = {
        "folder": own.as_posix(),
        # A PostgreSQL source's TLS keywords, resolved here: the worker's environment holds no
        # variable, a certificate authority's included.
        "tls": _postgres_tls(source),
        "source": source.model_dump(mode="json", by_alias=True),
        "password": password,
        "sql": sql,
        "parameters": [p.model_dump(mode="json") for p in parameters],
        "memory_limit": memory_limit,
        "threads": int(threads),
        "batch_size": int(batch_size),
        "describe": describe,
    }
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-m", __name__],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            env=_environment(),
        )
    except BaseException:
        shutil.rmtree(own, ignore_errors=True)
        raise
    holder: list[SourceStream] = []

    def expire() -> None:
        holder[0].timed_out = True
        if process.poll() is None:
            process.kill()

    timer = threading.Timer(timeout_ms / 1000, expire)
    timer.daemon = True
    stream = SourceStream(process, timer, own)
    holder.append(stream)
    timer.start()
    try:
        if process.stdin is None:
            raise OSError("no input")
        process.stdin.write(json.dumps(request).encode())
        process.stdin.close()
    except OSError:
        raise stream._failure() from None
    return stream


def _postgres_tls(source: DatabaseSource) -> str:
    if not isinstance(source, PostgresSourceAttachment):
        return ""
    from orvex.store import lake as lake_module

    return lake_module._source_tls(source)


def _environment() -> dict[str, str]:
    """The worker's environment: what Python and DuckDB need to start, and nothing secret."""
    kept = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "LANG", "LC_ALL", "PYTHONPATH")
    found = {name: os.environ[name] for name in kept if name in os.environ}
    for name, value in os.environ.items():  # DuckDB's extension folder, set by CI and tests
        if name.startswith("DUCKDB_") and "SECRET" not in name:
            found[name] = value
    return found


# --- the worker ---------------------------------------------------------------------------------


def _source_of(document: Mapping[str, Any]) -> DatabaseSource:
    kind = document.get("type")
    if kind == "sqlite":
        return SqliteSourceAttachment.model_validate(document)
    if kind == "postgres":
        return PostgresSourceAttachment.model_validate(document)
    return FileSourceAttachment.model_validate(document)


def _size_of(text: str) -> int | None:
    units = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4, "B": 1}
    parts = text.split()
    if len(parts) != 2 or parts[1] not in units:
        return None
    try:
        return int(float(parts[0]) * units[parts[1]])
    except ValueError:
        return None


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
    from orvex.store import lake as lake_module

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
            raise lake_module._source_not_found(source)
        _attach(source, connection, f"ATTACH {_quote(source.path)} AS {name} (READ_ONLY)")
        readable.append(path.as_posix())
    connection.execute(f"USE {name}")
    row = connection.execute(lake_module.TABLE_FUNCTIONS_SQL).fetchone()
    text = str(row[0]) if row and row[0] is not None else ""
    table_functions = frozenset(text.split("|")) - {""} - lake_module.SHARED_HARMLESS_FUNCTIONS
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


def sqlite_backup(source: SqliteSourceAttachment, copy: Path) -> None:
    """A consistent copy of the SQLite file at `copy`, WAL included, by SQLite's backup API:
    pages only, no statement and no schema SQL is run on the original."""
    from orvex.store import lake as lake_module

    path = Path(source.path)
    if not path.is_file():
        raise lake_module._source_not_found(source)
    uri = path.absolute().as_uri() + "?mode=ro"
    try:
        original = sqlite3.connect(uri, uri=True)
        target = sqlite3.connect(copy)
        try:
            original.backup(target)
        finally:
            original.close()
            target.close()
    except sqlite3.Error as error:
        raise StoreError(
            "STORE_SOURCE_INVALID",
            f"Source {source.name} is not a readable SQLite database.",
            {"source": source.name, "sqlite": str(error)},
        ) from None


def _sqlite_copy(source: SqliteSourceAttachment, copy: Path) -> list[tuple[str, str, str]]:
    """A private copy of the lake's copy of the SQLite file, in the worker's folder, and (table,
    column, declared type) of its tables, read here in the worker. Its views stay: here they
    reach nothing but the copy."""
    sqlite_backup(source, copy)
    found: list[tuple[str, str, str]] = []
    try:
        target = sqlite3.connect(copy)
        try:
            tables = target.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
            for (table,) in list(tables):
                for column, kind in target.execute(
                    "SELECT name, type FROM pragma_table_info(?)", (table,)
                ):
                    found.append((str(table), str(column), str(kind or "")))
        finally:
            target.close()
    except sqlite3.Error as error:
        raise StoreError(
            "STORE_SOURCE_INVALID",
            f"Source {source.name} is not a readable SQLite database.",
            {"source": source.name, "sqlite": str(error)},
        ) from None
    return found


def _postgres_inexact(
    source: PostgresSourceAttachment, connection: duckdb.DuckDBPyConnection
) -> set[tuple[str, str, str, str]]:
    from orvex.store import lake as lake_module

    scope = "" if not source.schema_ else f" AND n.nspname = {_quote(source.schema_)}"
    inner = lake_module.postgres_inexact_sql(scope)
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
    from orvex.store import lake as lake_module

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
        sql = lake_module._plain(str(request["sql"]))
        statement = lake_module._single_statement(sql, frozenset(table_functions))
        if statement.type != duckdb.StatementType.SELECT:
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A source is read with one SELECT.",
                {"statement": statement.type.name},
            )
        bound = lake_module._bound(statement, parameters)
        typed = lake_module._typed(sql, parameters)
        entries = frozenset(
            lake_module.Inexact(source.name, schema, table, column, label)
            for schema, table, column, label in inexact
        )
        try:
            column = lake_module._inexact_column_read(connection, typed, bound, entries)
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
