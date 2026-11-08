"""Copies of a SQLite source, made with SQLite's backup API (pages only, no SQL run on the
original): the lake's one copy per open lake, and a worker's private copy with its columns.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from orvex.contracts.store.v1 import SqliteSourceAttachment
from orvex.store.errors import StoreError, _source_not_found


def sqlite_backup(source: SqliteSourceAttachment, copy: Path) -> None:
    """A consistent copy of the SQLite file at `copy`, WAL included, by SQLite's backup API:
    pages only, no statement and no schema SQL is run on the original."""
    path = Path(source.path)
    if not path.is_file():
        raise _source_not_found(source)
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
