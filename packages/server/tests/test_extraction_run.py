"""P1-17 part 2 (#98): `orvex run` brings an extraction model's rows from ClickHouse into the lake.

Needs a ClickHouse server, as `test_clickhouse.py`: `TRANSFORM_TEST_CLICKHOUSE` with an
administrator in `TRANSFORM_TEST_CLICKHOUSE_USER` / `_PASSWORD`. Each test gets its own database,
`events` table and SELECT-only user, dropped afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import clickhouse_connect
import duckdb
import pytest
from clickhouse_connect.driver.client import Client
from orvex.server.cli import main
from orvex.server.identity import load_identity

SERVER = os.environ.get("TRANSFORM_TEST_CLICKHOUSE")
pytestmark = pytest.mark.skipif(SERVER is None, reason="no ClickHouse server for tests")

SLUG = load_identity().slug
READER_PASSWORD = "reader-" + uuid.uuid4().hex
VARIABLE = "TEST_CLICKHOUSE_RUN_PASSWORD"
EXTRACTION = (
    "version: 1\nconnection: analytics_ch\nmaterialized: incremental\nincremental:\n"
    "  strategy: merge\n  unique_key: [event_id]\n  time_column: event_time\n  lookback: 10m\n"
    "  filter: manual\n"
)
EXAMPLE = (
    "SELECT event_id, meter_id, event_time, toDecimal64(value, 4) AS value\nFROM events\n"
    "WHERE event_time > {since:DateTime64(6, 'UTC')}\n"
)
DOWNSTREAM = (
    "SELECT meter_id, count(*) AS events, sum(value) AS value FROM raw.ch_events GROUP BY ALL"
)


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


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """A database with 1,000 `events` (one per second from 2024-01-01 00:00 UTC) and a user
    that may only SELECT from it."""
    monkeypatch.setenv(VARIABLE, READER_PASSWORD)
    suffix = uuid.uuid4().hex[:12]
    database, reader = f"run_{suffix}", f"runner_{suffix}"
    client = admin()
    for statement in [
        f"CREATE DATABASE {database}",
        f"CREATE TABLE {database}.events (event_id UInt64, meter_id String, "
        "event_time DateTime64(6, 'UTC'), value Float64) ENGINE = MergeTree ORDER BY event_id",
        f"INSERT INTO {database}.events SELECT number, concat('M', toString(number % 7)), "
        "toDateTime64('2024-01-01 00:00:00', 6, 'UTC') + number, number / 8 FROM numbers(1000)",
        f"CREATE USER {reader} IDENTIFIED WITH sha256_password BY '{READER_PASSWORD}'",
        f"GRANT SELECT ON {database}.* TO {reader}",
    ]:
        client.command(statement)
    try:
        yield {"database": database, "reader": reader}
    finally:
        client.command(f"DROP USER IF EXISTS {reader}")
        client.command(f"DROP DATABASE IF EXISTS {database}")
        client.close()


def project(folder: Path, source: dict[str, str], models: dict[str, str]) -> Path:
    assert SERVER is not None
    url = urlsplit(SERVER)
    files = {
        "project.yaml": "version: 1\nname: Test\ntime_zone: UTC\n",
        "connections.yaml": (
            "version: 1\nconnections:\n  analytics_ch:\n    type: clickhouse\n"
            f"    host: {url.hostname}\n    port: {url.port}\n"
            f"    database: {source['database']}\n    user: {source['reader']}\n"
            f"    password: ${{env:{VARIABLE}}}\n    secure: {str(url.scheme == 'https').lower()}\n"
            "    read_only: true\n"
        ),
        **models,
    }
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


def run(capsys: pytest.CaptureFixture[str], folder: Path, *extra: str) -> tuple[int, str]:
    code = main(["run", str(folder), *extra])
    return code, capsys.readouterr().out


def lake_value(folder: Path, sql: str) -> list[tuple[object, ...]]:
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    lake.execute("SET TimeZone = 'UTC'")
    catalog = folder / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    result = lake.execute(sql).fetchall()
    lake.close()
    return result


EVENTS = (
    "SELECT event_id, meter_id, strftime(event_time, '%Y-%m-%d %H:%M:%S.%f'), "
    "CAST(value AS VARCHAR) FROM lake.raw.ch_events ORDER BY event_id"
)


def record(folder: Path, model: str = "raw.ch_events") -> tuple[object, ...]:
    (row,) = lake_value(
        folder,
        "SELECT outcome, error_code, error_message, CAST(rows_read AS VARCHAR), since, watermark "
        f"FROM lake._meta.run_records WHERE model = '{model}' ORDER BY started_at DESC LIMIT 1",
    )
    return row


def test_incremental_runs_merge_new_and_late_rows(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": EXAMPLE,
            "models/raw/ch_events.yaml": EXTRACTION,
            "models/marts/by_meter.sql": DOWNSTREAM,
        },
    )
    code, out = run(capsys, folder)
    assert code == 0, out
    first = lake_value(folder, EVENTS)
    assert len(first) == 1000
    assert first[999] == (999, "M5", "2024-01-01 00:16:39.000000", "124.8750")
    assert record(folder)[0:4] == ("success", None, None, "1000")
    assert record(folder)[4] == "1900-01-01 00:00:00.000000"  # the lowest DateTime64 value
    assert record(folder)[5] == "2024-01-01T00:16:39.000000+00:00"
    # Downstream DuckDB model, in the same run, after the extraction model.
    assert lake_value(folder, "SELECT sum(events) FROM lake.marts.by_meter") == [(1000,)]
    # 3 new rows, and 1 changed row inside the 10m lookback (event 995).
    client = admin()
    try:
        client.command(
            f"INSERT INTO {source['database']}.events SELECT 1000 + number, 'M9', "
            "toDateTime64('2024-01-01 00:20:00', 6, 'UTC') + number, 1 FROM numbers(3)"
        )
        client.command(
            f"ALTER TABLE {source['database']}.events UPDATE value = 7777 WHERE event_id = 995 "
            "SETTINGS mutations_sync = 2"
        )
    finally:
        client.close()
    code, out = run(capsys, folder)
    assert code == 0, out
    second = lake_value(folder, EVENTS)
    assert len(second) == 1003
    changed = [row for row in second if row not in first]
    assert changed == [
        (995, "M1", "2024-01-01 00:16:35.000000", "7777.0000"),
        (1000, "M9", "2024-01-01 00:20:00.000000", "1.0000"),
        (1001, "M9", "2024-01-01 00:20:01.000000", "1.0000"),
        (1002, "M9", "2024-01-01 00:20:02.000000", "1.0000"),
    ]
    outcome, _, _, rows, since, watermark = record(folder)
    assert outcome == "success"
    # Watermark 00:16:39 minus the 10m lookback: rows after 00:06:39 are re-read (600 + 3).
    assert since == "2024-01-01 00:06:39.000000"
    assert rows == "603"
    assert watermark == "2024-01-01T00:20:02.000000+00:00"
    assert lake_value(folder, "SELECT sum(events) FROM lake.marts.by_meter") == [(1003,)]
    # The run's ClickHouse query was read-only, with `since` bound by the server.
    logs = admin()
    try:
        logs.command("SYSTEM FLUSH LOGS")
        found = logs.query(
            "SELECT Settings['readonly'], query FROM system.query_log WHERE type = 'QueryFinish' "
            "AND user = {reader:String} AND query LIKE '%rowNumberInAllBlocks%'",
            parameters={"reader": source["reader"]},
        ).result_rows
    finally:
        logs.close()
    assert len(found) == 2
    assert all(readonly == "1" for readonly, _ in found)
    assert all("_CAST('" in str(query) for _, query in found)


def test_a_table_model_reloads_every_row(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": "SELECT event_id, meter_id FROM events",
            "models/raw/ch_events.yaml": "version: 1\nconnection: analytics_ch\n",
        },
    )
    code, out = run(capsys, folder)
    assert code == 0, out
    assert "built raw.ch_events: 1000 rows" in out
    assert record(folder)[0:4] == ("success", None, None, "1000")
    assert record(folder)[4] is None  # no `{since:…}` in a table model


def test_a_clickhouse_error_mid_stream_rolls_back(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": EXAMPLE,
            "models/raw/ch_events.yaml": EXTRACTION,
        },
    )
    assert run(capsys, folder)[0] == 0
    before = lake_value(folder, EVENTS)
    failing = EXAMPLE.replace(
        "toDecimal64(value, 4) AS value",
        "toDecimal64(value, 4) AS value, throwIf(event_id = 600, 'stop here') AS boom",
    )
    project(tmp_path, source, {"models/raw/ch_events.sql": failing})
    code, out = run(capsys, folder)
    assert code == 1
    assert "SOURCE_QUERY_FAILED" in out
    assert "(ClickHouse code 395)" in out
    assert READER_PASSWORD not in out
    outcome, error_code, message, *_ = record(folder)
    assert (outcome, error_code) == ("failed", "SOURCE_QUERY_FAILED")
    assert "stop here" in str(message)
    assert lake_value(folder, EVENTS) == before  # nothing written


def test_text_that_is_not_utf8_fails_the_run(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": (
                "SELECT event_id, if(event_id = 41, unhex('C328'), meter_id) AS meter_id "
                "FROM events ORDER BY event_id"
            ),
            "models/raw/ch_events.yaml": "version: 1\nconnection: analytics_ch\n",
        },
    )
    code, out = run(capsys, folder)
    assert code == 1
    assert "UNSUPPORTED_SOURCE_VALUE" in out
    assert "meter_id (row 42 of the stream)" in out
    assert record(folder)[0:2] == ("failed", "UNSUPPORTED_SOURCE_VALUE")


def test_a_wrong_password_never_shows_it(
    tmp_path: Path,
    source: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": EXAMPLE,
            "models/raw/ch_events.yaml": EXTRACTION,
        },
    )
    wrong = "wrong-" + uuid.uuid4().hex
    monkeypatch.setenv(VARIABLE, wrong)
    code, out = run(capsys, folder)
    assert code == 1
    assert "SOURCE_CONNECTION_FAILED" in out
    assert wrong not in out
    assert READER_PASSWORD not in out
