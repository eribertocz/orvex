"""P1-05: loading `project.yaml` and `connections.yaml` (contract `project` v1)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from orvex.contracts.project.v1 import (
    CsvConnection,
    DuckdbConnection,
    DucklakeConnection,
    DucklakeSqliteCatalog,
    ParquetConnection,
    PostgresConnection,
    SqliteConnection,
)
from orvex.server.cli import main
from orvex.server.project import ProjectLoadError, load_project, resolve_secret

from tests.support.bounds import linear

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
    ssl_mode: verify-full
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
    }
    archive = loaded.connections["archive"]
    legacy = loaded.connections["legacy"]
    field = loaded.connections["field"]
    production = loaded.connections["production"]
    assert isinstance(archive, ParquetConnection)
    assert archive.path.root == str(tmp_path / "data/readings/*.parquet")
    assert isinstance(legacy, DuckdbConnection)
    assert legacy.path.root == str(tmp_path / "legacy.duckdb")
    assert isinstance(field, SqliteConnection)
    assert field.path.root == "/srv/field/app.sqlite"
    assert isinstance(production, PostgresConnection)
    assert production.schema_ == "public"
    assert production.password.root == "${env:TEST_SECRET_PASSWORD}"


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


@pytest.mark.parametrize(
    "text",
    [
        "version: 1\nconnections: &c {a: *c}\n",
        "version: 1\nconnections:\n  a: &x {type: csv, path: a.csv}\n  b: *x\n",
        "version: 1\nx: &a [1, 1]\ny: &b [*a, *a]\nz: &c [*b, *b]\nconnections: {}\n",
    ],
    ids=["self-reference", "alias", "nested-aliases"],
)
def test_anchors_and_aliases_are_refused(tmp_path: Path, text: str) -> None:
    error = failure(project(tmp_path, connections=text))
    assert error.code == "PROJECT_YAML_INVALID"
    assert error.error.details.line is not None


def test_a_file_that_is_not_utf8_is_refused_without_its_content(tmp_path: Path) -> None:
    folder = project(tmp_path)
    (folder / "connections.yaml").write_bytes(
        f"version: 1\n# \xe9\nconnections: {{}}\npassword: {SECRET}\n".encode("latin-1")
    )
    error = failure(folder)
    assert error.code == "PROJECT_YAML_INVALID"
    assert error.error.details.file == "connections.yaml"
    assert SECRET not in chain(error)
    assert error.__context__ is None


def test_factory_is_not_a_time_zone(tmp_path: Path) -> None:
    error = failure(project(tmp_path, PROJECT.replace("America/La_Paz", "Factory")))
    assert error.code == "PROJECT_SCHEMA_INVALID"


def test_a_clickhouse_connection_is_never_a_source_of_a_model(tmp_path: Path) -> None:
    # ADR-0028: ClickHouse is queried in place; extraction into the lake is retired.
    text = (
        "version: 1\nconnections:\n  events:\n    type: clickhouse\n"
        "    host: ch.example.invalid\n    port: 8443\n    database: events\n"
        "    user: reader\n    password: ${env:TEST_SECRET_PASSWORD}\n    read_only: true\n"
    )
    loaded = load_project(project(tmp_path, connections=text))
    assert "events" not in loaded.sources
    assert loaded.direct == frozenset({"events"})


@pytest.mark.parametrize("depth", [40, 500, 5000])
@pytest.mark.parametrize("file", ["project.yaml", "connections.yaml"])
def test_deeply_nested_files_are_refused(tmp_path: Path, file: str, depth: int) -> None:
    nested = "[" * depth + "]" * depth
    folder = project(tmp_path, connections="version: 1\nconnections: {}\n")
    if file == "project.yaml":
        (folder / file).write_text(PROJECT + f"retention: {nested}\n", encoding="utf-8")
    else:
        (folder / file).write_text(f"version: 1\nconnections: {nested}\n", encoding="utf-8")
    error = failure(folder)
    assert error.code == "PROJECT_YAML_INVALID"
    assert error.error.details.file == file
    assert error.__context__ is None


def test_oversized_file_is_refused(tmp_path: Path) -> None:
    folder = project(tmp_path, PROJECT + "# " + "x" * (1024 * 1024) + "\n")
    error = failure(folder)
    assert error.code == "PROJECT_YAML_INVALID"


def test_unreadable_connections_file(tmp_path: Path) -> None:
    folder = project(tmp_path)
    (folder / "connections.yaml").mkdir()
    error = failure(folder)
    assert error.code == "PROJECT_NOT_FOUND"
    assert error.error.details.file == "connections.yaml"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes and /dev/zero")
def test_connections_file_must_be_a_regular_file(tmp_path: Path) -> None:
    folder = project(tmp_path)
    connections = folder / "connections.yaml"
    connections.symlink_to("/dev/zero")
    assert failure(folder).code == "PROJECT_NOT_FOUND"
    connections.unlink()
    os.mkfifo(connections)
    error = failure(folder)  # a pipe would block forever if it were opened
    assert error.code == "PROJECT_NOT_FOUND"
    assert error.error.details.file == "connections.yaml"


def test_error_paths_escape_key_names(tmp_path: Path) -> None:
    error = failure(project(tmp_path, PROJECT + "a/b~c: 1\n"))
    assert error.error.details.path == "/a~1b~0c"
    assert error.error.details.line == 4


@pytest.mark.parametrize(
    ("old", "new", "path"),
    [
        ("    schema: public\n", "    schema: ~\n", "/connections/production/schema"),
        (
            "    ssl_mode: verify-full\n",
            "    ssl_mode:\n",
            "/connections/production/ssl_mode",
        ),
    ],
    ids=["null-schema", "empty-ssl-mode"],
)
def test_null_connection_fields_are_refused(tmp_path: Path, old: str, new: str, path: str) -> None:
    error = failure(project(tmp_path, connections=CONNECTIONS.replace(old, new)))
    assert error.code == "PROJECT_SCHEMA_INVALID"
    assert error.error.details.path == path


@pytest.mark.parametrize("file", ["project.yaml", "connections.yaml"])
@pytest.mark.parametrize("value", ["1" * 4400, "2024-02-30"], ids=["huge-integer", "bad-date"])
def test_values_python_cannot_build_are_refused(tmp_path: Path, file: str, value: str) -> None:
    folder = project(tmp_path, connections="version: 1\nconnections: {}\n")
    if file == "project.yaml":
        (folder / file).write_text(PROJECT + f"extra: {value}\n", encoding="utf-8")
    else:
        (folder / file).write_text(f"version: 1\nconnections: {{}}\nx: {value}\n")
    error = failure(folder)
    assert error.code in {"PROJECT_YAML_INVALID", "PROJECT_SCHEMA_INVALID"}
    assert error.error.details.file == file
    assert error.__context__ is None


@pytest.mark.parametrize(
    "connections",
    [
        "version: 1\nconnections:\n  a: {type: csv, path: a.csv}\n"
        "  <<: {a: {type: csv, path: b}}\n",
        "version: 1\nconnections:\n  legacy:\n    type: duckdb\n    path: a\n    read_only: true\n"
        "    <<: {read_only: false}\n",
        "version: 1\n<<: {connections: {}}\n",
    ],
    ids=["duplicate-connection", "duplicate-key", "top-level"],
)
def test_merge_keys_are_refused(tmp_path: Path, connections: str) -> None:
    error = failure(project(tmp_path, connections=connections))
    assert error.code == "PROJECT_YAML_INVALID"
    assert error.error.details.line is not None


# --- #79: lines and keys as written, oversized files, empty connections.yaml -------------------

BASE = "version: 1\nname: Test\ntime_zone: UTC\n"  # three lines: the case under test is line 4


def test_an_unbuildable_integer_is_named_with_its_line(tmp_path: Path) -> None:
    digits = "9" * 5000
    error = failure(project(tmp_path, BASE + f"x: {digits}\n"))
    assert error.code == "PROJECT_YAML_INVALID"
    details = error.error.details
    assert (details.file, details.line, details.path) == ("project.yaml", 4, "/x")
    assert "an integer with more than 4300 digits" in error.error.message
    assert digits[:20] not in error.error.message


def test_an_impossible_date_is_named_with_its_line(tmp_path: Path) -> None:
    connections = (
        "version: 1\nconnections:\n  f:\n    type: parquet\n    path: a.parquet\n"
        "    since: 2024-02-30\n"
    )
    error = failure(project(tmp_path, connections=connections))
    assert error.code == "PROJECT_YAML_INVALID"
    details = error.error.details
    assert (details.file, details.line, details.path) == (
        "connections.yaml",
        6,
        "/connections/f/since",
    )
    assert "not a real date or time" in error.error.message
    assert "2024-02-30" not in error.error.message


def test_an_oversized_project_file_is_refused_in_linear_time(tmp_path: Path) -> None:
    def refuse(keys: int) -> None:
        settings = BASE + "".join(f"k{index}: 1\n" for index in range(keys))
        folder = tmp_path / f"p{keys}"
        folder.mkdir(exist_ok=True)
        error = failure(project(folder, settings))
        assert error.code == "PROJECT_SCHEMA_INVALID"
        assert "Unknown key 'k0'" in error.error.message
        assert (error.error.details.line, error.error.details.path) == (4, "/k0")

    linear(refuse, 15000)  # the large run is the 60,000 keys of the regression


@pytest.mark.parametrize("key", ["null", "1", "true"])
def test_a_key_that_is_not_text_is_named_as_written(tmp_path: Path, key: str) -> None:
    error = failure(project(tmp_path, BASE + f"{key}: x\n"))
    assert error.code == "PROJECT_SCHEMA_INVALID"
    assert (error.error.details.line, error.error.details.path) == (4, f"/{key}")


@pytest.mark.parametrize(
    "connections",
    ["", "# only a comment\n", "  \n\n", "null\n", "~\n"],
    ids=["empty", "comments", "white-space", "null", "tilde"],
)
def test_an_empty_connections_file_means_no_connections(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], connections: str
) -> None:
    folder = project(tmp_path, connections=connections)
    assert load_project(folder).connections == {}
    assert main(["validate", str(folder)]) == 0, capsys.readouterr().out


@pytest.mark.parametrize("connections", ["[a]\n", "x\n"], ids=["list", "scalar"])
def test_a_connections_file_that_is_not_a_mapping_is_refused(
    tmp_path: Path, connections: str
) -> None:
    error = failure(project(tmp_path, connections=connections))
    assert error.code == "PROJECT_SCHEMA_INVALID"
    assert "The file must be a mapping" in error.error.message


# --- ClickHouse Direct Query connections (P2-31 PR 1, #225) ----------------------------------

CLICKHOUSE = """version: 1
connections:
  warehouse:
    type: clickhouse
    host: {host}
    port: 8443
    database: sales
    user: bi_reader
    password: ${{env:WAREHOUSE_PASSWORD}}
    read_only: {read_only}
{extra}"""


def clickhouse(host: str = "ch.example.com", read_only: str = "true", extra: str = "") -> str:
    return CLICKHOUSE.format(host=host, read_only=read_only, extra=extra)


def test_a_clickhouse_connection_loads_with_safe_defaults_and_is_direct_query_only(
    tmp_path: Path,
) -> None:
    loaded = load_project(project(tmp_path, connections=clickhouse()))
    connection = loaded.connections["warehouse"]
    assert connection.type == "clickhouse"
    assert (connection.tls, connection.tls_root_cert, connection.evidence_schedule) == (
        True,
        "system",
        "1d",
    )
    assert loaded.direct == frozenset({"warehouse"})
    assert dict(loaded.sources) == {}


def test_a_clickhouse_connection_on_another_machine_without_tls_is_refused(
    tmp_path: Path,
) -> None:
    error = failure(project(tmp_path, connections=clickhouse(extra="    tls: false\n")))
    assert error.code == "SOURCE_TLS_REQUIRED"
    assert error.error.details.path == "/connections/warehouse/tls"


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_a_local_clickhouse_may_go_without_tls(tmp_path: Path, host: str) -> None:
    text = clickhouse(host=f"'{host}'", extra="    tls: false\n")
    assert load_project(project(tmp_path, connections=text)).direct == frozenset({"warehouse"})


def test_a_clickhouse_connection_must_be_read_only(tmp_path: Path) -> None:
    error = failure(project(tmp_path, connections=clickhouse(read_only="false")))
    assert error.code == "SOURCE_NOT_READ_ONLY"


@pytest.mark.parametrize(
    "extra",
    [
        "    evidence_schedule: 30m\n",
        "    evidence_schedule: 0h\n",
        "    tls_root_cert: /etc/ca.pem\n",
        "    host_list: a,b\n",
    ],
    ids=["below-an-hour", "zero", "literal-ca-path", "unknown-field"],
)
def test_a_clickhouse_connection_outside_its_schema_is_refused(tmp_path: Path, extra: str) -> None:
    assert failure(project(tmp_path, connections=clickhouse(extra=extra))).code == (
        "PROJECT_SCHEMA_INVALID"
    )


def test_a_clickhouse_password_must_be_a_reference(tmp_path: Path) -> None:
    text = clickhouse().replace("${env:WAREHOUSE_PASSWORD}", "s3cret-value")
    error = failure(project(tmp_path, connections=text))
    assert error.code == "PROJECT_SCHEMA_INVALID"
    assert "s3cret-value" not in str(error)


def test_check_names_a_model_that_reads_a_clickhouse_connection(tmp_path: Path) -> None:
    from orvex.server import commands

    folder = project(tmp_path, connections=clickhouse())
    (folder / "models" / "s").mkdir(parents=True)
    (folder / "models" / "s" / "m.sql").write_text("SELECT * FROM warehouse.orders\n", "utf-8")
    checked = commands.check_project(folder)
    assert [(p.code, "Direct Query" in p.message) for p in checked.problems] == [
        ("MODEL_REFERENCE_UNKNOWN", True)
    ]


# --- Existing DuckLake connections (P2-30 PR 1, #399) ----------------------------------------

DUCKLAKE_POSTGRES = """version: 1
connections:
  billing:
    type: ducklake
    catalog:
      type: postgres
      host: {host}
      port: 5432
      database: lakes
      user: billing_reader
      password: ${{env:BILLING_CATALOG_PASSWORD}}
      schema: billing
{catalog_extra}    data_path: {data_path}
    read_only: {read_only}
{extra}"""


def ducklake(
    host: str = "catalog.example.com",
    catalog_extra: str = "      ssl_mode: verify-full\n",
    data_path: str = "s3://lakes/billing/",
    read_only: str = "true",
    extra: str = "    s3:\n      region: us-east-1\n"
    "      key_id: ${env:BILLING_KEY}\n      secret: ${env:BILLING_SECRET}\n",
) -> str:
    return DUCKLAKE_POSTGRES.format(
        host=host,
        catalog_extra=catalog_extra,
        data_path=data_path,
        read_only=read_only,
        extra=extra,
    )


def test_a_ducklake_connection_loads_with_safe_defaults_and_is_direct_query_only(
    tmp_path: Path,
) -> None:
    loaded = load_project(project(tmp_path, connections=ducklake()))
    connection = loaded.connections["billing"]
    assert connection.type == "ducklake"
    assert (connection.schedule, connection.freshness) == ("1h", None)
    assert loaded.direct == frozenset({"billing"})
    assert dict(loaded.sources) == {}


def test_a_ducklake_sqlite_catalog_and_local_data_are_resolved_against_the_project(
    tmp_path: Path,
) -> None:
    text = (
        "version: 1\nconnections:\n  billing:\n    type: ducklake\n"
        "    catalog:\n      type: sqlite\n      path: lakes/billing.sqlite\n"
        "    data_path: lakes/billing/\n    read_only: true\n    schedule: 5m\n"
        "    freshness: 2h\n"
    )
    folder = project(tmp_path, connections=text)
    connection = load_project(folder).connections["billing"]
    assert isinstance(connection, DucklakeConnection)
    assert isinstance(connection.catalog.root, DucklakeSqliteCatalog)
    assert connection.catalog.root.path.root == str(folder / "lakes" / "billing.sqlite")
    assert connection.data_path == str(folder / "lakes" / "billing") + "/"
    assert (connection.schedule, connection.freshness) == ("5m", "2h")


@pytest.mark.parametrize("kind", ["duckdb", "mysql", "quack"])
def test_a_ducklake_catalog_other_than_postgres_or_sqlite_is_a_schema_error(
    tmp_path: Path, kind: str
) -> None:
    text = (
        "version: 1\nconnections:\n  billing:\n    type: ducklake\n"
        f"    catalog:\n      type: {kind}\n      path: lakes/billing.{kind}\n"
        "    data_path: lakes/billing/\n    read_only: true\n"
    )
    assert failure(project(tmp_path, connections=text)).code == "PROJECT_SCHEMA_INVALID"


def test_a_ducklake_connection_must_be_read_only(tmp_path: Path) -> None:
    error = failure(project(tmp_path, connections=ducklake(read_only="false")))
    assert error.code == "SOURCE_NOT_READ_ONLY"


@pytest.mark.parametrize("catalog_extra", ["", "      ssl_mode: disable\n"])
def test_a_remote_ducklake_catalog_without_verify_full_is_refused(
    tmp_path: Path, catalog_extra: str
) -> None:
    error = failure(project(tmp_path, connections=ducklake(catalog_extra=catalog_extra)))
    assert error.code == "SOURCE_TLS_REQUIRED"
    assert error.error.details.path == "/connections/billing/catalog/ssl_mode"


def test_a_local_ducklake_catalog_may_go_without_tls(tmp_path: Path) -> None:
    text = ducklake(host="localhost", catalog_extra="      ssl_mode: disable\n")
    assert load_project(project(tmp_path, connections=text)).direct == frozenset({"billing"})


def test_ducklake_object_storage_over_plain_http_to_another_machine_is_refused(
    tmp_path: Path,
) -> None:
    extra = (
        "    s3:\n      region: us-east-1\n      endpoint: minio.example.com:9000\n"
        "      use_ssl: false\n      key_id: ${env:BILLING_KEY}\n"
        "      secret: ${env:BILLING_SECRET}\n"
    )
    error = failure(project(tmp_path, connections=ducklake(extra=extra)))
    assert error.code == "SOURCE_TLS_REQUIRED"
    assert error.error.details.path == "/connections/billing/s3/use_ssl"
    local = extra.replace("minio.example.com", "localhost")
    assert load_project(project(tmp_path, connections=ducklake(extra=local))).direct


@pytest.mark.parametrize(
    ("data_path", "extra"),
    [
        ("s3://lakes/billing/", ""),
        (
            "lakes/billing/",
            "    s3:\n      region: us-east-1\n      key_id: ${env:K}\n      secret: ${env:S}\n",
        ),
        (
            "gs://lakes/billing/",
            "    s3:\n      region: us-east-1\n      key_id: ${env:K}\n      secret: ${env:S}\n",
        ),
        ("https://lakes.example.com/billing/", ""),
        (
            "s3://lakes/../billing/",
            "    s3:\n      region: us-east-1\n      key_id: ${env:K}\n      secret: ${env:S}\n",
        ),
    ],
    ids=["s3-without-settings", "settings-without-s3", "other-kind", "web-url", "dot-dot"],
)
def test_a_ducklake_data_path_must_match_its_storage_settings(
    tmp_path: Path, data_path: str, extra: str
) -> None:
    text = ducklake(data_path=data_path, extra=extra)
    assert failure(project(tmp_path, connections=text)).code == "PROJECT_SCHEMA_INVALID"


@pytest.mark.parametrize(
    "extra",
    ["    schedule: 4m\n", "    schedule: 0h\n", "    freshness: 1w\n", "    snapshot: 3\n"],
    ids=["schedule-below-5m", "schedule-zero", "freshness-unit", "unknown-field"],
)
def test_a_ducklake_connection_outside_its_schema_is_refused(tmp_path: Path, extra: str) -> None:
    text = ducklake() + extra
    assert failure(project(tmp_path, connections=text)).code == "PROJECT_SCHEMA_INVALID"


def test_a_ducklake_catalog_password_must_be_a_reference(tmp_path: Path) -> None:
    text = ducklake().replace("${env:BILLING_CATALOG_PASSWORD}", "s3cret-value")
    error = failure(project(tmp_path, connections=text))
    assert error.code == "PROJECT_SCHEMA_INVALID"
    assert "s3cret-value" not in str(error)


def test_a_ducklake_sqlite_catalog_cannot_be_a_url(tmp_path: Path) -> None:
    text = (
        "version: 1\nconnections:\n  billing:\n    type: ducklake\n"
        "    catalog:\n      type: sqlite\n      path: s3://lakes/billing.sqlite\n"
        "    data_path: /srv/billing/\n    read_only: true\n"
    )
    assert failure(project(tmp_path, connections=text)).code == "LAKE_LOCATION_INVALID"


@pytest.mark.parametrize("kind", ["nfs4", "fuse.sshfs", None])
def test_a_ducklake_sqlite_catalog_must_be_on_a_local_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str | None
) -> None:
    from orvex.server import project as project_module

    monkeypatch.setattr(project_module, "file_system", lambda path: kind)
    text = (
        "version: 1\nconnections:\n  billing:\n    type: ducklake\n"
        "    catalog:\n      type: sqlite\n      path: /mnt/shared/billing.sqlite\n"
        "    data_path: /mnt/shared/billing/\n    read_only: true\n"
    )
    error = failure(project(tmp_path, connections=text))
    assert error.code == "LAKE_LOCATION_INVALID"
    assert error.error.details.path == "/connections/billing/catalog/path"
