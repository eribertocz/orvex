"""An existing DuckLake, attached read-only and isolated (P2-30 PR 1, #399; ADR-0027 §3).

Another system owns and writes the lake, so its catalog and its files are untrusted. Each lake is
attached in a DuckDB instance of its own, holding that lake, its secrets and nothing else. The
instance is opened in a fixed order, and nothing of the lake is read before the checks that
guard it pass:

1. **The location**, as for a managed lake: object storage over HTTPS only, no secret in the
   connection string, a SQLite catalog given by an absolute path.
2. **The catalog, before the attach.**
   - A PostgreSQL catalog is first reached through a plain, read-only PostgreSQL attach. Its
     session must be read-only (`SOURCE_NOT_READ_ONLY`), and its role dedicated and minimal:
     used by no other connection, a member of no privileged built-in role, and unable to use
     any object outside the catalog schema, system catalogs aside (`STORE_SOURCE_INVALID`).
     The catalog's owner can turn catalog tables into views over `SECURITY INVOKER` functions,
     which run with this role; so these checks come before DuckLake reads any catalog table.
   - A SQLite catalog is opened without following links. On the opened descriptors, the file
     and its `-wal`, `-shm` and `-journal` files must be regular files, distinct, and none of
     them a hard link to a protected file (`STORE_SOURCE_INVALID`). It must be in WAL mode:
     measured on DuckDB 2.0.0, a reader of a rollback-journal catalog keeps a lock that makes
     every later commit of the owner fail.
   - The catalog's version must be one the bundled DuckLake reads without migrating and the
     corpus has run on (`ENGINE_VERSION_UNTESTED`).
3. **The attach:** DuckDB's `READ_ONLY`, `AUTOMATIC_MIGRATION false`, and the declared
   `DATA_PATH`, which DuckLake refuses unless it is the catalog's own. A PostgreSQL catalog's
   own session is then checked read-only again, and a SQLite catalog's files are checked again.
4. **The sandbox:** every setting is set globally (the time zone included), then
   `enable_external_access = false`, then `lock_configuration = true`. Files can then be read
   only under the data location and the instance's spill folder, and the SQLite catalog file
   with its companions (DuckDB 2.0.0 resolves symbolic links before it checks the list).
   `allowed_configs` is never set: after the lock, no setting changes.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import os
import shutil
import stat
import tempfile
from collections.abc import Sequence
from pathlib import Path

import duckdb
from orvex.contracts.store.v1 import (
    ExistingLakeAttachment,
    PostgresLakeLocation,
    ProtectedFile,
    RolesInUseItem,
    Snapshot,
    SqliteLakeLocation,
)
from orvex.store import locations
from orvex.store.errors import StoreError
from orvex.store.sql_text import literal as _quote

LAKE = "lake"
CHECK = "catalog_check"
METADATA = f"__ducklake_metadata_{LAKE}"
# The catalog versions of the bundled DuckLake that the semantic corpus runs on. An older one
# would be read through DuckLake's compatibility path (measured: it fails on inlined data), a
# newer one is refused by DuckLake itself; both are refused before the attach.
TESTED_CATALOG_VERSIONS = frozenset({"1.1"})
# Added last to every catalog connection string (libpq keeps the last of a repeated keyword).
READ_ONLY_SESSION = "options='-c default_transaction_read_only=on'"
DEFAULT_SCHEMA = "public"
PRIVILEGED_ROLES = (
    "pg_read_all_data",
    "pg_read_server_files",
    "pg_execute_server_program",
    "pg_write_all_data",
)
SQLITE_COMPANIONS = ("-wal", "-shm", "-journal")
# A protected catalog's companion files: SQLite's, and a DuckDB-file catalog's write-ahead log.
PROTECTED_COMPANIONS = (*SQLITE_COMPANIONS, ".wal")
SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_WAL_FORMAT = 2  # header bytes 18 and 19: the write and read format versions
_EXPECTED_DATABASES = frozenset({LAKE, "memory", "system", "temp"})

_READ_ONLY_SQL = (
    "SELECT current_setting('default_transaction_read_only') AS d, "
    "current_setting('transaction_read_only') AS t"
)


def privileges_sql(schema: str) -> str:
    """PostgreSQL SQL naming each kind of thing the session's role can use beyond the catalog
    schema: superuser, a privileged built-in role, or any privilege on a schema, table, view,
    sequence, function or foreign server elsewhere. Every role it is a member of counts (it
    could `SET ROLE` to it), and so does `PUBLIC` (`has_*_privilege` includes it). System
    catalogs (`pg_catalog`, `information_schema`, TOAST and temporary schemas) aside. `schema`
    matches `[a-z_][a-z0-9_]*`."""
    roles = ", ".join(f"'{role}'" for role in PRIVILEGED_ROLES)
    return (
        "WITH member AS (SELECT r.oid, r.rolname, r.rolsuper FROM pg_catalog.pg_roles AS r "
        "WHERE pg_catalog.pg_has_role(current_user, r.oid, 'MEMBER')), "
        "outside AS (SELECT n.oid FROM pg_catalog.pg_namespace AS n "
        f"WHERE n.nspname <> '{schema}' "
        "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
        "AND n.nspname !~ '^pg_(toast|temp_|toast_temp_)') "
        "SELECT DISTINCT kind AS v FROM ("
        "SELECT 'superuser' AS kind FROM member WHERE rolsuper "
        f"UNION ALL SELECT 'role ' || rolname FROM member WHERE rolname IN ({roles}) "
        "UNION ALL SELECT 'schema' FROM outside AS o, member AS m "
        "WHERE pg_catalog.has_schema_privilege(m.oid, o.oid, 'USAGE, CREATE') "
        "UNION ALL SELECT CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' "
        "WHEN 'S' THEN 'sequence' WHEN 'f' THEN 'foreign table' ELSE 'table' END "
        "FROM pg_catalog.pg_class AS c JOIN outside AS o ON o.oid = c.relnamespace, member AS m "
        "WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') AND CASE WHEN c.relkind = 'S' "
        "THEN pg_catalog.has_sequence_privilege(m.oid, c.oid, 'USAGE, SELECT, UPDATE') "
        "ELSE pg_catalog.has_table_privilege(m.oid, c.oid, "
        "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') "
        "OR pg_catalog.has_any_column_privilege(m.oid, c.oid, "
        "'SELECT, INSERT, UPDATE, REFERENCES') "
        "END "
        "UNION ALL SELECT 'function' FROM pg_catalog.pg_proc AS p "
        "JOIN outside AS o ON o.oid = p.pronamespace, member AS m "
        "WHERE pg_catalog.has_function_privilege(m.oid, p.oid, 'EXECUTE') "
        "UNION ALL SELECT 'foreign server' FROM pg_catalog.pg_foreign_server AS s, member AS m "
        "WHERE pg_catalog.has_server_privilege(m.oid, s.oid, 'USAGE')"
        ") AS found"
    )


class ExistingLake:
    """One existing DuckLake, attached read-only in a DuckDB instance of its own."""

    def __init__(
        self, attachment: ExistingLakeAttachment, *, memory_limit: str, threads: int
    ) -> None:
        location = attachment.location
        self.name = attachment.name
        if location.encrypted is not None:
            raise StoreError(
                "STORE_LOCATION_INVALID",
                f"Connection {self.name}: encryption is the lake owner's setting and cannot be "
                "declared here.",
                {"connection": self.name},
            )
        if not memory_limit or threads < 1:
            raise StoreError(
                "STORE_LIMITS_REQUIRED", "The lake needs a memory limit and a thread count.", {}
            )
        # Location checks first, and every secret registered, before anything is opened.
        password = ""
        if isinstance(location, PostgresLakeLocation):
            password = locations._postgres_password(location)
            self._schema = location.metadata_schema or DEFAULT_SCHEMA
            self._connection_text = self._catalog_connection(location, attachment.roles_in_use)
        data_path = _with_slash(location.data_path)
        storage = locations._storage_credentials(location.storage, data_path)
        self._descriptors: list[int] = []
        self._catalog_identity: tuple[int, int] | None = None
        self._protected: frozenset[tuple[int, int]] = frozenset()
        if isinstance(location, SqliteLakeLocation):
            self._catalog_path = self._sqlite_path(location)
            self._protected = _protected_identities(self.name, attachment.protected_files)
        self._spill = tempfile.mkdtemp(prefix="existing-lake-spill-")
        try:
            self._connection = duckdb.connect(
                config={
                    "memory_limit": memory_limit,
                    "threads": threads,
                    "allow_persistent_secrets": False,
                    "autoinstall_known_extensions": False,
                    "autoload_known_extensions": False,
                }
            )
        except (duckdb.Error, ValueError) as error:
            shutil.rmtree(self._spill, ignore_errors=True)
            raise StoreError(
                "STORE_LIMITS_REQUIRED",
                "The memory limit or the thread count is not valid.",
                {"duckdb": str(error)},
            ) from None
        try:
            self._load("ducklake")
            options = [
                "READ_ONLY",
                "AUTOMATIC_MIGRATION false",
                f"DATA_PATH {_quote(data_path)}",
            ]
            if storage is not None:
                self._storage_secret(storage, data_path)
            if isinstance(location, PostgresLakeLocation):
                self._check_postgres(password)
                options.append(f"METADATA_SCHEMA {_quote(self._schema)}")
                self._attach(f"ducklake:postgres:{self._connection_text}", options)
                self._require_read_only(METADATA)
                files: list[str] = []
            else:
                self._check_sqlite()
                self._attach(f"ducklake:sqlite:{self._catalog_path}", options)
                self._check_sqlite_files(after_attach=True)
                files = [self._catalog_path, *(self._catalog_path + s for s in SQLITE_COMPANIONS)]
            self._lock(locations._sandbox_folder(data_path), files)
        except BaseException:
            self.close()
            raise

    # --- the catalog's connection --------------------------------------------------------------

    def _catalog_connection(
        self, location: PostgresLakeLocation, roles_in_use: Sequence[RolesInUseItem]
    ) -> str:
        """The catalog's libpq connection string as every catalog session uses it: TLS
        verified against the system's authorities unless it names its own, a bounded connect,
        and a read-only session. Its role must be named, and used by no other connection."""
        pairs = locations._conninfo(location.connection)
        if "options" in pairs:
            raise StoreError(
                "STORE_LOCATION_INVALID",
                f"Connection {self.name}: the catalog's connection cannot set options; the "
                "session's options are the store's.",
                {"connection": self.name},
            )
        user = pairs.get("user", "")
        if not user:
            raise StoreError(
                "STORE_LOCATION_INVALID",
                f"Connection {self.name}: the catalog's connection must name its role (user).",
                {"connection": self.name},
            )
        if user in {role.root for role in roles_in_use}:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Connection {self.name}: the catalog's role is used by another connection or by "
                "a managed lake; an existing lake needs a role of its own.",
                {"connection": self.name, "kind": "role in use"},
            )
        return (
            f"{locations._with_root_cert(location.connection)} "
            f"connect_timeout={locations.POSTGRES_CONNECT_TIMEOUT_S} {READ_ONLY_SESSION}"
        )

    def _check_postgres(self, password: str) -> None:
        """Before DuckLake reads any catalog table: a read-only session, a minimal role, a
        tested catalog version. Reached through a plain PostgreSQL attach, detached after."""
        self._load("postgres")
        try:
            self._connection.execute(
                f"CREATE TEMPORARY SECRET (TYPE postgres, PASSWORD {_quote(password)})"
            )
            self._connection.execute(
                f"ATTACH {_quote(self._connection_text)} AS {CHECK} (TYPE postgres, READ_ONLY)"
            )
        except duckdb.Error as error:
            raise StoreError(
                "STORE_CONNECTION_FAILED",
                f"Connection {self.name}: the catalog could not be reached or refused the "
                "credentials.",
                {"connection": self.name, "duckdb": str(error)},
            ) from None
        self._require_read_only(CHECK)
        found = self._postgres_rows(privileges_sql(self._schema))
        if found:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Connection {self.name}: the catalog's role can use more than the catalog schema "
                f"({', '.join(found)}). Use a role that can read only that schema.",
                {"connection": self.name, "kind": found[0]},
            )
        version = self._postgres_rows(
            f"SELECT value AS v FROM \"{self._schema}\".ducklake_metadata WHERE key = 'version'"
        )
        self._require_version(version)
        self._connection.execute(f"DETACH {CHECK}")

    def _require_read_only(self, database: str) -> None:
        try:
            row = self._connection.execute(
                "SELECT d, t FROM postgres_query($database, $sql)",
                {"database": database, "sql": _READ_ONLY_SQL},
            ).fetchone()
        except duckdb.Error as error:
            raise StoreError(
                "STORE_CONNECTION_FAILED",
                f"Connection {self.name}: the catalog session could not be inspected.",
                {"connection": self.name, "duckdb": str(error)},
            ) from None
        if row is None or tuple(map(str, row)) != ("on", "on"):
            raise StoreError(
                "SOURCE_NOT_READ_ONLY",
                f"Connection {self.name}: the catalog's PostgreSQL session is not read-only "
                "(default_transaction_read_only must be on).",
                {"connection": self.name},
            )

    def _postgres_rows(self, sql: str) -> list[str]:
        try:
            row = self._connection.execute(
                "SELECT list(v ORDER BY v) FROM postgres_query($database, $sql)",
                {"database": CHECK, "sql": sql},
            ).fetchone()
        except duckdb.Error as error:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Connection {self.name}: the catalog could not be inspected.",
                {"connection": self.name, "duckdb": str(error)},
            ) from None
        found = row[0] if row is not None and isinstance(row[0], list) else []
        return [str(value) for value in found]

    def _require_version(self, found: list[str]) -> None:
        version = found[0] if len(found) == 1 else ""
        if version not in TESTED_CATALOG_VERSIONS:
            raise StoreError(
                "ENGINE_VERSION_UNTESTED",
                f"Connection {self.name}: the catalog's DuckLake version is not one this release "
                f"reads without migrating ({', '.join(sorted(TESTED_CATALOG_VERSIONS))}).",
                {"connection": self.name, "version": version or "(none)"},
            )

    # --- a SQLite catalog ----------------------------------------------------------------------

    def _sqlite_path(self, location: SqliteLakeLocation) -> str:
        path = Path(location.path)
        if not path.is_absolute():
            raise StoreError(
                "STORE_LOCATION_INVALID",
                f"Connection {self.name}: the SQLite catalog needs an absolute path.",
                {"connection": self.name},
            )
        # The folders are resolved; the file itself is never followed (`_check_sqlite_files`).
        return (path.parent.resolve() / path.name).as_posix()

    def _check_sqlite(self) -> None:
        """Before DuckLake reads the catalog: its files, its journal mode and its version."""
        self._check_sqlite_files(after_attach=False)
        header = os.pread(self._descriptors[0], 100, 0)
        if not header.startswith(SQLITE_HEADER) or len(header) < 20:
            raise self._sqlite_invalid("it is not a SQLite database")
        if header[18] != SQLITE_WAL_FORMAT or header[19] != SQLITE_WAL_FORMAT:
            raise self._sqlite_invalid(
                "it is not in WAL mode, and a reader of a rollback-journal catalog blocks every "
                "commit of the lake's owner"
            )
        self._load("sqlite")
        try:
            self._connection.execute(
                f"ATTACH {_quote(self._catalog_path)} AS {CHECK} (TYPE sqlite, READ_ONLY)"
            )
            row = self._connection.execute(
                f"SELECT list(value) FROM {CHECK}.ducklake_metadata WHERE key = 'version'"
            ).fetchone()
            self._connection.execute(f"DETACH {CHECK}")
        except duckdb.Error as error:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Connection {self.name}: the catalog is not a readable DuckLake catalog.",
                {"connection": self.name, "duckdb": str(error)},
            ) from None
        found = row[0] if row is not None and isinstance(row[0], list) else []
        self._require_version([str(value) for value in found])

    def _check_sqlite_files(self, *, after_attach: bool) -> None:
        """The catalog and each `-wal`, `-shm` and `-journal` file that exists, opened without
        following a link: regular files, distinct from each other, none a protected file. Kept
        open while the lake is open; after the attach the catalog must still be the same file."""
        opened = [_open_no_follow(self.name, self._catalog_path)]
        if opened[0] is None:
            raise self._sqlite_invalid("the file does not exist")
        opened += [_open_no_follow(self.name, self._catalog_path + s) for s in SQLITE_COMPANIONS]
        identities: list[tuple[int, int]] = []
        for descriptor in opened:
            if descriptor is None:
                continue
            self._descriptors.append(descriptor)
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise self._sqlite_invalid("a catalog file is not a regular file")
            identities.append((status.st_dev, status.st_ino))
        if len(set(identities)) != len(identities):
            raise self._sqlite_invalid("its files are links to each other")
        if self._protected & set(identities):
            raise self._sqlite_invalid("a catalog file is a hard link to a protected file")
        if not after_attach:
            self._catalog_identity = identities[0]
        elif identities[0] != self._catalog_identity:
            raise self._sqlite_invalid("the file was replaced while it was attached")

    def _sqlite_invalid(self, reason: str) -> StoreError:
        return StoreError(
            "STORE_SOURCE_INVALID",
            f"Connection {self.name}: the SQLite catalog cannot be used: {reason}.",
            {"connection": self.name, "reason": reason},
        )

    # --- the instance --------------------------------------------------------------------------

    def _load(self, extension: str) -> None:
        try:
            self._connection.install_extension(extension)
            self._connection.load_extension(extension)
        except duckdb.Error as error:
            raise StoreError(
                "STORE_EXTENSION_UNAVAILABLE",
                f"The DuckDB extension {extension} could not be installed or loaded.",
                {"extension": extension, "duckdb": str(error)},
            ) from None

    def _storage_secret(self, storage: locations._Storage, data_path: str) -> None:
        """The data location's temporary secret, scoped to it; web file systems turned off."""
        settings = storage.settings
        if settings.type == "azure":
            self._load("azure")
            options = [
                "TYPE azure",
                f"CONNECTION_STRING {_quote(storage.key_id)}",
                f"SCOPE {_quote(data_path)}",
            ]
        else:
            self._load("httpfs")
            self._connection.execute(
                "SET disabled_filesystems = 'HTTPFileSystem,HuggingFaceFileSystem'"
            )
            options = [
                f"TYPE {settings.type}",
                f"KEY_ID {_quote(storage.key_id)}",
                f"SECRET {_quote(storage.secret)}",
                f"USE_SSL {'true' if settings.use_ssl else 'false'}",
                f"SCOPE {_quote(data_path)}",
            ]
            if settings.type == "s3":
                options.append(f"REGION {_quote(settings.region)}")
                if settings.url_style is not None:
                    options.append(f"URL_STYLE {_quote(settings.url_style)}")
            else:
                options.append("URL_STYLE 'path'")
            if settings.endpoint is not None:
                options.append(f"ENDPOINT {_quote(settings.endpoint)}")
        try:
            self._connection.execute(f"CREATE TEMPORARY SECRET ({', '.join(options)})")
        except duckdb.Error as error:
            raise StoreError(
                "STORE_CATALOG_UNAVAILABLE",
                f"Connection {self.name}: the object storage settings were refused.",
                {"connection": self.name, "duckdb": str(error)},
            ) from None

    def _attach(self, target: str, options: list[str]) -> None:
        try:
            self._connection.execute(f"ATTACH {_quote(target)} AS {LAKE} ({', '.join(options)})")
        except duckdb.Error as error:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Connection {self.name}: the lake could not be attached read-only at its "
                "declared data path.",
                {"connection": self.name, "duckdb": str(error)},
            ) from None

    def _lock(self, data_folder: str, files: list[str]) -> None:
        """Every setting, globally; then no external access but the allow-list; then the lock.
        The order is fixed: nothing can change after the last step."""
        row = self._connection.execute(
            "SELECT list(database_name) FROM duckdb_databases()"
        ).fetchone()
        databases = set(row[0]) if row is not None and isinstance(row[0], list) else set()
        if databases != _EXPECTED_DATABASES:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Connection {self.name}: the instance holds another database.",
                {"connection": self.name},
            )
        folders = [data_folder.rstrip("/") + "/", Path(self._spill).resolve().as_posix() + "/"]
        for statement in (
            "SET GLOBAL TimeZone = 'UTC'",
            "SET GLOBAL python_enable_replacements = false",
            "SET GLOBAL arrow_lossless_conversion = true",
            f"SET temp_directory = {_quote(self._spill)}",
            f"SET allowed_directories = [{', '.join(map(_quote, folders))}]",
            f"SET allowed_paths = [{', '.join(map(_quote, files))}]",
            "SET enable_external_access = false",
            "SET lock_configuration = true",
        ):
            self._connection.execute(statement)

    def latest_snapshot(self) -> Snapshot:
        """The lake's latest snapshot, as its catalog lists it now (verification is PR 2)."""
        row = self._connection.execute(
            f"SELECT snapshot_id, epoch_us(snapshot_time) FROM ducklake_snapshots({_quote(LAKE)}) "
            "ORDER BY snapshot_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise StoreError(
                "STORE_SOURCE_INVALID", f"Connection {self.name}: the lake has no snapshot.", {}
            )
        committed = dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(microseconds=int(row[1]))
        return Snapshot(kind="snapshot", id=int(row[0]), committed_at=committed)

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
        for descriptor in self._descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        self._descriptors = []
        shutil.rmtree(self._spill, ignore_errors=True)

    def __enter__(self) -> ExistingLake:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _with_slash(path: str) -> str:
    return path if path.endswith("/") else path + "/"


def _open_no_follow(name: str, path: str) -> int | None:
    """A read-only descriptor of `path` itself, never of a link's target; None if absent. A
    symbolic link is refused (`ELOOP`)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        return os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        code = errno.errorcode.get(error.errno or 0, "error")
        reason = (
            "a catalog file is a symbolic link"
            if error.errno == errno.ELOOP
            else f"a catalog file cannot be opened ({code})"
        )
        raise StoreError(
            "STORE_SOURCE_INVALID",
            f"Connection {name}: the SQLite catalog cannot be used: {reason}.",
            {"connection": name, "reason": reason},
        ) from None


def _protected_identities(name: str, files: Sequence[ProtectedFile]) -> frozenset[tuple[int, int]]:
    """The device and inode of every protected file and of its companion files that exist. One
    that exists but cannot be examined fails closed."""
    found: set[tuple[int, int]] = set()
    for item in files:
        base = item.root
        for path in (base, *(base + suffix for suffix in PROTECTED_COMPANIONS)):
            try:
                status = os.stat(path)
            except FileNotFoundError:
                continue
            except OSError:
                raise StoreError(
                    "STORE_SOURCE_INVALID",
                    f"Connection {name}: a protected file cannot be examined, so the catalog "
                    "cannot be shown to be apart from it.",
                    {"connection": name},
                ) from None
            found.add((status.st_dev, status.st_ino))
    return frozenset(found)
