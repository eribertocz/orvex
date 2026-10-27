"""P1-06: file connections become store source attachments."""

from __future__ import annotations

from pathlib import Path

import pytest
from orvex.contracts.project.v1 import (
    CsvConnection,
    DuckdbConnection,
    ParquetConnection,
    PostgresConnection,
)
from orvex.transform.sources import Connection, file_sources


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
    sources = file_sources(connections)
    assert [(s.name, s.type, s.path) for s in sources] == [
        ("archive", "parquet", str(tmp_path / "r" / "*.parquet")),
        ("legacy", "duckdb", str(tmp_path / "l.duckdb")),
        ("tariffs", "csv", str(tmp_path / "tariffs.csv")),
    ]


def test_home_is_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    connection = CsvConnection.model_validate({"type": "csv", "path": "~/data/a.csv"})
    sources = file_sources({"a": connection})
    assert sources[0].path == str(tmp_path / "data" / "a.csv")
