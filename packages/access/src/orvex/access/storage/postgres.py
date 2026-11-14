"""The PostgreSQL access store of server mode: its own schema, owned by its own role (adapter).

- **The connection** is a restricted libpq `keyword=value` string: `host`, `port`, `dbname`,
  `user`, `sslmode` and `sslrootcert`, nothing else. The password comes from the environment
  variable the caller names, never from the string.
- **TLS.** A server on another machine is reached only with `sslmode=verify-full`, which
  encrypts and checks that the certificate is the named host's. Without TLS
  (`sslmode=disable`, or no `sslmode`), only a loopback host or a Unix socket is accepted.
- **Not a superuser.** A superuser bypasses every grant, so the store refuses one: its role must
  be an ordinary role that owns its schema.
- **Its own schema.** All tables live in one schema, `access` by default, created by the first
  migration with the connected role as its owner and no privilege for `PUBLIC`. A schema that
  exists and belongs to another role is refused: another module's role could otherwise read or
  write it. The session's `search_path` is that schema alone.
- **One writer at a time.** A write transaction first takes the store's write lock (a session
  advisory lock), then starts `SERIALIZABLE`. Writers therefore run one after the other, as on
  SQLite, so no decision rests on a fact another transaction changed meanwhile, and the audit
  log's head is never contended. PostgreSQL may still end a transaction (`40001`, `40P01`); the
  directory runs it again. A read that decides a last-Builder or
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
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass

import pg8000.native
from orvex.access.errors import ACCESS_STORE_UNSAFE, VALIDATION_FAILED, AccessError
from orvex.access.storage.store import Row, Transaction, TransientConflict, UniqueViolation
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
        # 2: the audit log (#437). The role holds no UPDATE, DELETE or TRUNCATE grant on the
        # log's tables; retention runs through `audit_remove_through` alone.
        # No foreign key names `audit_keys`: checking one needs a row lock, which needs an UPDATE
        # grant. `verify` trusts a key only through the signatures that lead to it.
        (
            f"""CREATE TABLE instance (
                id integer PRIMARY KEY CHECK (id = 1),
                instance_id text NOT NULL CHECK ({_ID.format(c="instance_id")})
            )""",
            "INSERT INTO instance (id, instance_id) VALUES (1, gen_random_uuid()::text)",
            f"""CREATE TABLE audit_events (
                seq bigint PRIMARY KEY CHECK (seq >= 1),
                id text NOT NULL UNIQUE CHECK ({_ID.format(c="id")}),
                at text NOT NULL CHECK ({_INSTANT.format(c="at")}),
                kind text NOT NULL CHECK (char_length(kind) BETWEEN 1 AND 64),
                document text NOT NULL CHECK (char_length(document) BETWEEN 2 AND 65536),
                hash text NOT NULL CHECK (hash ~ '^[0-9a-f]{{64}}$')
            )""",
            f"""CREATE TABLE audit_head (
                id integer PRIMARY KEY CHECK (id = 1),
                seq bigint NOT NULL CHECK (seq >= 0),
                hash text NOT NULL CHECK (hash ~ '^[0-9a-f]{{64}}$'),
                at text CHECK (at IS NULL OR ({_INSTANT.format(c="at")})),
                key text,
                signature text CHECK (signature IS NULL OR signature ~ '^[0-9a-f]{{128}}$')
            )""",
            "INSERT INTO audit_head (id, seq, hash) VALUES (1, 0, '" + "0" * 64 + "')",
            """CREATE TABLE audit_keys (
                key text PRIMARY KEY CHECK (key ~ '^sha256:[0-9a-f]{64}$'),
                public_key text NOT NULL UNIQUE CHECK (public_key ~ '^[0-9a-f]{64}$'),
                created_at text NOT NULL,
                previous text,
                previous_signs text CHECK (previous_signs ~ '^[0-9a-f]{128}$'),
                signs_previous text CHECK (signs_previous ~ '^[0-9a-f]{128}$'),
                CHECK ((previous IS NULL) = (previous_signs IS NULL)),
                CHECK ((previous IS NULL) = (signs_previous IS NULL))
            )""",
            f"""CREATE TABLE audit_checkpoints (
                seq bigint PRIMARY KEY CHECK (seq >= 1),
                hash text NOT NULL CHECK (hash ~ '^[0-9a-f]{{64}}$'),
                at text NOT NULL CHECK ({_INSTANT.format(c="at")}),
                key text NOT NULL CHECK (key ~ '^sha256:[0-9a-f]{{64}}$'),
                signature text NOT NULL CHECK (signature ~ '^[0-9a-f]{{128}}$')
            )""",
            """CREATE FUNCTION audit_remove_through(through bigint) RETURNS bigint
                LANGUAGE plpgsql AS $body$
                DECLARE removed bigint;
                BEGIN
                    EXECUTE format('GRANT DELETE ON TABLE audit_events TO %I', current_user);
                    DELETE FROM audit_events WHERE seq <= through;
                    GET DIAGNOSTICS removed = ROW_COUNT;
                    EXECUTE format('REVOKE DELETE ON TABLE audit_events FROM %I', current_user);
                    RETURN removed;
                END
                $body$""",
            "REVOKE UPDATE, DELETE, TRUNCATE ON audit_events, audit_checkpoints, audit_keys "
            "FROM CURRENT_USER",
        ),
        (
            f"""CREATE TABLE credentials (
                user_id text PRIMARY KEY REFERENCES users (id),
                hash text NOT NULL CHECK (
                    char_length(hash) BETWEEN 32 AND 512
                    AND hash ~ '^[$]argon2id[$][A-Za-z0-9$=,+/]+$'
                ),
                temporary boolean NOT NULL,
                expires_at text CHECK (
                    expires_at IS NULL OR ({_INSTANT.format(c="expires_at")})
                ),
                changed_at text NOT NULL CHECK ({_INSTANT.format(c="changed_at")}),
                CHECK (temporary = (expires_at IS NOT NULL))
            )""",
            f"""CREATE TABLE sessions (
                id_hash text PRIMARY KEY CHECK (id_hash ~ '^[0-9a-f]{{64}}$'),
                user_id text NOT NULL REFERENCES users (id),
                authz_version bigint NOT NULL CHECK (authz_version >= 1),
                created_at text NOT NULL CHECK ({_INSTANT.format(c="created_at")}),
                seen_at text NOT NULL CHECK ({_INSTANT.format(c="seen_at")}),
                password_change_only boolean NOT NULL
            )""",
            "CREATE INDEX sessions_user ON sessions (user_id)",
            f"""CREATE TABLE login_delays (
                key text PRIMARY KEY CHECK (key ~ '^[0-9a-f]{{64}}$'),
                account boolean NOT NULL,
                failures bigint NOT NULL CHECK (failures >= 1),
                failed_at text NOT NULL CHECK ({_INSTANT.format(c="failed_at")}),
                delayed_until text NOT NULL CHECK ({_INSTANT.format(c="delayed_until")})
            )""",
            "CREATE INDEX login_delays_age ON login_delays (failed_at)",
            """CREATE TABLE sign_in_settings (
                id integer PRIMARY KEY CHECK (id = 1),
                absolute_seconds bigint NOT NULL CHECK (absolute_seconds >= 1),
                idle_seconds bigint NOT NULL CHECK (
                    idle_seconds >= 1 AND idle_seconds <= absolute_seconds
                ),
                public_origin text CHECK (
                    public_origin IS NULL OR char_length(public_origin) <= 300
                ),
                trusted_proxies text NOT NULL CHECK (char_length(trusted_proxies) <= 8192)
            )""",
            "INSERT INTO sign_in_settings VALUES (1, 43200, 3600, NULL, '[]')",
        ),
        (
            f"""CREATE TABLE oidc_identities (
                issuer text NOT NULL CHECK (
                    char_length(issuer) BETWEEN 9 AND 512 AND issuer LIKE 'https://%'
                ),
                subject text NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 255),
                provider text NOT NULL CHECK (char_length(provider) BETWEEN 1 AND 32),
                user_id text NOT NULL REFERENCES users (id),
                linked_at text NOT NULL CHECK ({_INSTANT.format(c="linked_at")}),
                PRIMARY KEY (issuer, subject)
            )""",
            "CREATE INDEX oidc_identities_user ON oidc_identities (user_id)",
            f"""CREATE TABLE oidc_bindings (
                reference text PRIMARY KEY CHECK (reference ~ '^[0-9a-f]{{64}}$'),
                provider text NOT NULL CHECK (char_length(provider) BETWEEN 1 AND 32),
                state text NOT NULL CHECK (char_length(state) = 43),
                nonce text NOT NULL CHECK (char_length(nonce) = 43),
                verifier text NOT NULL CHECK (char_length(verifier) = 43),
                created_at text NOT NULL CHECK ({_INSTANT.format(c="created_at")}),
                refreshing text CHECK (refreshing IS NULL OR refreshing ~ '^[0-9a-f]{{64}}$')
            )""",
            "CREATE INDEX oidc_bindings_age ON oidc_bindings (created_at)",
            f"""CREATE TABLE oidc_sessions (
                session text PRIMARY KEY REFERENCES sessions (id_hash) ON DELETE CASCADE,
                provider text NOT NULL CHECK (char_length(provider) BETWEEN 1 AND 32),
                issuer text NOT NULL,
                subject text NOT NULL,
                refreshed_at text NOT NULL CHECK ({_INSTANT.format(c="refreshed_at")})
            )""",
            """CREATE TABLE oidc_mapping_state (
                provider text PRIMARY KEY CHECK (char_length(provider) BETWEEN 1 AND 32),
                digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$')
            )""",
            """CREATE TABLE oidc_grants (
                user_id text NOT NULL REFERENCES users (id),
                project text NOT NULL,
                group_name text NOT NULL,
                provider text NOT NULL CHECK (char_length(provider) BETWEEN 1 AND 32),
                PRIMARY KEY (user_id, project, group_name)
            )""",
        ),
        (
            f"""CREATE TABLE attributes (
                project text NOT NULL,
                user_id text NOT NULL,
                name text NOT NULL CHECK (
                    name ~ '^[a-z][a-z0-9_]{{0,62}}$'
                    AND name NOT IN ('user_id', 'user', 'project', 'role', 'groups')
                ),
                type text NOT NULL CHECK (type IN ('text', 'integer', 'boolean', 'list')),
                value text NOT NULL CHECK (char_length(value) BETWEEN 1 AND 1100000),
                set_at text NOT NULL CHECK ({_INSTANT.format(c="set_at")}),
                PRIMARY KEY (project, user_id, name),
                FOREIGN KEY (project, user_id) REFERENCES memberships (project, user_id)
                    ON DELETE CASCADE
            )""",
        ),
        (
            """CREATE TABLE query_limits (
                role text PRIMARY KEY CHECK (role IN ('builder', 'author', 'viewer')),
                max_concurrent integer NOT NULL CHECK (max_concurrent BETWEEN 1 AND 64),
                max_run_ms integer NOT NULL CHECK (max_run_ms BETWEEN 1000 AND 3600000)
            )""",
            "INSERT INTO query_limits VALUES ('builder', 4, 120000), ('author', 2, 60000), "
            "('viewer', 2, 30000)",
        ),
        # 7: an opaque id for each OIDC link, and each caller's budget of lookups (#446).
        (
            "ALTER TABLE oidc_identities ADD COLUMN link_id text",
            "UPDATE oidc_identities "
            "SET link_id = replace(CAST(gen_random_uuid() AS text), '-', '')",
            "ALTER TABLE oidc_identities ALTER COLUMN link_id SET NOT NULL",
            "ALTER TABLE oidc_identities ADD CONSTRAINT oidc_identities_link_id "
            "CHECK (link_id ~ '^[0-9a-f]{32}$')",
            "ALTER TABLE oidc_identities ADD CONSTRAINT oidc_identities_link_unique "
            "UNIQUE (link_id)",
            f"""CREATE TABLE lookup_budgets (
                user_id text PRIMARY KEY REFERENCES users (id),
                window_start text NOT NULL CHECK ({_INSTANT.format(c="window_start")}),
                used bigint NOT NULL CHECK (used >= 1)
            )""",
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
        superuser = connection.run("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if superuser != [[False]]:
            connection.close()
            raise AccessError(
                ACCESS_STORE_UNSAFE,
                "The access store's role is a superuser, which no grant can restrict.",
                {},
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

    def transaction(self, *, read_only: bool = False) -> AbstractContextManager[Transaction]:
        if read_only:
            return self._transaction("START TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        return self._serialized("START TRANSACTION ISOLATION LEVEL SERIALIZABLE")

    def remove_audit_through(self, transaction: Transaction, seq: int) -> None:
        """The role holds no DELETE grant on the events: the dedicated function takes it for the
        removal and gives it up again, in the same transaction."""
        transaction.rows("SELECT audit_remove_through(:seq)", seq=seq)

    def migration_transaction(self) -> AbstractContextManager[Transaction]:
        """The schema change runs at `READ COMMITTED`, under the same write lock."""
        return self._serialized("START TRANSACTION ISOLATION LEVEL READ COMMITTED")

    @contextmanager
    def _serialized(self, begin: str) -> Iterator[Transaction]:
        """Take the store's write lock before the transaction starts, so its snapshot comes after
        the previous writer's commit: one writer at a time, as SQLite's `BEGIN IMMEDIATE`. A
        `SERIALIZABLE` snapshot taken while waiting for a row would not see that commit."""
        connection = self._connection()
        key = f"access:{self.settings.schema}"
        connection.run("SELECT pg_advisory_lock(hashtext(:key))", key=key)
        try:
            with self._transaction(begin) as made:
                yield made
        finally:
            with suppress(InterfaceError):
                connection.run("SELECT pg_advisory_unlock(hashtext(:key))", key=key)

    @contextmanager
    def _transaction(self, begin: str) -> Iterator[Transaction]:
        connection = self._connection()
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
