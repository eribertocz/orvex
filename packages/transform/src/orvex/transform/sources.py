"""Turn a project's file connections into the store's source attachments (P1-06).

The project loader (contract `project` v1) has already resolved relative paths against the
project folder; `~` is expanded here for paths given directly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from orvex.contracts.project.v1 import (
    ClickhouseConnection,
    CsvConnection,
    DuckdbConnection,
    ParquetConnection,
    PostgresConnection,
    SqliteConnection,
)
from orvex.contracts.store.v1 import SourceAttachment

type Connection = (
    ParquetConnection
    | CsvConnection
    | DuckdbConnection
    | SqliteConnection
    | PostgresConnection
    | ClickhouseConnection
)


def file_sources(connections: Mapping[str, Connection]) -> list[SourceAttachment]:
    """The Parquet, CSV and DuckDB connections as source attachments, sorted by name.

    Database connections (SQLite, PostgreSQL, ClickHouse) are attached by later steps.
    """
    attachments: list[SourceAttachment] = []
    for name in sorted(connections):
        connection = connections[name]
        if isinstance(connection, ParquetConnection | CsvConnection | DuckdbConnection):
            attachments.append(
                SourceAttachment(
                    kind="source_attachment",
                    name=name,
                    type=connection.type,
                    path=os.path.expanduser(connection.path.root),
                )
            )
    return attachments
