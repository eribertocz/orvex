"""P1-06, P1-07: connections become store source attachments."""

from __future__ import annotations

from pathlib import Path

import pytest
from orvex.contracts.project.v1 import (
    CsvConnection,
    DuckdbConnection,
    ParquetConnection,
    PostgresConnection,
    SqliteConnection,
)
from orvex.contracts.store.v1 import PostgresSourceAttachment, SqliteSourceAttachment
from orvex.transform.sources import Connection, source_attachments


def test_file_connections_become_sources(tmp_path: Path) -> None:
    connections: dict[str, Connection] = {
        "tariffs": CsvConnection.model_validate(
            {"type": "csv", "path": str(tmp_path / "tariffs.csv")}
        ),
        "archive": ParquetConnection.model_validate(
            {"type": "parquet", "path": str(tmp_path / "r" / "*.parquet")}
        ),
        "legacy": DuckdbConnection.model_validate(
            {"type": "duckdb", "path": str(tmp_path / "l.duckdb"), "read_only": True}
        ),
        "production": PostgresConnection.model_validate(
            {
                "type": "postgres",
                "host": "db.example.invalid",
                "port": 5432,
                "database": "billing",
                "user": "reader",
                "password": "${env:PRODUCTION_PASSWORD}",
                "read_only": True,
            }
        ),
    }
    sources = source_attachments(connections)
    assert [(s.name, s.type) for s in sources] == [
        ("archive", "parquet"),
        ("legacy", "duckdb"),
        ("production", "postgres"),
        ("tariffs", "csv"),
    ]
    paths = {s.name: getattr(s, "path", None) for s in sources}
    assert paths["archive"] == str(tmp_path / "r" / "*.parquet")
    production = sources[2]
    assert isinstance(production, PostgresSourceAttachment)
    assert production.password_env == "PRODUCTION_PASSWORD"
    assert production.host == "db.example.invalid"
    assert production.schema_ is None


def test_sqlite_and_postgres_schema(tmp_path: Path) -> None:
    connections: dict[str, Connection] = {
        "field": SqliteConnection.model_validate(
            {"type": "sqlite", "path": str(tmp_path / "f.sqlite"), "read_only": True}
        ),
        "production": PostgresConnection.model_validate(
            {
                "type": "postgres",
                "host": "db.example.invalid",
                "port": 5432,
                "database": "billing",
                "user": "reader",
                "password": "${env:PRODUCTION_PASSWORD}",
                "read_only": True,
                "schema": "public",
            }
        ),
    }
    field, production = source_attachments(connections)
    assert isinstance(field, SqliteSourceAttachment)
    assert field.path == str(tmp_path / "f.sqlite")
    assert isinstance(production, PostgresSourceAttachment)
    assert production.schema_ == "public"
    assert "PRODUCTION_PASSWORD" in repr(production)


def test_home_is_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    connection = CsvConnection.model_validate({"type": "csv", "path": "~/data/a.csv"})
    (attachment,) = source_attachments({"a": connection})
    assert not isinstance(attachment, PostgresSourceAttachment)
    assert attachment.path == str(tmp_path / "data" / "a.csv")
