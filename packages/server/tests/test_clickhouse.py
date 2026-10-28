"""P1-17 (#65): extraction models validated on a real ClickHouse, read-only, reading no row.

Needs a ClickHouse server: `TRANSFORM_TEST_CLICKHOUSE` (its HTTP URL) with an administrator in
`TRANSFORM_TEST_CLICKHOUSE_USER` / `_PASSWORD` (the CI service). The tests create a database, a
synthetic `events` table and a user with SELECT only, and drop them afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import clickhouse_connect
import pytest
from clickhouse_connect.driver.client import Client
from orvex.server.commands import check_project

SERVER = os.environ.get("TRANSFORM_TEST_CLICKHOUSE")
pytestmark = pytest.mark.skipif(SERVER is None, reason="no ClickHouse server for tests")

READER_PASSWORD = "reader-" + uuid.uuid4().hex
VARIABLE = "TEST_CLICKHOUSE_READER_PASSWORD"

# One column per mapped type, plain and Nullable; the expected DuckDB type of each.
TYPES = {
    "c_int8": ("Int8", "TINYINT"),
    "c_int16": ("Int16", "SMALLINT"),
    "c_int32": ("Int32", "INTEGER"),
    "c_int64": ("Int64", "BIGINT"),
    "c_int128": ("Int128", "HUGEINT"),
    "c_uint8": ("UInt8", "UTINYINT"),
    "c_uint16": ("UInt16", "USMALLINT"),
    "c_uint32": ("UInt32", "UINTEGER"),
    "c_uint64": ("UInt64", "UBIGINT"),
    "c_uint128": ("UInt128", "UHUGEINT"),
    "c_decimal": ("Decimal(38, 10)", "DECIMAL(38,10)"),
    "c_float32": ("Float32", "FLOAT"),
    "c_float64": ("Float64", "DOUBLE"),
    "c_string": ("String", "VARCHAR"),
    "c_fixed": ("FixedString(8)", "VARCHAR"),
    "c_low": ("LowCardinality(String)", "VARCHAR"),
    "c_enum8": ("Enum8('a' = 1, 'b' = 2)", "VARCHAR"),
    "c_enum16": ("Enum16('x' = 1000)", "VARCHAR"),
    "c_bool": ("Bool", "BOOLEAN"),
    "c_uuid": ("UUID", "UUID"),
    "c_date": ("Date", "DATE"),
    "c_date32": ("Date32", "DATE"),
    "c_datetime": ("DateTime('America/La_Paz')", "TIMESTAMP WITH TIME ZONE"),
    "c_datetime64": ("DateTime64(6, 'UTC')", "TIMESTAMP WITH TIME ZONE"),
    "c_server_zone": ("DateTime64(3)", "TIMESTAMP WITH TIME ZONE"),
}
UNSUPPORTED = {
    "u_int256": "Int256",
    "u_array": "Array(Int32)",
    "u_decimal": "Decimal(76, 10)",
    "u_nanos": "DateTime64(9)",
}


def admin() -> Client:
    assert SERVER is not None
    url = urlsplit(SERVER)
    return clickhouse_connect.get_client(
        host=url.hostname,
        port=url.port,
        username=os.environ.get("TRANSFORM_TEST_CLICKHOUSE_USER", "default"),
        password=os.environ.get("TRANSFORM_TEST_CLICKHOUSE_PASSWORD", ""),
        secure=url.scheme == "https",
    )


@pytest.fixture(scope="module")
def source() -> Iterator[dict[str, str]]:
    """A database with `events` and `types`, and a user that may only SELECT from it."""
    suffix = uuid.uuid4().hex[:12]
    database, reader = f"test_{suffix}", f"reader_{suffix}"
    client = admin()
    columns = ", ".join(
        [f"{name} {kind}" for name, (kind, _) in TYPES.items()]
        + [f"n_{name} Nullable({kind})" for name, (kind, _) in TYPES.items() if "Low" not in kind]
        + [f"{name} {kind}" for name, kind in UNSUPPORTED.items()]
    )
    statements = [
        f"CREATE DATABASE {database}",
        f"CREATE TABLE {database}.events (event_id UInt64, meter_id String, "
        "event_time DateTime64(6, 'UTC'), value Float64) ENGINE = MergeTree ORDER BY event_id",
        f"INSERT INTO {database}.events SELECT number, concat('M', toString(number % 7)), "
        "toDateTime64('2024-01-01 00:00:00', 6, 'UTC') + number, number / 10 FROM numbers(1000)",
        f"CREATE TABLE {database}.types ({columns}) ENGINE = MergeTree ORDER BY tuple()",
        f"CREATE USER {reader} IDENTIFIED WITH sha256_password BY '{READER_PASSWORD}'",
        f"GRANT SELECT ON {database}.* TO {reader}",
    ]
    for statement in statements:
        client.command(statement)
    try:
        yield {"database": database, "reader": reader}
    finally:
        client.command(f"DROP USER IF EXISTS {reader}")
        client.command(f"DROP DATABASE IF EXISTS {database}")
        client.close()


def project(
    folder: Path,
    source: dict[str, str],
    models: dict[str, str],
    *,
    read_only: str = "true",
) -> Path:
    assert SERVER is not None
    url = urlsplit(SERVER)
    files = {
        "project.yaml": "version: 1\nname: Test\ntime_zone: America/La_Paz\n",
        "connections.yaml": (
            "version: 1\nconnections:\n  analytics_ch:\n    type: clickhouse\n"
            f"    host: {url.hostname}\n    port: {url.port}\n"
            f"    database: {source['database']}\n    user: {source['reader']}\n"
            f"    password: ${{env:{VARIABLE}}}\n    secure: {str(url.scheme == 'https').lower()}\n"
            f"    read_only: {read_only}\n"
        ),
        **models,
    }
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


EXTRACTION = (
    "version: 1\nconnection: analytics_ch\nmaterialized: incremental\nincremental:\n"
    "  strategy: merge\n  unique_key: [event_id]\n  time_column: event_time\n  lookback: 10m\n"
    "  filter: manual\n"
)
EXAMPLE = (
    "SELECT event_id, meter_id, event_time, toDecimal64(value, 4) AS value\nFROM events\n"
    "WHERE event_time > {since:DateTime64(6, 'UTC')}\n"
)
TABLE = "version: 1\nconnection: analytics_ch\n"


@pytest.fixture
def password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VARIABLE, READER_PASSWORD)


def problems(folder: Path) -> list[str]:
    return [str(item) for item in check_project(folder).problems]


@pytest.mark.usefixtures("password")
def test_the_example_validates_without_reading_rows(tmp_path: Path, source: dict[str, str]) -> None:
    marker = "e_" + uuid.uuid4().hex  # a table alias: it reaches query_log verbatim
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": EXAMPLE.replace("FROM events", f"FROM events AS {marker}"),
            "models/raw/ch_events.yaml": EXTRACTION,
            # A downstream DuckDB model binds against the mapped columns.
            "models/marts/by_meter.sql": (
                "SELECT meter_id, sum(value) AS value, max(event_time) AS last_event "
                "FROM raw.ch_events GROUP BY ALL"
            ),
            "models/marts/by_meter.yaml": ("version: 1\ntests:\n  - not_null: [meter_id]\n"),
        },
    )
    checked = check_project(folder)
    assert [str(item) for item in checked.problems] == []
    assert checked.columns["raw.ch_events"] == [
        ("event_id", "UBIGINT"),
        ("meter_id", "VARCHAR"),
        ("event_time", "TIMESTAMP WITH TIME ZONE"),
        ("value", "DECIMAL(18,4)"),
    ]
    assert checked.columns["marts.by_meter"] == [
        ("meter_id", "VARCHAR"),
        ("value", "DECIMAL(38,4)"),
        ("last_event", "TIMESTAMP WITH TIME ZONE"),
    ]
    client = admin()
    try:
        client.command("SYSTEM FLUSH LOGS")
        rows = client.query(
            "SELECT query_kind, Settings['readonly'] FROM system.query_log "
            "WHERE type = 'QueryFinish' AND user = {reader:String} "
            "AND position(query, {marker:String}) > 0",
            parameters={"reader": source["reader"], "marker": marker},
        ).result_rows
        reads = client.query(
            "SELECT count() FROM system.query_log WHERE user = {reader:String} "
            "AND query_kind = 'Select' AND has(tables, {events:String})",
            parameters={"reader": source["reader"], "events": f"{source['database']}.events"},
        ).result_rows
    finally:
        client.close()
    # The engine's queries on the model: EXPLAIN and DESCRIBE only (nothing executed), read-only.
    assert sorted(str(row[0]) for row in rows) == ["Describe", "Explain"]
    assert all(str(row[1]) == "1" for row in rows)
    assert reads == [(0,)]  # no SELECT ever read the source table


@pytest.mark.usefixtures("password")
def test_every_mapped_type(tmp_path: Path, source: dict[str, str]) -> None:
    plain = ", ".join(TYPES)
    nullable = ", ".join(f"n_{name}" for name, (kind, _) in TYPES.items() if "Low" not in kind)
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/types.sql": f"SELECT {plain}, {nullable} FROM types",
            "models/raw/types.yaml": TABLE,
        },
    )
    checked = check_project(folder)
    assert [str(item) for item in checked.problems] == []
    expected = [(name, duckdb) for name, (_, duckdb) in TYPES.items()]
    expected += [(f"n_{name}", d) for name, (k, d) in TYPES.items() if "Low" not in k]
    assert checked.columns["raw.types"] == expected


@pytest.mark.usefixtures("password")
def test_unsupported_types_are_named(tmp_path: Path, source: dict[str, str]) -> None:
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/types.sql": f"SELECT c_uint64, {', '.join(UNSUPPORTED)} FROM types",
            "models/raw/types.yaml": TABLE,
        },
    )
    (found,) = problems(folder)
    assert found.startswith("UNSUPPORTED_SOURCE_TYPE models/raw/types.sql ")
    for name, kind in UNSUPPORTED.items():
        assert f"{name} ({kind})" in found
    assert "c_uint64" not in found


@pytest.mark.usefixtures("password")
@pytest.mark.parametrize(
    ("sql", "sidecar", "code"),
    [
        ("SELECT 1; SELECT 2", TABLE, "INVALID_MODEL_SQL"),
        ("INSERT INTO events (event_id) VALUES (1)", TABLE, "INVALID_MODEL_SQL"),
        ("SELEC event_id FROM events", TABLE, "INVALID_MODEL_SQL"),
        ("SELECT event_id FROM missing_table", TABLE, "INVALID_MODEL_SQL"),
        # The session stays read-only: the SQL cannot lift it.
        ("SELECT event_id FROM events SETTINGS readonly = 0", TABLE, "INVALID_MODEL_SQL"),
        # A scalar subquery would be evaluated (reading rows) while ClickHouse describes it.
        (
            "SELECT event_id, (SELECT sum(value) FROM events) AS s FROM events",
            TABLE,
            "INVALID_MODEL_SQL",
        ),
        (
            "SELECT * FROM events -- WHERE event_time > {since:DateTime64(6, 'UTC')}",
            EXTRACTION,
            "MISSING_SINCE_PARAMETER",
        ),
        (
            "SELECT * FROM events WHERE meter_id > {since:String}",
            EXTRACTION,
            "MODEL_CONFIG_INVALID",
        ),
        (
            EXAMPLE,
            EXTRACTION.replace("  filter: manual\n", ""),
            "INCREMENTAL_FILTER_NOT_PROVABLE",
        ),
    ],
)
def test_rejected_models(
    tmp_path: Path, source: dict[str, str], sql: str, sidecar: str, code: str
) -> None:
    folder = project(
        tmp_path,
        source,
        {"models/raw/ch_events.sql": sql, "models/raw/ch_events.yaml": sidecar},
    )
    found = problems(folder)
    assert [item.split(" ", 1)[0] for item in found] == [code], found


def test_connection_rules(
    tmp_path: Path, source: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    models = {"models/raw/ch_events.sql": EXAMPLE, "models/raw/ch_events.yaml": EXTRACTION}
    writable = project(tmp_path / "a", source, models, read_only="false")
    assert [item.split(" ", 1)[0] for item in problems(writable)] == ["SOURCE_NOT_READ_ONLY"]
    folder = project(tmp_path / "b", source, models)
    monkeypatch.delenv(VARIABLE, raising=False)
    (missing,) = problems(folder)
    assert missing.startswith("PROJECT_SECRET_MISSING connections.yaml ")
    assert VARIABLE in missing
    # A user whose profile fixes readonly to 0 (read-write) cannot be made read-only.
    client = admin()
    writer = f"{source['reader']}_rw"
    try:
        client.command(
            f"CREATE USER {writer} IDENTIFIED WITH sha256_password BY '{READER_PASSWORD}' "
            "SETTINGS readonly = 0 CONST"
        )
        client.command(f"GRANT SELECT ON {source['database']}.* TO {writer}")
        monkeypatch.setenv(VARIABLE, READER_PASSWORD)
        fixed = project(tmp_path / "c", {**source, "reader": writer}, models)
        (refused_writer,) = problems(fixed)
        assert refused_writer.startswith("SOURCE_NOT_READ_ONLY models/raw/ch_events.sql ")
    finally:
        client.command(f"DROP USER IF EXISTS {writer}")
        client.close()
    wrong = "wrong-" + uuid.uuid4().hex
    monkeypatch.setenv(VARIABLE, wrong)
    (refused,) = problems(folder)
    assert refused.startswith("SOURCE_CONNECTION_FAILED models/raw/ch_events.sql ")
    assert wrong not in refused
    assert READER_PASSWORD not in refused
