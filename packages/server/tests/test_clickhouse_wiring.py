"""The ClickHouse engine's wiring in `server` (P2-31 PR 4, #225; ADR-0027 §4, §7): the
development switch, the engine entries, the model compiled against the engine's tables, the
evidence schedule and the serve process's connections, without a ClickHouse server (the ones
that need one are in `tests/conformance/test_clickhouse_engine.py`)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from orvex.contracts.project.v1 import ClickhouseConnection
from orvex.contracts.store.v1 import TablesResult
from orvex.semantic.evidence import EvidenceRecord
from orvex.server import cli, clickhouse_engines, development_switch
from orvex.server.clickhouse_engines import ClickHouseEngines, target
from orvex.server.clickhouse_evidence import EvidenceSchedule
from orvex.server.engine_catalog import engine_catalog, live_environment
from orvex.server.identity import load_identity
from orvex.server.project import load_project
from orvex.server.semantic_model import ExternalTables, Loaded, load_semantic
from orvex.store.errors import StoreError

from .test_lake_separation import make_project
from .test_semantic_model import VENDORS, codes, extension, model, opened, ran

IDENTITY = load_identity()
AT = dt.datetime(2026, 11, 8, tzinfo=dt.UTC)
WAREHOUSE = (
    "  warehouse:\n    type: clickhouse\n    host: localhost\n    port: 8123\n"
    "    database: billing\n    user: reader\n    password: ${env:WAREHOUSE_PASSWORD}\n"
    "    tls: false\n    read_only: true\n"
)
SAME = WAREHOUSE.replace("  warehouse:", "  warehouse_again:").replace("reader", "other")


def test_the_switch_is_named_from_the_identity_and_on_only_when_exactly_one() -> None:
    name = development_switch.variable(IDENTITY, development_switch.CLICKHOUSE_SUFFIX)
    assert name == IDENTITY.slug.upper().replace("-", "_") + "_DEV_CLICKHOUSE"
    assert development_switch.clickhouse(IDENTITY, {name: "1"})
    for off in ({}, {name: ""}, {name: "0"}, {name: "true"}, {name: " 1"}):
        assert not development_switch.clickhouse(IDENTITY, off)
    # Each engine has its own switch: one never turns the other on.
    assert not development_switch.clickhouse(IDENTITY, {development_switch.variable(IDENTITY): "1"})


def connections(tmp_path: Path, *entries: str) -> dict[str, object]:
    return dict(load_project(make_project(tmp_path / "p", *entries)).connections)


def test_every_clickhouse_connection_is_a_live_engine_and_one_database_is_one_environment(
    tmp_path: Path,
) -> None:
    found = engine_catalog(connections(tmp_path, WAREHOUSE, SAME))
    entry = found.entries["warehouse"]
    assert (entry.kind, entry.route) == ("clickhouse", "live")
    assert "detail_pages" in entry.capabilities
    assert entry.environment == found.entries["warehouse_again"].environment
    assert entry.environment == "localhost:8123/billing"
    assert "reader" not in entry.environment
    assert "PASSWORD" not in entry.environment


def test_the_connection_s_target_names_its_password_variable_never_its_value(
    tmp_path: Path,
) -> None:
    connection = connections(tmp_path, WAREHOUSE)["warehouse"]
    assert isinstance(connection, ClickhouseConnection)
    found = target(connection)
    assert (found.host, found.port, found.database, found.user) == (
        "localhost",
        8123,
        "billing",
        "reader",
    )
    assert (found.password_env, found.tls, found.ca_file) == ("WAREHOUSE_PASSWORD", False, None)
    assert live_environment(connection) == "localhost:8123/billing"


# --- the model, compiled against the engine's tables ----------------------------------------


def on_warehouse(second: str | None = None, related: bool = False) -> str:
    document = json.loads(model(source="readings"))
    document["datasets"][0]["custom_extensions"] = [
        extension({"element": "dataset", "connection": "warehouse"})
    ]
    if second is not None:
        managed = json.loads(model(source=second))["datasets"][0] | {"name": "managed_copy"}
        document["datasets"].append(managed)
    if related:
        document["relationships"] = [
            {
                "name": "copy_of",
                "from": "consumption",
                "to": "managed_copy",
                "from_columns": ["consumer_id"],
                "to_columns": ["consumer_id"],
            }
        ]
    return json.dumps(document)


def warehouse_tables() -> TablesResult:
    columns = [
        {"name": "consumer_id", "type": "VARCHAR", "nullable": False},
        {"name": "tariff", "type": "VARCHAR", "nullable": True},
        {"name": "kwh", "type": "DECIMAL(18,4)", "nullable": True},
    ]
    return TablesResult.model_validate(
        {
            "kind": "tables_result",
            "snapshot": 0,
            "tables": [{"schema": "main", "table": "readings", "columns": columns}],
        }
    )


def load(tmp_path: Path, document: str, external: dict[str, ExternalTables] | None) -> Loaded:
    ran(tmp_path, **{"semantic/m.ossie.json": document})
    text = (tmp_path / "connections.yaml").read_text()
    (tmp_path / "connections.yaml").write_text(text + WAREHOUSE)
    with opened(tmp_path) as managed:
        return load_semantic(
            tmp_path,
            managed,
            managed.current_snapshot().id,
            VENDORS,
            audience="builder",
            connections=load_project(tmp_path).connections,
            external=external,
        )


def test_with_the_switch_off_a_dataset_on_clickhouse_is_unsupported_route(tmp_path: Path) -> None:
    found = load(tmp_path, on_warehouse(), None)
    assert found.model is None
    assert codes(found) == [("UNSUPPORTED_OPERATION", "semantic/m.ossie.json")]
    assert found.problems[0].message.startswith("operation: route")
    assert "warehouse" in found.problems[0].message


def test_with_the_engine_enabled_the_dataset_is_live_on_its_connection(tmp_path: Path) -> None:
    found = load(
        tmp_path, on_warehouse(), {"warehouse": ExternalTables(warehouse_tables(), frozenset())}
    )
    assert found.problems == ()
    assert found.model is not None
    entry = found.model.routing.of("consumption")
    assert entry is not None
    assert (entry.connection, entry.route) == ("warehouse", "live")


def test_a_relationship_between_clickhouse_and_the_managed_lake_is_cross_engine(
    tmp_path: Path,
) -> None:
    given = {"warehouse": ExternalTables(warehouse_tables(), frozenset())}
    found = load(tmp_path, on_warehouse(second="marts.consumption", related=True), given)
    assert found.model is None
    assert codes(found) == [("CROSS_ENGINE_QUERY", "semantic/m.ossie.json")]


def test_a_table_with_a_column_of_no_exact_type_is_refused_naming_the_column(
    tmp_path: Path,
) -> None:
    refused = StoreError(
        "UNSUPPORTED_SOURCE_TYPE",
        "Column readings.tags has type Array(String), which has no exact type.",
        {"table": "readings", "column": "tags", "type": "Array(String)"},
    )
    given = {
        "warehouse": ExternalTables(
            warehouse_tables(), frozenset(), None, {"main.readings": refused}
        )
    }
    found = load(tmp_path, on_warehouse(), given)
    assert codes(found) == [("UNSUPPORTED_SOURCE_TYPE", "semantic/m.ossie.json")]
    assert "readings.tags" in found.problems[0].message


# --- the evidence schedule and the serve process ----------------------------------------------


def test_the_evidence_schedule_is_daily_by_default_and_never_under_an_hour() -> None:
    found = EvidenceSchedule("warehouse", None, lambda now: ())
    assert found.every == dt.timedelta(days=1)
    assert EvidenceSchedule("warehouse", "2h", lambda now: ()).every == dt.timedelta(hours=2)
    with pytest.raises(ValueError, match="shorter"):
        EvidenceSchedule("warehouse", "30m", lambda now: ())


def test_a_failed_verification_keeps_no_record() -> None:
    calls: list[int] = []

    record = EvidenceRecord("dataset", "readings", "unique", 0, "f", "verified", {}, AT)

    def verification(now: dt.datetime) -> tuple[EvidenceRecord, ...]:
        calls.append(1)
        if len(calls) > 1:
            raise StoreError("STORE_CONNECTION_FAILED", "gone", {})
        return (record,)

    schedule = EvidenceSchedule("warehouse", "1h", verification)
    now = AT
    assert schedule.due(now)
    assert schedule.check(now) == (record,)
    assert not schedule.due(now + dt.timedelta(minutes=59))
    with pytest.raises(StoreError):
        schedule.check(now + dt.timedelta(hours=1))
    assert schedule.records() == ()  # nothing stays verified on records not renewed


def test_a_connection_that_fails_verification_is_its_error_for_every_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WAREHOUSE_PASSWORD", raising=False)
    with clickhouse_engines.opened(connections(tmp_path, WAREHOUSE), timeout_ms=1000) as engines:
        found = engines.pinned()
    assert set(found) == {"warehouse"}
    error = found["warehouse"]
    assert isinstance(error, StoreError)
    assert error.code == "STORE_SECRET_MISSING"


def test_with_the_switch_off_serve_connects_no_clickhouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = development_switch.variable(IDENTITY, development_switch.CLICKHOUSE_SUFFIX)
    monkeypatch.delenv(name, raising=False)
    folder = make_project(tmp_path / "p", WAREHOUSE)
    with cli.connected_clickhouse(folder, IDENTITY) as engines:
        assert engines is None
    monkeypatch.setenv(name, "1")
    monkeypatch.delenv("WAREHOUSE_PASSWORD", raising=False)
    with cli.connected_clickhouse(folder, IDENTITY) as engines:
        assert isinstance(engines, ClickHouseEngines)
        assert len(engines) == 1
