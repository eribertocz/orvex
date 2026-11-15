"""Which access store an instance uses, chosen by its mode and nothing else (#436): the one
configuration `admin create` and the server-mode start share, as the access contract's store
location.

- `local`: SQLite, `access.sqlite` in the instance directory.
- `server`: PostgreSQL, always. `<SLUG>_ACCESS_POSTGRES` holds the connection and
  `<SLUG>_ACCESS_POSTGRES_PASSWORD` its password.

- **No fallback** (Owner, 2026-11-10). Server mode without both variables is refused with
  `ACCESS_STORE_NOT_CONFIGURED`. It never falls back to SQLite, which would make a second,
  silent set of users.
- **The connection's rules** are the access store's, checked when the instance is opened: a
  restricted libpq string, and `sslmode=verify-full` (certificate and host name checked) for any
  server that is not this machine. Nothing is weakened when a variable is wrong: the opening is
  refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from orvex.contracts.access.errors import AccessError
from orvex.contracts.access.values import PostgresLocation, SqliteLocation
from orvex.server.identity import Identity

ACCESS_STORE_NOT_CONFIGURED = "ACCESS_STORE_NOT_CONFIGURED"
SQLITE_FILE = "access.sqlite"

type Mode = Literal["local", "server"]


def postgres_variables(identity: Identity) -> tuple[str, str]:
    """`<SLUG>_ACCESS_POSTGRES` and `<SLUG>_ACCESS_POSTGRES_PASSWORD`, from the identity slug."""
    prefix = identity.slug.upper().replace("-", "_")
    return f"{prefix}_ACCESS_POSTGRES", f"{prefix}_ACCESS_POSTGRES_PASSWORD"


def access_store(
    mode: Mode, identity: Identity, instance: Path, environment: Mapping[str, str]
) -> SqliteLocation | PostgresLocation:
    if mode == "local":
        return SqliteLocation(str(instance / SQLITE_FILE))
    connection_variable, password_variable = postgres_variables(identity)
    connection = environment.get(connection_variable, "")
    password = environment.get(password_variable, "")
    if not connection or not password:
        raise AccessError(
            ACCESS_STORE_NOT_CONFIGURED,
            f"Server mode keeps its users in PostgreSQL: set {connection_variable} and "
            f"{password_variable}.",
            {"variables": f"{connection_variable} {password_variable}"},
        )
    return PostgresLocation(connection, password)
