"""P1-08: model discovery and the model-config sidecar."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from orvex.contracts.project.v1 import ClickhouseConnection, CsvConnection, PostgresConnection
from orvex.transform.models import Discovery, discover_models
from orvex.transform.sources import Connection

SIDECAR = """\
version: 1
description: Meter readings, one row per reading
owner: eriberto
materialized: incremental
incremental:
  strategy: merge
  unique_key: [reading_id]
  time_column: updated_at
  lookback: 1h
  partition_by: [reading_month]
  filter: auto
freshness: 10m
full_refresh:
  cron: "0 3 * * 0"
  time_zone: America/La_Paz
on_schema_change: fail
tests:
  - unique: [reading_id]
  - not_null: [reading_id, consumer_id, read_at]
  - accepted_range: { column: kwh, min: 0 }
  - accepted_values: { column: status, values: [valid, estimated, voided] }
  - relationship: { column: consumer_id, to: staging.consumers, field: consumer_id }
reconciliation:
  - name: kwh_by_month
    measure: sum(kwh)
    group_by: [date_trunc('month', read_at)]
    source: production.readings
    tolerance: 0
"""


def connections() -> dict[str, Connection]:
    return {
        "tariffs": CsvConnection.model_validate({"type": "csv", "path": "/data/t.csv"}),
        "events": ClickhouseConnection.model_validate(
            {
                "type": "clickhouse",
                "host": "ch.example.invalid",
                "port": 8443,
                "database": "events",
                "user": "reader",
                "password": "${env:EVENTS_PASSWORD}",
                "read_only": True,
            }
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


def write(folder: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


def discover(folder: Path) -> Discovery:
    return discover_models(folder, connections())


def codes(found: Discovery) -> list[tuple[str, str]]:
    return [(item.code.root, item.details.file) for item in found.problems]


def test_models_are_found_named_and_defaulted(tmp_path: Path) -> None:
    found = discover(
        write(
            tmp_path,
            {
                "models/staging/readings.sql": "SELECT 1 AS reading_id",
                "models/staging/consumers.sql": "SELECT 'C1' AS consumer_id",
                "models/staging/consumers.yaml": "version: 1\nmaterialized: view\n",
                "models/marts/billing.sql": "SELECT 1 AS x",
                "models/README.md": "notes",
                "models/staging/.hidden.sql": "not a model",
            },
        )
    )
    assert found.problems == ()
    assert [model.name for model in found.models] == [
        "marts.billing",
        "staging.consumers",
        "staging.readings",
    ]
    consumers = found.models[1]
    readings = found.models[2]
    assert consumers.config.materialized == "view"
    assert consumers.config_file == "models/staging/consumers.yaml"
    assert readings.config.materialized == "table"
    assert readings.config.on_schema_change == "fail"
    assert readings.config_file is None
    assert readings.sql == "SELECT 1 AS reading_id"


def test_sidecar_with_every_field_validates(tmp_path: Path) -> None:
    found = discover(
        write(
            tmp_path,
            {
                "models/staging/readings.sql": "SELECT 1 AS x",
                "models/staging/readings.yaml": SIDECAR,
            },
        )
    )
    assert found.problems == ()
    config = found.models[0].config
    assert config.incremental is not None
    assert config.incremental.strategy == "merge"
    assert config.tests is not None
    assert len(config.tests) == 5


@pytest.mark.parametrize(
    ("change", "pointer"),
    [
        (("owner: eriberto", "owner: eriberto\ncolour: blue"), "/colour"),
        (("freshness: 10m", "freshness: 10 minutes"), "/freshness"),
        (("strategy: merge", "strategy: upsert"), "/incremental/strategy"),
        (("  - unique: [reading_id]", "  - unique_together: [reading_id]"), "/tests/0"),
    ],
    ids=["unknown-key", "bad-duration", "unknown-strategy", "unknown-test"],
)
def test_sidecar_violations_report_pointer_and_line(
    tmp_path: Path, change: tuple[str, str], pointer: str
) -> None:
    sidecar = SIDECAR.replace(*change)
    found = discover(
        write(
            tmp_path,
            {
                "models/staging/readings.sql": "SELECT 1 AS x",
                "models/staging/readings.yaml": sidecar,
            },
        )
    )
    assert found.models == ()
    (item,) = found.problems
    assert item.code.root == "MODEL_CONFIG_INVALID"
    assert item.details.file == "models/staging/readings.yaml"
    assert (item.details.path or "").startswith(pointer)
    assert item.details.line is not None


def test_every_problem_is_reported_at_once(tmp_path: Path) -> None:
    found = discover(
        write(
            tmp_path,
            {
                "models/staging/orphan.yaml": "version: 1\n",
                "models/staging/forecast.py": "print('x')",
                "models/staging/broken.sql": "SELEC 1",
                "models/staging/two.sql": "SELECT 1; SELECT 2",
                "models/production/readings.sql": "SELECT 1",
                "models/staging/fine.sql": "SELECT 1 AS x",
            },
        )
    )
    assert codes(found) == [
        ("MODEL_SCHEMA_CONFLICT", "models/production"),
        ("INVALID_MODEL_SQL", "models/staging/broken.sql"),
        ("UNSUPPORTED_MODEL_KIND", "models/staging/forecast.py"),
        ("ORPHAN_MODEL_CONFIG", "models/staging/orphan.yaml"),
        ("INVALID_MODEL_SQL", "models/staging/two.sql"),
    ]
    assert [model.name for model in found.models] == ["staging.fine"]
    broken = found.problems[1]
    assert broken.details.line == 1


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t SELECT 1",
        "CREATE TABLE t AS SELECT 1",
        "COPY t TO 'x.csv'",
        "",
        "-- only a comment",
    ],
    ids=["insert", "ctas", "copy", "empty", "comment"],
)
def test_only_a_select_is_a_model(tmp_path: Path, sql: str) -> None:
    found = discover(write(tmp_path, {"models/staging/m.sql": sql}))
    assert codes(found) == [("INVALID_MODEL_SQL", "models/staging/m.sql")]


@pytest.mark.parametrize(
    "sql",
    ["WITH a AS (SELECT 1 AS x) SELECT x FROM a", "SELECT 1 AS x UNION ALL SELECT 2 AS x"],
    ids=["with", "union"],
)
def test_with_and_union_are_selects(tmp_path: Path, sql: str) -> None:
    found = discover(write(tmp_path, {"models/staging/m.sql": sql}))
    assert found.problems == ()


def test_extraction_model_is_not_parsed_by_duckdb(tmp_path: Path) -> None:
    found = discover(
        write(
            tmp_path,
            {
                "models/raw/events.sql": "SELECT toStartOfMonth(ts) AS month FROM events FINAL",
                "models/raw/events.yaml": "version: 1\nconnection: events\n",
            },
        )
    )
    assert found.problems == ()
    assert found.models[0].extraction


@pytest.mark.parametrize("connection", ["production", "unknown"])
def test_extraction_needs_a_clickhouse_connection(tmp_path: Path, connection: str) -> None:
    found = discover(
        write(
            tmp_path,
            {
                "models/raw/events.sql": "SELECT 1",
                "models/raw/events.yaml": f"version: 1\nconnection: {connection}\n",
            },
        )
    )
    (item,) = found.problems
    assert item.code.root == "MODEL_CONFIG_INVALID"
    assert item.details.path == "/connection"
    assert item.details.line == 2


@pytest.mark.parametrize(
    "relative",
    [
        "models/Staging/a.sql",
        "models/2024/a.sql",
        "models/staging/my-model.sql",
        "models/x.sql",
        "models/a/b/c.sql",
        "models/_meta/runs.sql",
        "models/staging/readings.yml",
    ],
)
def test_invalid_paths(tmp_path: Path, relative: str) -> None:
    found = discover(write(tmp_path, {relative: "SELECT 1"}))
    assert codes(found) == [("MODEL_PATH_INVALID", relative)]


def test_lake_folder_is_reserved(tmp_path: Path) -> None:
    found = discover(write(tmp_path, {"models/lake/a.sql": "SELECT 1"}))
    assert codes(found) == [("MODEL_SCHEMA_CONFLICT", "models/lake")]


def test_sidecar_yaml_rules_apply(tmp_path: Path) -> None:
    found = discover(
        write(
            tmp_path,
            {
                "models/staging/a.sql": "SELECT 1",
                "models/staging/a.yaml": "version: 1\nowner: x\nowner: y\n",
                "models/staging/b.sql": "SELECT 1",
                "models/staging/b.yaml": "version: 2\n",
            },
        )
    )
    assert codes(found) == [
        ("MODEL_CONFIG_INVALID", "models/staging/a.yaml"),
        ("MODEL_CONFIG_INVALID", "models/staging/b.yaml"),
    ]
    assert found.problems[0].details.line == 3


def test_discovery_never_writes(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "models/staging/a.sql": "SELECT 1",
            "models/staging/a.yaml": SIDECAR,
            "models/staging/orphan.yaml": "version: 1\n",
        },
    )

    def state() -> dict[str, tuple[str, int]]:
        return {
            path.relative_to(tmp_path).as_posix(): (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
            for path in tmp_path.rglob("*")
            if path.is_file()
        }

    before = state()
    discover(tmp_path)
    assert state() == before
    assert sorted(path.name for path in tmp_path.rglob("*")) == sorted(
        ["models", "staging", "a.sql", "a.yaml", "orphan.yaml"]
    )


def test_no_models_folder_means_no_models(tmp_path: Path) -> None:
    assert discover(tmp_path) == Discovery((), ())
