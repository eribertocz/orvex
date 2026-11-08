"""The existing-DuckLake engine's wiring in `server` (P2-30 PR 3, #399; ADR-0027 §3, §4, §7):
the development switch, the engine entries, the model compiled against the lake's tables, the
attachment rebuilt from the instance, and the schedule's periods."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
from orvex.contracts.project.v1 import DucklakeConnection
from orvex.contracts.store.v1 import (
    ExistingLakeAttachment,
    PostgresLakeLocation,
    Snapshot,
    SqliteLakeLocation,
    TablesResult,
)
from orvex.server import development_switch, existing_lake_attachments
from orvex.server.engine_catalog import engine_catalog, environment
from orvex.server.existing_lake_snapshots import SnapshotSchedule, period
from orvex.server.identity import load_identity
from orvex.server.instance import ProjectRegistry
from orvex.server.project import ProjectLoadError, load_project
from orvex.server.semantic_model import ExternalTables, Loaded, load_semantic

from .test_lake_separation import lake, make_project
from .test_semantic_model import VENDORS, codes, extension, model, opened, ran

IDENTITY = load_identity()
BILLING = (
    "  billing:\n    type: ducklake\n    catalog:\n      type: postgres\n"
    "      host: localhost\n      port: 5432\n      database: lakes\n"
    "      user: billing_reader\n      password: ${env:LAKE_PASSWORD}\n"
    "      schema: billing\n    data_path: s3://lakes/billing/\n"
    "    s3:\n      region: us-east-1\n      key_id: ${env:LAKE_KEY}\n"
    "      secret: ${env:LAKE_SECRET}\n    read_only: true\n"
)
SAME_LAKE = BILLING.replace("  billing:", "  billing_again:")


# --- the development switch ------------------------------------------------------------------


def test_the_switch_is_named_from_the_identity_and_on_only_when_exactly_one() -> None:
    name = development_switch.variable(IDENTITY)
    assert name == IDENTITY.slug.upper().replace("-", "_") + "_DEV_EXISTING_DUCKLAKE"
    assert development_switch.existing_ducklake(IDENTITY, {name: "1"})
    for off in ({}, {name: ""}, {name: "0"}, {name: "true"}, {name: " 1"}):
        assert not development_switch.existing_ducklake(IDENTITY, off)


# --- engine entries --------------------------------------------------------------------------


def connections(tmp_path: Path, *entries: str) -> dict[str, object]:
    folder = make_project(tmp_path / "p", *entries)
    return dict(load_project(folder).connections)


def test_every_ducklake_connection_is_an_engine_and_one_lake_is_one_environment(
    tmp_path: Path,
) -> None:
    found = engine_catalog(connections(tmp_path, BILLING, SAME_LAKE))
    assert set(found.entries) == {"billing", "billing_again"}
    entry = found.entries["billing"]
    assert (entry.kind, entry.route) == ("ducklake", "existing_ducklake")
    assert "detail_pages" not in entry.capabilities
    assert entry.environment == found.entries["billing_again"].environment
    assert "LAKE_PASSWORD" not in entry.environment
    assert "billing_reader" not in entry.environment
    assert found.others == frozenset()
    assert engine_catalog(["archive"]).entries == {}


def test_two_lakes_are_two_environments(tmp_path: Path) -> None:
    other = BILLING.replace("schema: billing", "schema: collections").replace(
        "  billing:", "  collections:"
    )
    found = connections(tmp_path, BILLING, other)
    first, second = found["billing"], found["collections"]
    assert isinstance(first, DucklakeConnection)
    assert isinstance(second, DucklakeConnection)
    assert environment(first) != environment(second)


# --- the model, compiled against the lake's tables --------------------------------------------


def on_lake(source: str = "readings", second: str | None = None) -> str:
    document = json.loads(model(source=source))
    document["datasets"][0]["custom_extensions"] = [
        extension({"element": "dataset", "connection": "billing"})
    ]
    if second is not None:
        managed = json.loads(model(source=second))["datasets"][0] | {"name": "managed_copy"}
        document["datasets"].append(managed)
    return json.dumps(document)


def tables(snapshot: int = 7) -> TablesResult:
    columns = [
        {"name": "consumer_id", "type": "VARCHAR", "nullable": False},
        {"name": "tariff", "type": "VARCHAR", "nullable": True},
        {"name": "kwh", "type": "DECIMAL(18,4)", "nullable": True},
    ]
    return TablesResult.model_validate(
        {
            "kind": "tables_result",
            "snapshot": snapshot,
            "tables": [{"schema": "main", "table": "readings", "columns": columns}],
        }
    )


def load(tmp_path: Path, document: str, external: dict[str, ExternalTables] | None) -> Loaded:
    ran(tmp_path, **{"semantic/m.ossie.json": document})
    text = (tmp_path / "connections.yaml").read_text()
    (tmp_path / "connections.yaml").write_text(text + BILLING)
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


def test_with_the_switch_off_a_dataset_on_the_lake_is_unsupported_route(tmp_path: Path) -> None:
    found = load(tmp_path, on_lake(), None)
    assert found.model is None
    assert codes(found) == [("UNSUPPORTED_OPERATION", "semantic/m.ossie.json")]
    assert found.problems[0].message.startswith("operation: route")


def test_with_the_engine_enabled_the_dataset_compiles_against_the_lakes_tables(
    tmp_path: Path,
) -> None:
    found = load(tmp_path, on_lake(), {"billing": ExternalTables(tables(), frozenset())})
    assert found.problems == ()
    assert found.model is not None
    entry = found.model.routing.of("consumption")
    assert entry is not None
    assert entry.connection == "billing"


def test_a_dataset_on_a_view_of_the_lake_is_refused(tmp_path: Path) -> None:
    given = {"billing": ExternalTables(tables(), frozenset({"main.readings"}))}
    found = load(tmp_path, on_lake(), given)
    assert codes(found) == [("STORE_SOURCE_INVALID", "semantic/m.ossie.json")]


def test_a_table_the_lake_does_not_have_is_a_reference_problem(tmp_path: Path) -> None:
    given = {"billing": ExternalTables(tables(), frozenset())}
    found = load(tmp_path, on_lake(source="nowhere"), given)
    assert codes(found) == [("OSSIE_INVALID", "semantic/m.ossie.json")]
    assert "not a lake table at snapshot 7" in found.problems[0].message


def test_one_source_name_read_on_two_engines_is_refused(tmp_path: Path) -> None:
    # The managed lake's `marts.consumption` and the lake's table under the same name.
    lake_tables = tables().model_copy(deep=True)
    lake_tables.tables[0].schema_ = "marts"
    lake_tables.tables[0].table = "consumption"
    given = {"billing": ExternalTables(lake_tables, frozenset())}
    found = load(tmp_path, on_lake(source="marts.consumption", second="marts.consumption"), given)
    assert codes(found) == [("OSSIE_INVALID", "semantic/m.ossie.json")]
    assert "two engines" in found.problems[0].message


# --- the attachment, from the instance as it stands -------------------------------------------


def test_the_attachment_is_rebuilt_from_the_instance_at_each_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for variable in ("LAKE_PASSWORD", "LAKE_KEY", "LAKE_SECRET"):
        monkeypatch.setenv(variable, f"value-{variable}")
    instance = tmp_path / "instance"
    billing = make_project(tmp_path / "billing", BILLING)
    ProjectRegistry(instance).register(billing)
    connection = load_project(billing).connections["billing"]
    assert isinstance(connection, DucklakeConnection)
    current = existing_lake_attachments.current(
        instance, billing, IDENTITY.slug, "billing", connection
    )
    first = current()
    assert isinstance(first.location, PostgresLakeLocation)
    assert first.location.password_env == "LAKE_PASSWORD"
    assert "value-" not in first.model_dump_json()  # variables, never their values
    later = make_project(tmp_path / "later", lake("later", tmp_path / "later-lake/"))
    ProjectRegistry(instance).register(later)
    second = current()
    protected = {item.root for item in second.protected_locations or []}
    assert str(later.resolve()) in protected
    assert str(later.resolve()) not in {item.root for item in first.protected_locations or []}
    assert "later_reader" in {role.root for role in second.roles_in_use}


def test_a_sqlite_lake_is_located_by_its_catalog_file(tmp_path: Path) -> None:
    text = (
        "  owned:\n    type: ducklake\n    catalog:\n      type: sqlite\n"
        "      path: lakes/owned.sqlite\n    data_path: lakes/owned/\n    read_only: true\n"
    )
    folder = make_project(tmp_path / "p", text)
    connection = load_project(folder).connections["owned"]
    assert isinstance(connection, DucklakeConnection)
    found = existing_lake_attachments.location("owned", connection)
    assert isinstance(found, SqliteLakeLocation)
    assert found.path == str((folder / "lakes/owned.sqlite").resolve())
    assert found.data_path == (folder / "lakes/owned").resolve().as_posix() + "/"


@pytest.mark.parametrize(
    "connection_string",
    [
        "DefaultEndpointsProtocol=http;AccountName=a;AccountKey=a2V5",
        "AccountName=a;AccountKey=a2V5;BlobEndpoint=http://blob.example.net",
        "defaultendpointsprotocol=https;DefaultEndpointsProtocol=http;AccountName=a",
    ],
)
def test_an_azure_lake_is_reached_only_over_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connection_string: str
) -> None:
    monkeypatch.setenv("LAKE_AZURE", connection_string)
    text = (
        "  owned:\n    type: ducklake\n    catalog:\n      type: sqlite\n"
        "      path: lakes/owned.sqlite\n    data_path: az://owned/data/\n"
        "    azure:\n      connection_string: ${env:LAKE_AZURE}\n    read_only: true\n"
    )
    connection = load_project(make_project(tmp_path / "p", text)).connections["owned"]
    assert isinstance(connection, DucklakeConnection)
    with pytest.raises(ProjectLoadError) as caught:
        existing_lake_attachments.location("owned", connection)
    assert caught.value.error.code.root == "SOURCE_TLS_REQUIRED"
    assert "a2V5" not in caught.value.error.message


# --- the schedule's periods ---------------------------------------------------------------------


def test_the_schedule_is_at_least_five_minutes_and_an_hour_by_default() -> None:
    assert period(None) == dt.timedelta(hours=1)
    assert period("5m", minimum=dt.timedelta(minutes=5)) == dt.timedelta(minutes=5)
    assert period("2d") == dt.timedelta(days=2)
    for wrong in ("4m", "0h"):
        with pytest.raises(ValueError, match=r"shorter|not a duration"):
            period(wrong, minimum=dt.timedelta(minutes=5))
    with pytest.raises(ValueError, match="not a duration"):
        period("5s")


class Fixed:
    """A lake whose latest snapshot is always the same, and passes."""

    snapshot = Snapshot(kind="snapshot", id=3, committed_at=dt.datetime(2026, 11, 8, tzinfo=dt.UTC))

    def latest(self) -> Snapshot:
        return self.snapshot

    def committed_at(self, snapshot: int) -> dt.datetime | None:
        return self.snapshot.committed_at if snapshot == self.snapshot.id else None

    def verify(self, snapshot: Snapshot, current: ExistingLakeAttachment) -> Snapshot:
        return snapshot


def schedule_of(every: str) -> SnapshotSchedule:
    attachment = ExistingLakeAttachment.model_construct()
    return SnapshotSchedule(
        "p",
        "billing",
        Fixed(),
        attachment=lambda: attachment,
        evidence=lambda _: None,
        schedule=every,
        freshness=None,
    )


def test_a_check_is_due_on_the_connections_schedule() -> None:
    schedule = schedule_of("10m")
    start = dt.datetime(2026, 11, 8, 1, tzinfo=dt.UTC)
    assert schedule.due(start)
    assert (schedule.state(start).color, schedule.state(start).served) == ("red", None)
    assert schedule.check(start).color == "green"
    assert not schedule.due(start + dt.timedelta(minutes=9))
    assert schedule.due(start + dt.timedelta(minutes=10))
    assert schedule.pin() == {
        "id": 3,
        "committed_at": Fixed.snapshot.committed_at.isoformat(),
        "project": "p",
    }
    with pytest.raises(ValueError, match="shorter"):
        schedule_of("4m")
