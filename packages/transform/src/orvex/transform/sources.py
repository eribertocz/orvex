"""Turn a project's connections into the store's source attachments (P1-06, P1-07).

The project loader (contract `project` v1) has already resolved relative paths against the
project folder; `~` is expanded here for paths given directly. Passwords stay references: a
`${env:NAME}` becomes `password_env: NAME`, and only the store reads the variable; so does a
PostgreSQL source's `ssl_root_cert` (#122).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from orvex.contracts.project.v1 import (
    ClickhouseConnection,
    CsvConnection,
    DuckdbConnection,
    ParquetConnection,
    PostgresConnection,
    SqliteConnection,
)
from orvex.contracts.store.v1 import (
    FileSourceAttachment,
    PostgresSourceAttachment,
    SqliteSourceAttachment,
)

type Connection = (
    ParquetConnection
    | CsvConnection
    | DuckdbConnection
    | SqliteConnection
    | PostgresConnection
    | ClickhouseConnection
)
type Attachment = FileSourceAttachment | SqliteSourceAttachment | PostgresSourceAttachment

_REFERENCE = re.compile(r"\$\{env:([A-Z_][A-Z0-9_]*)\}")


def source_attachments(connections: Mapping[str, Connection]) -> list[Attachment]:
    """Every connection DuckDB attaches, as a source attachment, sorted by name.

    ClickHouse is reached through extraction models instead (P1-17).
    """
    attachments: list[Attachment] = []
    for name in sorted(connections):
        connection = connections[name]
        if isinstance(connection, ParquetConnection | CsvConnection | DuckdbConnection):
            attachments.append(
                FileSourceAttachment(
                    kind="source_attachment",
                    name=name,
                    type=connection.type,
                    path=os.path.expanduser(connection.path.root),
                )
            )
        elif isinstance(connection, SqliteConnection):
            attachments.append(
                SqliteSourceAttachment(
                    kind="source_attachment",
                    name=name,
                    type="sqlite",
                    path=os.path.expanduser(connection.path.root),
                )
            )
        elif isinstance(connection, PostgresConnection):
            reference = _REFERENCE.fullmatch(connection.password.root)
            if reference is None:  # the project contract allows nothing else
                raise ValueError(f"connection {name}: the password is not a ${{env:NAME}}")
            attachments.append(
                PostgresSourceAttachment.model_validate(
                    {
                        "kind": "source_attachment",
                        "name": name,
                        "type": "postgres",
                        "host": connection.host,
                        "port": connection.port,
                        "database": connection.database,
                        "user": connection.user,
                        "password_env": reference.group(1),
                        **({"schema": connection.schema_} if connection.schema_ else {}),
                        **({"sslmode": connection.ssl_mode} if connection.ssl_mode else {}),
                        **(
                            {"sslrootcert": connection.ssl_root_cert}
                            if connection.ssl_mode == "verify-full" and connection.ssl_root_cert
                            else {}
                        ),
                    }
                )
            )
    return attachments
