"""P1-05: loading `project.yaml` and `connections.yaml` (contract `project` v1)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from orvex.contracts.project.v1 import (
    ClickhouseConnection,
    CsvConnection,
    DuckdbConnection,
    ParquetConnection,
    PostgresConnection,
    SqliteConnection,
)
from orvex.server.project import ProjectLoadError, load_project, resolve_secret

PROJECT = """\
version: 1
name: Distribución
time_zone: America/La_Paz
"""

CONNECTIONS = """\
version: 1
connections:
  archive:
    type: parquet
    path: data/readings/*.parquet
  tariffs:
    type: csv
    path: data/tariffs.csv
  legacy:
    type: duckdb
    path: legacy.duckdb
    read_only: true
  field:
    type: sqlite
    path: /srv/field/app.sqlite
    read_only: true
  production:
    type: postgres
    host: db.example.invalid
    port: 5432
    database: billing
    user: readonly_bi
    password: ${env:TEST_SECRET_PASSWORD}
    read_only: true
    schema: public
  events:
    type: clickhouse
    host: ch.example.invalid
    port: 8443
    database: events
    user: reader
    password: ${env:TEST_SECRET_PASSWORD}
    read_only: true
"""

SECRET = "very-recognizable"


def project(tmp_path: Path, settings: str = PROJECT, connections: str | None = None) -> Path:
    (tmp_path / "project.yaml").write_text(settings, encoding="utf-8")
    if connections is not None:
        (tmp_path / "connections.yaml").write_text(connections, encoding="utf-8")
    return tmp_path


def failure(folder: Path) -> ProjectLoadError:
    with pytest.raises(ProjectLoadError) as raised:
        load_project(folder)
    return raised.value


def test_complete_project_loads_with_every_connection_type(tmp_path: Path) -> None:
    loaded = load_project(project(tmp_path, connections=CONNECTIONS))
    assert loaded.settings.name == "Distribución"
    assert loaded.settings.time_zone == "America/La_Paz"
    kinds = {name: type(connection) for name, connection in loaded.connections.items()}
    assert kinds == {
        "archive": ParquetConnection,
        "tariffs": CsvConnection,
        "legacy": DuckdbConnection,
        "field": SqliteConnection,
        "production": PostgresConnection,
        "events": ClickhouseConnection,
    }
    archive = loaded.connections["archive"]
    legacy = loaded.connections["legacy"]
    field = loaded.connections["field"]
    production = loaded.connections["production"]
    events = loaded.connections["events"]
    assert isinstance(archive, ParquetConnection)
    assert archive.path.root == str(tmp_path / "data/readings/*.parquet")
    assert isinstance(legacy, DuckdbConnection)
    assert legacy.path.root == str(tmp_path / "legacy.duckdb")
    assert isinstance(field, SqliteConnection)
    assert field.path.root == "/srv/field/app.sqlite"
    assert isinstance(production, PostgresConnection)
    assert production.schema_ == "public"
    assert production.password.root == "${env:TEST_SECRET_PASSWORD}"
    assert isinstance(events, ClickhouseConnection)
    assert events.secure is True


def test_defaults_are_applied(tmp_path: Path) -> None:
    loaded = load_project(project(tmp_path))
    assert loaded.settings.locale == "es"
    assert loaded.settings.retention is not None
    assert loaded.settings.retention.snapshots == "90d"


def test_connections_file_is_optional(tmp_path: Path) -> None:
    assert load_project(project(tmp_path)).connections == {}


def test_secret_values_never_appear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("TEST_SECRET_PASSWORD", SECRET)
    caplog.set_level(logging.DEBUG)
    loaded = load_project(project(tmp_path, connections=CONNECTIONS))
    production = loaded.connections["production"]
    assert isinstance(production, PostgresConnection)
    assert SECRET not in repr(loaded)
    assert SECRET not in str(loaded)
    assert "${env:TEST_SECRET_PASSWORD}" in repr(production)
    broken = CONNECTIONS.replace("port: 5432", "port: not-a-port")
    error = failure(project(tmp_path, connections=broken))
    assert SECRET not in str(error)
    assert SECRET not in error.error.model_dump_json()
    assert SECRET not in caplog.text
    assert resolve_secret(production.password) == SECRET


def test_literal_password_is_refused_without_echoing_it(tmp_path: Path) -> None:
    literal = CONNECTIONS.replace("${env:TEST_SECRET_PASSWORD}", SECRET, 1)
    error = failure(project(tmp_path, connections=literal))
    assert error.code == "PROJECT_SCHEMA_INVALID"
    assert error.error.details.file == "connections.yaml"
    assert error.error.details.path == "/connections/production/password"
    assert error.error.details.line == 23
    assert SECRET not in str(error)
    assert SECRET not in error.error.model_dump_json()


def test_resolve_secret_reads_at_use_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_SECRET_MISSING", raising=False)
    with pytest.raises(ProjectLoadError) as raised:
        resolve_secret("${env:TEST_SECRET_MISSING}")
    assert raised.value.code == "PROJECT_SECRET_MISSING"
    assert raised.value.error.details.variable == "TEST_SECRET_MISSING"
    monkeypatch.setenv("TEST_SECRET_MISSING", "later")
    assert resolve_secret("${env:TEST_SECRET_MISSING}") == "later"


def test_missing_project_file(tmp_path: Path) -> None:
    error = failure(tmp_path)
    assert error.code == "PROJECT_NOT_FOUND"
    assert error.error.details.file == "project.yaml"


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("version: 1\nname: [unclosed\ntime_zone: UTC\n", 3),
        ("version: 1\nname: A\nname: B\ntime_zone: UTC\n", 3),
        ("version: 1\n---\nversion: 1\n", 2),
    ],
    ids=["syntax", "duplicate-key", "two-documents"],
)
def test_invalid_yaml_reports_the_line(tmp_path: Path, text: str, line: int) -> None:
    error = failure(project(tmp_path, text))
    assert error.code == "PROJECT_YAML_INVALID"
    assert error.error.details.file == "project.yaml"
    assert error.error.details.line == line


@pytest.mark.parametrize(
    ("settings", "path"),
    [
        (PROJECT + "colour: blue\n", "/colour"),
        ("version: 1\nname: Ventas\n", ""),
        (PROJECT.replace("America/La_Paz", "America/Atlantis"), "/time_zone"),
        (PROJECT.replace("Distribución", "123"), ""),
        (PROJECT + "locale: fr\n", "/locale"),
        (PROJECT + "locale: null\n", "/locale"),
        (PROJECT + "retention:\n  snapshots: 0d\n", "/retention/snapshots"),
        ("- a list\n", ""),
    ],
    ids=[
        "unknown-key",
        "missing-time-zone",
        "unknown-time-zone",
        "wrong-type",
        "unknown-locale",
        "null-locale",
        "zero-retention",
        "not-a-mapping",
    ],
)
def test_schema_violations_in_project_file(tmp_path: Path, settings: str, path: str) -> None:
    error = failure(project(tmp_path, settings))
    assert error.code == "PROJECT_SCHEMA_INVALID"
    assert error.error.details.file == "project.yaml"
    assert (error.error.details.path or "").startswith(path)


def test_wrong_type_names_the_field(tmp_path: Path) -> None:
    error = failure(project(tmp_path, PROJECT.replace("Distribución", "123")))
    assert error.error.details.path == "/name"
    assert error.error.details.line == 2


@pytest.mark.parametrize(
    ("connections", "path"),
    [
        (
            "version: 1\nconnections:\n  lake:\n    type: parquet\n    path: a.parquet\n",
            "/connections/lake",
        ),
        (
            "version: 1\nconnections:\n  a:\n    type: csv\n    path: a.csv\n"
            "  a:\n    type: csv\n    path: b.csv\n",
            "/connections/a",
        ),
        (
            "version: 1\nconnections:\n  Sales:\n    type: csv\n    path: a.csv\n",
            "/connections/Sales",
        ),
        ("version: 1\nconnections:\n  a:\n    type: mysql\n    path: a\n", "/connections/a/type"),
        (
            "version: 1\nconnections:\n  a:\n    type: sqlite\n    path: a.sqlite\n",
            "/connections/a/read_only",
        ),
        (
            "version: 1\nconnections:\n  a:\n    type: csv\n    path: a.csv\n    extra: 1\n",
            "/connections/a/extra",
        ),
    ],
    ids=[
        "named-lake",
        "duplicate-name",
        "bad-name",
        "unknown-type",
        "missing-read-only",
        "unknown-key",
    ],
)
def test_schema_violations_in_connections_file(tmp_path: Path, connections: str, path: str) -> None:
    error = failure(project(tmp_path, connections=connections))
    assert error.code == "PROJECT_SCHEMA_INVALID"
    assert error.error.details.file == "connections.yaml"
    assert error.error.details.path == path
    assert error.error.details.line is not None


@pytest.mark.parametrize("kind", ["duckdb", "sqlite"])
def test_read_only_false_is_refused(tmp_path: Path, kind: str) -> None:
    text = (
        f"version: 1\nconnections:\n  legacy:\n    type: {kind}\n    path: a\n"
        "    read_only: false\n"
    )
    error = failure(project(tmp_path, connections=text))
    assert error.code == "SOURCE_NOT_READ_ONLY"
    assert error.error.details.connection == "legacy"
    assert error.error.details.path == "/connections/legacy/read_only"
    assert error.error.details.line == 6


def test_postgres_read_only_false_is_refused(tmp_path: Path) -> None:
    text = CONNECTIONS.replace("    read_only: true\n    schema: public", "    read_only: false")
    error = failure(project(tmp_path, connections=text))
    assert error.code == "SOURCE_NOT_READ_ONLY"
    assert error.error.details.connection == "production"


@pytest.mark.parametrize("file", ["project.yaml", "connections.yaml"])
def test_unknown_version(tmp_path: Path, file: str) -> None:
    folder = project(tmp_path, connections="version: 1\nconnections: {}\n")
    target = folder / file
    target.write_text(target.read_text(encoding="utf-8").replace("version: 1", "version: 2"))
    error = failure(folder)
    assert error.code == "PROJECT_VERSION_UNSUPPORTED"
    assert error.error.details.file == file
    assert error.error.details.line == 1


@pytest.mark.parametrize("version", ["", "version: abc\n", "version: ~\n", "version: true\n"])
def test_connections_version_is_checked(tmp_path: Path, version: str) -> None:
    error = failure(project(tmp_path, connections=f"{version}connections: {{}}\n"))
    assert error.code == "PROJECT_SCHEMA_INVALID"
    assert error.error.details.file == "connections.yaml"


@pytest.mark.parametrize("version", ["true", "yes", "1.0"])
def test_project_version_must_be_the_integer_one(tmp_path: Path, version: str) -> None:
    error = failure(project(tmp_path, PROJECT.replace("version: 1", f"version: {version}")))
    assert error.code == "PROJECT_SCHEMA_INVALID"
    assert error.error.details.path == "/version"


@pytest.mark.parametrize("zone", ["localtime", "posixrules", "../etc/passwd"])
def test_time_zone_must_be_an_iana_name(tmp_path: Path, zone: str) -> None:
    error = failure(project(tmp_path, PROJECT.replace("America/La_Paz", zone)))
    assert error.code == "PROJECT_SCHEMA_INVALID"
    assert error.error.details.path == "/time_zone"


def chain(error: BaseException) -> str:
    """Every exception reachable from `error` through its cause and context, as text."""
    texts = []
    seen: BaseException | None = error
    while seen is not None:
        texts.append(repr(seen) + str(seen))
        seen = seen.__cause__ or seen.__context__
    return "\n".join(texts)


def test_literal_password_is_not_kept_in_the_exception_chain(tmp_path: Path) -> None:
    literal = CONNECTIONS.replace("${env:TEST_SECRET_PASSWORD}", SECRET, 1)
    error = failure(project(tmp_path, connections=literal))
    assert SECRET not in chain(error)
    broken_yaml = f"version: 1\nconnections:\n  a: {{password: {SECRET}\n"
    error = failure(project(tmp_path, connections=broken_yaml))
    assert error.code == "PROJECT_YAML_INVALID"
    assert SECRET not in chain(error)
