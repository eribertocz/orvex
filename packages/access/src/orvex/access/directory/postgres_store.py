"""The PostgreSQL access store of server mode: its own schema, owned by its own role (adapter).

- **The connection** is a restricted libpq `keyword=value` string: `host`, `port`, `dbname`,
  `user`, `sslmode` and `sslrootcert`, nothing else. The password comes from the environment
  variable the caller names, never from the string.
- **TLS.** A server on another machine is reached only with `sslmode=verify-full`, which
  encrypts and checks that the certificate is the named host's. Without TLS
  (`sslmode=disable`, or no `sslmode`), only a loopback host or a Unix socket is accepted.
- **Its own schema.** All tables live in one schema, `access` by default, created by the first
  migration with the connected role as its owner and no privilege for `PUBLIC`. A schema that
  exists and belongs to another role is refused: another module's role could otherwise read or
  write it. The session's `search_path` is that schema alone.
- **Isolation.** A write transaction is `SERIALIZABLE`: its outcome is the outcome of some order
  of the transactions one at a time, so no decision rests on a fact another transaction changed
  meanwhile (the caller's own role or flag included). PostgreSQL ends a transaction that could
  break this (`40001`), and the directory runs it again. A read that decides a last-Builder or
  last-administrator rule also locks the rows it counts (`FOR UPDATE`), so the two transactions
  that compete for them wait in turn. A read-only transaction is `REPEATABLE READ READ ONLY`: its
  reads see one state.
- Each thread has its own connection, dropped when the database breaks it; `close` closes all.
"""

from __future__ import annotations

import re
import ssl
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass

import pg8000.native
from orvex.access.directory.store import Row, Transaction, TransientConflict, UniqueViolation
from orvex.access.errors import ACCESS_STORE_UNSAFE, VALIDATION_FAILED, AccessError
from pg8000.exceptions import DatabaseError, InterfaceError

CONNECT_TIMEOUT_S = 10
_KEYWORDS = frozenset({"host", "port", "dbname", "user", "sslmode", "sslrootcert"})
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
_SCHEMA = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_UNIQUE = "23505"
_TRANSIENT = frozenset({"40001", "40P01", "55P03"})  # serialization, deadlock, lock not available

_ID = "{c} ~ '^[A-Za-z0-9_-]{{1,128}}$'"
_INSTANT = (
    "{c} ~ '^[0-9]{{4}}-[01][0-9]-[0-3][0-9]T[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}[.][0-9]{{6}}Z$'"
)


def migrations_for(schema: str) -> tuple[tuple[str, ...], ...]:
    """The migrations of a store in `schema`, the same schema as the SQLite store's."""
    return (
        (
            f"CREATE SCHEMA IF NOT EXISTS {schema} AUTHORIZATION CURRENT_USER",
            f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC",
            """CREATE TABLE store_meta (
                id integer PRIMARY KEY CHECK (id = 1),
                schema_version integer NOT NULL CHECK (schema_version >= 0),
                recovery_digest text CHECK (
                    recovery_digest IS NULL OR recovery_digest ~ '^[0-9a-f]{64}$'
                )
            )""",
            "INSERT INTO store_meta (id, schema_version) VALUES (1, 0)",
            f"""CREATE TABLE users (
                id text PRIMARY KEY CHECK ({_ID.format(c="id")}),
                user_name text NOT NULL CHECK (
                    user_name ~ '^[A-Za-z0-9][A-Za-z0-9._@+-]{{0,127}}$'
                ),
                display_name text NOT NULL CHECK (char_length(display_name) BETWEEN 1 AND 200),
                state text NOT NULL CHECK (state IN ('active', 'deactivated')),
                administrator boolean NOT NULL,
                authz_version bigint NOT NULL CHECK (authz_version >= 1),
                created_at text NOT NULL CHECK ({_INSTANT.format(c="created_at")}),
                changed_at text NOT NULL CHECK ({_INSTANT.format(c="changed_at")})
            )""",
            "CREATE UNIQUE INDEX users_user_name ON users (lower(user_name))",
            f"""CREATE TABLE memberships (
                project text NOT NULL CHECK ({_ID.format(c="project")}),
                user_id text NOT NULL REFERENCES users (id),
                role text NOT NULL CHECK (role IN ('builder', 'author', 'viewer')),
                PRIMARY KEY (project, user_id)
            )""",
            "CREATE INDEX memberships_user ON memberships (user_id)",
            f"""CREATE TABLE project_groups (
                project text NOT NULL CHECK ({_ID.format(c="project")}),
                name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 64),
                PRIMARY KEY (project, name)
            )""",
            """CREATE TABLE group_members (
                project text NOT NULL,
                group_name text NOT NULL,
                user_id text NOT NULL,
                PRIMARY KEY (project, group_name, user_id),
                FOREIGN KEY (project, group_name)
                    REFERENCES project_groups (project, name) ON DELETE CASCADE,
                FOREIGN KEY (project, user_id)
                    REFERENCES memberships (project, user_id) ON DELETE CASCADE
            )""",
            "CREATE INDEX group_members_member ON group_members (project, user_id)",
        ),
    )


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    """Where the store is: the connection's keywords, its password and its schema."""

    keywords: Mapping[str, str]
    password: str
    schema: str = "access"


def _invalid(reason: str) -> AccessError:
    return AccessError(VALIDATION_FAILED, f"The access store's connection {reason}.", {})


def read_settings(connection: str, password: str, schema: str = "access") -> PostgresSettings:
    """Check a connection string against the rules above, before anything connects."""
    keywords: dict[str, str] = {}
    for pair in connection.split():
        keyword, equals, value = pair.partition("=")
        if not equals or not value or keyword not in _KEYWORDS:
            raise _invalid("must be host, port, dbname, user, sslmode or sslrootcert pairs")
        keywords[keyword] = value
    if "host" not in keywords or "user" not in keywords or "dbname" not in keywords:
        raise _invalid("needs host, dbname and user")
    if not _SCHEMA.fullmatch(schema):
        raise _invalid("schema must be a lower-case identifier")
    host = keywords["host"]
    local = host in _LOOPBACK or host.startswith("/")
    mode = keywords.get("sslmode", "disable")
    if mode not in {"disable", "verify-full"}:
        raise _invalid("sslmode must be verify-full, or disable for this machine only")
    if mode == "disable" and not local:
        raise _invalid("to another machine needs sslmode=verify-full")
    if not password:
        raise _invalid("needs a password")
    return PostgresSettings(keywords, password, schema)


def _sqlstate(error: DatabaseError) -> str | None:
    """The SQLSTATE code PostgreSQL sent with the error."""
    fields = error.args[0] if error.args else None
    return str(fields["C"]) if isinstance(fields, dict) and "C" in fields else None


class _Transaction:
    locking = " FOR UPDATE"

    def __init__(self, connection: pg8000.native.Connection) -> None:
        self._connection = connection

    def rows(self, sql: str, /, **parameters: object) -> list[Row]:
        found = self._execute(sql, parameters)
        return [tuple(row) for row in found or []]

    def run(self, sql: str, /, **parameters: object) -> None:
        self._execute(sql, parameters)

    def _execute(self, sql: str, parameters: dict[str, object]) -> list[list[object]] | None:
        try:
            found: list[list[object]] | None = self._connection.run(sql, **parameters)
        except DatabaseError as error:
            code = _sqlstate(error)
            if code == _UNIQUE:
                raise UniqueViolation(str(code)) from error
            if code in _TRANSIENT:
                raise TransientConflict(str(code)) from error
            raise
        return found


class PostgresStore:
    """The access store in its own PostgreSQL schema."""

    def __init__(
        self, settings: PostgresSettings, migrations: Sequence[Sequence[str]] | None = None
    ) -> None:
        self.settings = settings
        self.migrations = migrations_for(settings.schema) if migrations is None else migrations
        self._local = threading.local()
        self._guard = threading.Lock()
        self._open: list[pg8000.native.Connection] = []

    def _connect(self) -> pg8000.native.Connection:
        keywords = self.settings.keywords
        # `False`, never `None`: the driver reads `None` as TLS without checking the certificate.
        context: ssl.SSLContext | bool = False
        if keywords.get("sslmode") == "verify-full":
            context = ssl.create_default_context(cafile=keywords.get("sslrootcert"))
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
        host = keywords["host"]
        connection = pg8000.native.Connection(
            keywords["user"],
            password=self.settings.password,
            database=keywords["dbname"],
            host=None if host.startswith("/") else host,
            unix_sock=f"{host}/.s.PGSQL.{keywords.get('port', '5432')}"
            if host.startswith("/")
            else None,
            port=int(keywords.get("port", "5432")),
            ssl_context=context,
            timeout=CONNECT_TIMEOUT_S,
            application_name="access",
        )
        schema = self.settings.schema
        owners = connection.run(
            "SELECT pg_get_userbyid(nspowner) = current_user FROM pg_namespace "
            "WHERE nspname = :schema",
            schema=schema,
        )
        if owners and owners != [[True]]:
            connection.close()
            raise AccessError(
                ACCESS_STORE_UNSAFE,
                "The access schema belongs to another database role.",
                {"schema": schema},
            )
        connection.run(f"SET search_path TO {schema}")
        return connection

    def _connection(self) -> pg8000.native.Connection:
        connection: pg8000.native.Connection | None = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._connect()
            self._local.connection = connection
            with self._guard:
                self._open.append(connection)
        return connection

    @contextmanager
    def transaction(self, *, read_only: bool = False) -> Iterator[Transaction]:
        connection = self._connection()
        begin = (
            "START TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            if read_only
            else "START TRANSACTION ISOLATION LEVEL SERIALIZABLE"
        )
        try:
            connection.run(begin)
        except InterfaceError:
            self._drop()
            raise
        try:
            yield _Transaction(connection)
        except BaseException:
            try:
                connection.run("ROLLBACK")
            except InterfaceError:
                self._drop()
            raise
        try:
            connection.run("COMMIT")
        except DatabaseError as error:
            code = _sqlstate(error)
            if code in _TRANSIENT:
                raise TransientConflict(str(code)) from error
            raise
        except InterfaceError:
            self._drop()
            raise

    def stored_version(self, transaction: Transaction) -> int:
        ((present,),) = transaction.rows("SELECT to_regclass('store_meta') IS NOT NULL")
        if not present:
            return 0
        found = transaction.rows("SELECT schema_version FROM store_meta")
        if len(found) != 1 or not isinstance(found[0][0], int):
            raise AccessError(ACCESS_STORE_UNSAFE, "The access store's version is unreadable.", {})
        return found[0][0]

    def serialize_migrations(self, transaction: Transaction) -> None:
        transaction.run(
            "SELECT pg_advisory_xact_lock(hashtext(:key))", key=f"access:{self.settings.schema}"
        )

    def _drop(self) -> None:
        connection: pg8000.native.Connection | None = getattr(self._local, "connection", None)
        self._local.connection = None
        if connection is not None:
            with self._guard:
                if connection in self._open:
                    self._open.remove(connection)
            with suppress(InterfaceError):
                connection.close()

    def close(self) -> None:
        """Close every thread's connection; call it once no transaction is running."""
        with self._guard:
            opened, self._open = self._open, []
        for connection in opened:
            with suppress(InterfaceError):
                connection.close()
        self._local = threading.local()
