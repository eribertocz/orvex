"""The synthetic project the transform kit runs on: its files, written to a fresh temporary
folder for each case, discovered through the contract, and the values its cases share."""

from __future__ import annotations

import datetime as dt
import tempfile
from collections.abc import Mapping
from pathlib import Path
from zoneinfo import ZoneInfo

from orvex.contracts.kit_client import KitClient
from orvex.contracts.project.v1 import ParquetConnection
from orvex.contracts.transform.models import Columns, Discovery, Model

LA_PAZ = ZoneInfo("America/La_Paz")
NEW_YORK = ZoneInfo("America/New_York")
READINGS_SQL = (
    "SELECT 1::BIGINT AS id, 12.5::DECIMAL(18,4) AS kwh, "
    "TIMESTAMPTZ '2026-03-08 01:30:00+00' AS read_at"
)
FILES = {
    "models/raw/readings.sql": READINGS_SQL,
    "models/staging/active.sql": "SELECT id, kwh, read_at FROM raw.readings WHERE kwh > 0",
    "models/staging/active.yaml": "version: 1\nmaterialized: view\n",
    "models/marts/fresh.sql": "SELECT * FROM staging.active",
    "models/marts/fresh.yaml": (
        "version: 1\nfreshness: 10m\ntests:\n  - not_null: [id]\n"
        "reconciliation:\n  - {name: total, measure: sum(kwh), source: feed.data}\n"
    ),
    "models/marts/weekly.sql": "SELECT * FROM raw.readings",
    "models/marts/weekly.yaml": (
        "version: 1\nfull_refresh: {cron: '0 3 * * 0', time_zone: America/New_York}\n"
    ),
    "models/marts/daily.sql": "SELECT id, kwh, read_at FROM raw.readings",
    "models/marts/daily.yaml": (
        "version: 1\nmaterialized: incremental\n"
        "incremental: {strategy: merge, time_column: read_at, unique_key: [id]}\n"
    ),
}
COLUMNS: Columns = (
    ("id", "BIGINT"),
    ("kwh", "DECIMAL(18,4)"),
    ("read_at", "TIMESTAMP WITH TIME ZONE"),
)
FEED = ParquetConnection.model_validate({"type": "parquet", "path": "/data/feed"})
CONNECTIONS = {"feed": FEED}


def at(text: str, zone: ZoneInfo = LA_PAZ) -> dt.datetime:
    return dt.datetime.fromisoformat(text).replace(tzinfo=zone)


def discover(
    client: KitClient,
    files: Mapping[str, str] = FILES,
    connections: Mapping[str, object] = CONNECTIONS,
) -> Discovery:
    """The models of `files`, discovered in a temporary folder removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="transform-kit-") as folder:
        for relative, text in files.items():
            path = Path(folder) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        found: Discovery = client.discover_models(folder, dict(connections))
    return found


def project(client: KitClient) -> dict[str, Model]:
    """The kit's models by name; the project must have no problem."""
    found = discover(client)
    expect(found.problems == (), f"the kit's project has problems: {found.problems}")
    return {model.name: model for model in found.models}


def expect(condition: bool, text: str) -> None:
    if not condition:
        raise AssertionError(text)
