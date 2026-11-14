"""An existing DuckLake for a security fixture, as another system owns it (#441, #447; ADR-0027
§3): built from the fixture's `lake.toml`, and changed as its owner, or an attacker on its host,
could change it (the cases' `lake-change` steps).

The lake lives on a file system of its own (`/dev/shm`), with a SQLite or a PostgreSQL catalog.
The instance's protected set stands apart from it, as the server builds one: a project folder,
the managed lake's data folder and catalog file, a Route A source file and another project's
managed-lake data (`Host.attachment`, rebuilt as the set stands at each verification).

`change` makes one change of a case and says what meets it next: the lake's schedule verifying
it again; the read alone (a link swapped in **after** verification); or the connection declared
again and attached anew (a location changed):

- `create_view`, `create_macro`: the owner creates one in the lake (`foreign` in a view's SQL
  is the foreign rows' file);
- `add_file`: a Parquet file of the foreign rows registered as a data file of the lake's table,
  `where` it lies: `inside_location` (the lake's own, as an owner adds one), `outside_location`,
  `symbolic_link_outside` (a link in the location to a file outside), `route_a_file` (a Route A
  source's file), `other_project_file` (another project's managed-lake data), or
  `hard_link_to_managed` (a managed-lake file on the lake's file system, linked in);
- `swap_file_for_link`: the table's data file replaced by a link to the foreign rows' file;
- `register_location_inside`: a managed-lake location registered inside the lake's location;
- `set_location`: the lake's declared location made to contain a managed lake's (`contains`):
  a managed-lake data location inside it;
- `replace_catalog_file`: the SQLite catalog replaced by a link to a copy of it elsewhere;
- `postgres_catalog`: the PostgreSQL catalog's reader role allowed to read another schema;
- `restore_catalog_reusing_id`: a backup taken, one more commit served, the catalog restored
  from the backup, then the owner's next commit (`delete_rows_where`), which reuses the id
  just served with another commit time.

A change a catalog cannot have (a SQLite file on PostgreSQL, a role on SQLite) raises
`NotApplicable`.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import duckdb
from orvex.contracts.store.v1 import (
    ExistingLakeAttachment,
    PostgresLakeLocation,
    ProtectedFile,
    ProtectedLocation,
    SqliteLakeLocation,
)

from conformance.runners import security_fixtures as sf

POSTGRES_PASSWORD = "STORE_TEST_POSTGRES_PASSWORD"
NAME = "warehouse"  # the connection's name


class NotApplicable(RuntimeError):
    """A change this lake's catalog cannot have."""


def sql_value(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


@dataclass
class Host:
    """The lake as its owner holds it, the host it lives on, and the instance's protected set."""

    location: SqliteLakeLocation | PostgresLakeLocation
    root: Path  # the lake's own file system: its catalog (SQLite), its data, and other files
    data: Path
    protected: Path  # the instance's own file system
    owner: Callable[[str], None]  # one statement, run by the owner in its own transaction
    catalog: Callable[[str], None]  # one statement on the catalog database itself
    foreign: Path  # a Parquet file of the foreign rows, outside the location
    table: str  # the table the foreign rows are shaped for
    backup: Callable[[], Callable[[], None]]  # the catalog's state; called back, restores it
    locations: list[Path] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)

    def attachment(self) -> ExistingLakeAttachment:
        """The attachment as the instance builds it now, with its protected set."""
        return ExistingLakeAttachment(
            kind="existing_lake_attachment",
            name=NAME,
            location=self.location,
            roles_in_use=[],
            protected_files=[ProtectedFile(str(path)) for path in self.files],
            protected_locations=[ProtectedLocation(str(path)) for path in self.locations],
        )


def _postgres(unavailable: Callable[[str, str], NoReturn]) -> dict[str, str]:
    connection = os.environ.get("STORE_TEST_POSTGRES")
    if not connection or not os.environ.get(POSTGRES_PASSWORD):
        unavailable(
            "no PostgreSQL configured for this run",
            "STORE_TEST_POSTGRES and STORE_TEST_POSTGRES_PASSWORD are required in CI",
        )
    return dict(pair.split("=", 1) for pair in connection.split())


def _execute(writer: duckdb.DuckDBPyConnection, database: str, sql: str) -> None:
    quoted = sql.replace("'", "''")
    writer.execute(f"CALL postgres_execute('{database}', '{quoted}', use_transaction := false)")


def _snapshots(found: sf.Fixture) -> list[list[str]]:
    """The owner's statements, one list per snapshot of `lake.toml`, in order."""
    settings = found.lake_settings()
    created: set[str] = set()
    statements: list[list[str]] = []
    for entry in settings["snapshots"]:
        table = entry["table"]
        columns = found.catalog[table]
        rows = list(csv.reader(io.StringIO((found.folder / entry["file"]).read_text("utf-8"))))
        step = []
        if table not in created:
            listed = ", ".join(f'"{c}" {kind.type}' for c, kind in columns.items())
            step.append(f'CREATE TABLE l."{table}" ({listed})')
            created.add(table)
        else:
            step.append(f'DELETE FROM l."{table}"')
        for row in rows[1:]:
            values = ", ".join(sql_value(None if v == "NULL" else v) for v in row)
            step.append(f'INSERT INTO l."{table}" VALUES ({values})')
        statements.append(step)
    return statements


def _protected(folder: Path) -> tuple[list[Path], list[Path]]:
    """A protected set as the server builds it, on the instance's file system: a project
    folder, its managed lake's data folder and catalog file, a Route A source file and another
    project's managed-lake data folder."""
    project, managed, sources = folder / "project", folder / "managed", folder / "sources"
    other = folder / "other-project" / "lake" / "data"
    for made in (project, managed / "data", sources, other):
        made.mkdir(parents=True)
    catalog = managed / "catalog.ducklake"
    catalog.write_bytes(b"managed")
    source = sources / "outages.parquet"
    source.write_bytes(b"source")
    return [project, managed / "data", catalog, source, other], [catalog, source]


@contextmanager
def existing_lake(
    found: sf.Fixture, catalog: str, unavailable: Callable[[str, str], NoReturn]
) -> Iterator[Host]:
    """An existing DuckLake that another system owns and writes, on `catalog`, holding the
    fixture's datasets; its data on a file system of its own (ADR-0027 §3)."""
    shared = Path("/dev/shm")
    if not shared.is_dir() or shared.stat().st_dev == Path(tempfile.gettempdir()).stat().st_dev:
        unavailable("no second file system here", "CI needs /dev/shm on a file system of its own")
    root = Path(tempfile.mkdtemp(dir=shared, prefix="security-cases-"))
    protected = Path(tempfile.mkdtemp(prefix="security-cases-instance-"))
    data = root / found.lake_settings()["data_location"]
    data.mkdir(parents=True)
    writer = duckdb.connect()
    cleanup: list[str] = []
    try:
        for extension in ("ducklake", "sqlite", "postgres"):
            writer.install_extension(extension)
            writer.load_extension(extension)
        if catalog == "sqlite":
            path = root / "catalog.sqlite"
            attach = (
                f"ATTACH 'ducklake:sqlite:{path}' AS l "
                f"(DATA_PATH '{data.as_posix()}/', DATA_INLINING_ROW_LIMIT 0)"
            )
            writer.execute(attach)
            location: SqliteLakeLocation | PostgresLakeLocation = SqliteLakeLocation(
                kind="lake_location", catalog="sqlite", path=str(path), data_path=f"{data}/"
            )
        else:
            # A database of its own, a catalog schema its owner writes and a reader role that
            # may read only that schema, as a careful DBA sets them up (ADR-0027 §3).
            settings = _postgres(unavailable)
            suffix = uuid.uuid4().hex[:12]
            database, owner, reader = f"sc_{suffix}", f"own_{suffix}", f"rd_{suffix}"
            quoted = os.environ[POSTGRES_PASSWORD].replace("'", "''")
            writer.execute(f"CREATE TEMPORARY SECRET (TYPE postgres, PASSWORD '{quoted}')")
            admin = " ".join(f"{k}={v}" for k, v in settings.items())
            writer.execute(f"ATTACH '{admin}' AS admin (TYPE postgres)")
            _execute(writer, "admin", f"CREATE DATABASE {database}")
            for role in (owner, reader):
                _execute(writer, "admin", f"CREATE ROLE {role} LOGIN PASSWORD '{quoted}'")
            cleanup += [database, owner, reader]
            on = " ".join(f"{k}={v}" for k, v in {**settings, "dbname": database}.items())
            writer.execute(f"ATTACH '{on}' AS db (TYPE postgres)")
            _execute(writer, "db", "REVOKE ALL ON SCHEMA public FROM PUBLIC")
            _execute(writer, "db", f"CREATE SCHEMA cat AUTHORIZATION {owner}")
            _execute(writer, "db", f"GRANT USAGE ON SCHEMA cat TO {reader}")
            as_owner = " ".join(
                f"{k}={v}" for k, v in {**settings, "dbname": database, "user": owner}.items()
            )
            attach = (
                f"ATTACH 'ducklake:postgres:{as_owner}' AS l "
                f"(DATA_PATH '{data.as_posix()}/', METADATA_SCHEMA 'cat', "
                "DATA_INLINING_ROW_LIMIT 0)"
            )
            writer.execute(attach)
            location = PostgresLakeLocation(
                kind="lake_location",
                catalog="postgres",
                connection=" ".join(
                    f"{k}={v}" for k, v in {**settings, "dbname": database, "user": reader}.items()
                ),
                password_env=POSTGRES_PASSWORD,
                metadata_schema="cat",
                data_path=f"{data}/",
            )
        for step in _snapshots(found):
            writer.execute("BEGIN TRANSACTION")
            for statement in step:
                writer.execute(statement)
            writer.execute("COMMIT")
        writer.execute("DETACH l")
        if catalog == "postgres":
            _execute(writer, "db", f"GRANT SELECT ON ALL TABLES IN SCHEMA cat TO {cleanup[2]}")
        if catalog == "sqlite":  # an owner keeps a shared catalog in WAL mode (ADR-0027 §3)
            connection = sqlite3.connect(root / "catalog.sqlite")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.close()

        def changed(statement: str) -> None:
            writer.execute(attach)
            try:
                writer.execute(statement)
            finally:
                writer.execute("DETACH l")

        def on_catalog(statement: str) -> None:
            if catalog == "sqlite":
                connection = sqlite3.connect(root / "catalog.sqlite")
                with connection:
                    connection.execute(statement)
                connection.close()
            else:
                _execute(writer, "db", statement)

        table = found.lake_settings()["snapshots"][-1]["table"]
        foreign = root / "outside" / "foreign.parquet"
        _parquet(writer, found, table, found.lake_settings()["foreign"]["file"], foreign)
        locations, files = _protected(protected)
        yield Host(
            location,
            root,
            data,
            protected,
            changed,
            on_catalog,
            foreign,
            table,
            lambda: _backup(writer, catalog, root),
            locations,
            files,
        )
    finally:
        if cleanup:
            database, owner, reader = cleanup
            writer.execute("DETACH DATABASE IF EXISTS db")
            _execute(writer, "admin", f"DROP DATABASE {database} WITH (FORCE)")
            for role in (owner, reader):
                _execute(writer, "admin", f"DROP ROLE {role}")
        writer.close()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(protected, ignore_errors=True)


def _parquet(
    writer: duckdb.DuckDBPyConnection, found: sf.Fixture, table: str, rows: str, path: Path
) -> None:
    """The fixture's `rows` file written as Parquet at `path`, with the columns of `table`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ", ".join(f"'{c}': '{kind.type}'" for c, kind in found.catalog[table].items())
    source = (found.folder / rows).as_posix()
    writer.execute(
        f"COPY (SELECT * FROM read_csv('{source}', header = true, columns = {{{columns}}})) "
        f"TO '{path.as_posix()}' (FORMAT parquet)"
    )


def _backup(writer: duckdb.DuckDBPyConnection, catalog: str, root: Path) -> Callable[[], None]:
    """The catalog's state now; the function returned puts it back, as a restore would."""
    if catalog == "sqlite":
        kept = root / f"backup-{uuid.uuid4().hex[:8]}.sqlite"
        source = sqlite3.connect(root / "catalog.sqlite")
        target = sqlite3.connect(kept)
        source.backup(target)
        source.close()
        target.close()

        def restore_sqlite() -> None:
            source = sqlite3.connect(kept)
            target = sqlite3.connect(root / "catalog.sqlite")
            source.backup(target)
            source.close()
            target.close()

        return restore_sqlite
    schema = f"bk_{uuid.uuid4().hex[:8]}"
    listed = "SELECT table_name FROM information_schema.tables WHERE table_schema = ''cat''"
    tables = [
        row[0]
        for row in writer.execute(f"SELECT * FROM postgres_query('db', '{listed}')").fetchall()
    ]
    _execute(writer, "db", f"CREATE SCHEMA {schema}")
    for name in tables:
        _execute(writer, "db", f'CREATE TABLE {schema}."{name}" AS TABLE cat."{name}"')

    def restore_postgres() -> None:
        for name in tables:
            _execute(writer, "db", f'DELETE FROM cat."{name}"')
            _execute(writer, "db", f'INSERT INTO cat."{name}" SELECT * FROM {schema}."{name}"')

    return restore_postgres


VERIFY, READ, DECLARE = "verify", "read", "declare"  # what meets a change (`change`)


def change(host: Host, step: Mapping[str, Any], verified: Callable[[], None]) -> str:
    """Make `step`'s change; what meets it next: `VERIFY`, the lake's schedule verifying it
    again; `READ`, the read alone; `DECLARE`, the connection declared again as changed and
    attached anew (`verified`: one verification on the schedule, for a change made of several
    commits)."""
    op = step["op"]
    if op == "create_view":
        sql = str(step["sql"]).replace("foreign", f"read_parquet('{host.foreign.as_posix()}')")
        host.owner(f'CREATE VIEW l."{step["name"]}" AS {sql}')
    elif op == "create_macro":
        host.owner(f'CREATE MACRO l."{step["name"]}"() AS TABLE {step["sql"]}')
    elif op == "add_file":
        host.owner(f"CALL ducklake_add_data_files('l', '{host.table}', '{_placed(host, step)}')")
    elif op == "swap_file_for_link":
        served = _data_files(host)[0]
        served.unlink()
        served.symlink_to(host.foreign)
        return READ
    elif op == "register_location_inside":
        inside = host.data / "registered-project"
        inside.mkdir()
        host.locations.append(inside)
    elif op == "set_location":  # the lake's location now holds a managed lake's
        if step["contains"] != "managed_lake":
            raise RuntimeError(f"no location containing {step['contains']!r} in this runner")
        managed = host.data / "managed-lake" / "data"
        managed.mkdir(parents=True)
        host.locations.append(managed)
        return DECLARE
    elif op == "replace_catalog_file":
        if not isinstance(host.location, SqliteLakeLocation):
            raise NotApplicable("a PostgreSQL catalog has no catalog file")
        path = Path(host.location.path)
        other = host.root / "elsewhere" / "catalog.sqlite"
        other.parent.mkdir()
        shutil.copyfile(path, other)
        path.unlink()
        path.symlink_to(other)
    elif op == "postgres_catalog":
        if not isinstance(host.location, PostgresLakeLocation):
            raise NotApplicable("a SQLite catalog has no role")
        pairs = dict(pair.split("=", 1) for pair in host.location.connection.split())
        reader = pairs["user"]
        host.catalog("CREATE SCHEMA other_rows")
        host.catalog("CREATE TABLE other_rows.secret (zone_id TEXT)")
        host.catalog(f"GRANT USAGE ON SCHEMA other_rows TO {reader}")
        host.catalog(f"GRANT SELECT ON other_rows.secret TO {reader}")
    elif op == "restore_catalog_reusing_id":
        restore = host.backup()  # taken at the served snapshot
        host.owner('CREATE TABLE l."restore_marker" (marker INTEGER)')  # the next commit
        verified()  # served in turn
        restore()  # back to the backup: the next commit reuses the id just served
        host.owner(f'DELETE FROM l."{host.table}" WHERE {step["delete_rows_where"]}')
    else:
        raise RuntimeError(f"no lake change {op!r} in this runner")
    return VERIFY


def _data_files(host: Host) -> list[Path]:
    found: list[Path] = sorted(host.data.rglob("ducklake-*.parquet"))
    if not found:
        raise RuntimeError("the lake holds no data file")
    return found


def _placed(host: Host, step: Mapping[str, Any]) -> str:
    """Where `add_file` puts its file, as the catalog then names it."""
    where, name = step["where"], f"added-{uuid.uuid4().hex[:8]}.parquet"
    if step["rows"] not in {"foreign", "managed"}:
        raise RuntimeError(f"no rows {step['rows']!r} in this runner")
    folder = _data_files(host)[0].parent
    if where == "inside_location":
        placed = folder / name
        shutil.copyfile(host.foreign, placed)
    elif where == "outside_location":
        placed = host.root / "outside" / name
        shutil.copyfile(host.foreign, placed)
    elif where == "symbolic_link_outside":
        target = host.root / "outside" / name
        shutil.copyfile(host.foreign, target)
        placed = folder / name
        placed.symlink_to(target)
    elif where == "route_a_file":  # the Route A source file, rewritten with the rows
        placed = host.files[1]
        shutil.copyfile(host.foreign, placed)
    elif where == "other_project_file":
        placed = host.locations[4] / name
        shutil.copyfile(host.foreign, placed)
    elif where == "hard_link_to_managed":  # a managed lake on the lake's own file system
        managed = host.root / "managed" / "data"
        managed.mkdir(parents=True)
        original = managed / name
        shutil.copyfile(host.foreign, original)
        host.locations.append(managed)
        host.files.append(original)
        placed = folder / name
        os.link(original, placed)
    else:
        raise RuntimeError(f"no place {where!r} in this runner")
    return placed.as_posix()
