"""P1-17 part 2 (#98): the `run` command brings extraction models' ClickHouse rows to the lake.

Needs a ClickHouse server, as `test_clickhouse.py`: `TRANSFORM_TEST_CLICKHOUSE` with an
administrator in `TRANSFORM_TEST_CLICKHOUSE_USER` / `_PASSWORD`. Each test gets its own database,
`events` table and SELECT-only user, dropped afterwards.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
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

from tests.support.services import required_in_ci

SERVER = required_in_ci("TRANSFORM_TEST_CLICKHOUSE")  # fails in CI when unset (#173)
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
# A downstream incremental DuckDB model with the automatic filter (P1-13) on the extracted table.
AUTO = "SELECT event_id, meter_id, event_time, value FROM raw.ch_events"
AUTO_SIDECAR = (
    "version: 1\nmaterialized: incremental\nincremental:\n  strategy: merge\n"
    "  unique_key: [event_id]\n  time_column: event_time\n"
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
        f"GRANT SELECT ON system.numbers TO {reader}",
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


def main_output(capsys: pytest.CaptureFixture[str], arguments: list[str]) -> tuple[int, str]:
    code = main(arguments)
    return code, capsys.readouterr().out


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
            "models/marts/events_auto.sql": AUTO,
            "models/marts/events_auto.yaml": AUTO_SIDECAR,
        },
    )
    # The downstream model keeps its automatic filter on the extracted table.
    code, out = main_output(capsys, ["validate", str(folder), "--show-sql", "marts.events_auto"])
    assert code == 0, out
    assert "$since" in out
    code, out = run(capsys, folder)
    assert code == 0, out
    first = lake_value(folder, EVENTS)
    assert len(first) == 1000
    assert first[999] == (999, "M5", "2024-01-01 00:16:39.000000", "124.8750")
    assert record(folder)[0:4] == ("success", None, None, "1000")
    assert record(folder)[4] == "1900-01-01 00:00:00"  # the lowest DateTime64 value
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
    # The automatic filter read only rows newer than its watermark: the 3 new rows, not the
    # late change to event 995 (older than the watermark; no lookback declared).
    auto = lake_value(
        folder,
        "SELECT count(*), max(CAST(value AS VARCHAR)) FILTER (WHERE event_id = 995) "
        "FROM lake.marts.events_auto",
    )
    assert auto == [(1003, "124.3750")]
    assert record(folder, "marts.events_auto")[0] == "success"
    assert record(folder, "marts.events_auto")[4] is not None  # a real `$since`, not a rebuild
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
    outcome, error_code, message, rows_read, *_ = record(folder)
    assert (outcome, error_code) == ("failed", "SOURCE_QUERY_FAILED")
    assert "stop here" in str(message)
    assert str(rows_read).isdigit()  # the rows read before the failure are recorded
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


def test_ctrl_c_records_the_run_and_stops_the_clickhouse_query(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    table = "version: 1\nconnection: analytics_ch\n"
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": "SELECT event_id, meter_id FROM events",
            "models/raw/ch_events.yaml": table,
        },
    )
    assert run(capsys, folder)[0] == 0
    before = lake_value(folder, "SELECT count(*) FROM lake.raw.ch_events")
    snapshots = lake_value(folder, "SELECT count(*) FROM ducklake_snapshots('lake')")
    # About 2.6 s without output per block of 65,536 rows, for ever.
    mark = f"interrupted_{uuid.uuid4().hex[:8]}"
    project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": (
                f"SELECT number AS event_id, toString(number) AS meter_id, 1 AS {mark} "
                "FROM system.numbers WHERE sleepEachRow(0.00004) = 0"
            ),
        },
    )
    # Validation and the first block take about 3 s: by 6 s the rows are being loaded.
    timer = threading.Timer(6.0, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            main(["run", str(folder)])
    finally:
        timer.cancel()
    out = capsys.readouterr().out
    assert "STORE_CANCELLED" in out
    outcome, error_code, _, rows_read, *_ = record(folder)
    assert (outcome, error_code) == ("failed", "STORE_CANCELLED")
    assert str(rows_read).isdigit()
    assert lake_value(folder, "SELECT count(*) FROM lake.raw.ch_events") == before
    # Only the run record was committed after the interruption.
    after = lake_value(folder, "SELECT count(*) FROM ducklake_snapshots('lake')")
    assert after == [(int(str(snapshots[0][0])) + 1,)]
    deadline = time.monotonic() + 5
    while running(mark) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert running(mark) == 0


def running(mark: str) -> int:
    client = admin()
    try:
        return int(
            client.command(  # type: ignore[arg-type]  # a one-value SELECT returns its value
                f"SELECT count() FROM system.processes WHERE query LIKE '%{mark}%' "
                "AND query NOT LIKE '%system.processes%'"
            )
        )
    finally:
        client.close()


def test_a_partition_overwrite_model_is_checked_against_the_since_clickhouse_used(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # `{since:DateTime64(0, 'UTC')}` truncates the watermark 00:16:39.500 to 00:16:39, so the
    # last row is read again. Checked against the watermark, that row would count as the
    # partition's older rows reloaded, and the overwrite would delete the other 999 (#98).
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": (
                "SELECT event_id, month, read_at FROM (SELECT event_id, "
                "toString(toStartOfMonth(event_time)) AS month, "
                "addMilliseconds(event_time, 500) AS read_at FROM events) "
                "WHERE read_at > {since:DateTime64(0, 'UTC')}"
            ),
            "models/raw/ch_events.yaml": (
                "version: 1\nconnection: analytics_ch\nmaterialized: incremental\n"
                "incremental:\n  strategy: partition_overwrite\n  time_column: read_at\n"
                "  partition_by: [month]\n  filter: manual\n"
            ),
        },
    )
    code, out = run(capsys, folder)
    assert code == 0, out
    count = "SELECT count(*) FROM lake.raw.ch_events"
    assert lake_value(folder, count) == [(1000,)]
    code, out = run(capsys, folder)  # nothing changed at the source
    assert code == 1, out
    assert "PARTITION_INCOMPLETE" in out
    assert "'2024-01-01'" in out
    outcome, error_code, _, rows_read, since, _ = record(folder)
    assert (outcome, error_code, rows_read) == ("failed", "PARTITION_INCOMPLETE", "1")
    assert since == "2024-01-01 00:16:39"
    assert lake_value(folder, count) == [(1000,)]  # nothing deleted


def test_a_whole_partition_filter_passes_the_check(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The same model reloading whole months: the check passes and nothing is lost. The filter
    # compares read_at itself (#155: `toStartOfMonth(read_at) >= …` is refused).
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": (
                "SELECT event_id, month, read_at FROM (SELECT event_id, "
                "toString(toStartOfMonth(event_time)) AS month, "
                "addMilliseconds(event_time, 500) AS read_at FROM events) "
                "WHERE read_at >= toStartOfMonth({since:DateTime64(0, 'UTC')})"
            ),
            "models/raw/ch_events.yaml": (
                "version: 1\nconnection: analytics_ch\nmaterialized: incremental\n"
                "incremental:\n  strategy: partition_overwrite\n  time_column: read_at\n"
                "  partition_by: [month]\n  filter: manual\n"
            ),
        },
    )
    assert run(capsys, folder)[0] == 0
    code, out = run(capsys, folder)
    assert code == 0, out
    assert record(folder)[0:4] == ("success", None, None, "1000")
    assert lake_value(folder, "SELECT count(*) FROM lake.raw.ch_events") == [(1000,)]


PARTIAL_SIDECAR = (
    "version: 1\nconnection: analytics_ch\nmaterialized: incremental\n"
    "incremental:\n  strategy: partition_overwrite\n  time_column: read_at\n"
    "  partition_by: [month]\n  filter: manual\n"
)


def test_a_partial_reload_fails_and_keeps_the_table(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # #155: `>= {since:…}` reloads the last row of the month again; under the #95 rule that row
    # counted as the partition's older rows and the overwrite deleted the other 999.
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": (
                "SELECT event_id, toString(toStartOfMonth(event_time)) AS month, "
                "event_time AS read_at FROM events "
                "WHERE read_at >= {since:DateTime64(6, 'UTC')}"
            ),
            "models/raw/ch_events.yaml": PARTIAL_SIDECAR,
        },
    )
    assert run(capsys, folder)[0] == 0
    count = "SELECT count(*) FROM lake.raw.ch_events"
    assert lake_value(folder, count) == [(1000,)]
    code, out = run(capsys, folder)  # nothing changed at the source
    assert code == 1, out
    assert "PARTITION_INCOMPLETE" in out
    assert record(folder)[0:2] == ("failed", "PARTITION_INCOMPLETE")
    assert lake_value(folder, count) == [(1000,)]  # nothing deleted


def test_a_since_filter_on_another_column_is_refused_before_anything_runs(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # #155: the filter compares event_time, but the time column is read_at.
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": (
                "SELECT event_id, toString(toStartOfMonth(event_time)) AS month, "
                "addMilliseconds(event_time, 500) AS read_at FROM events "
                "WHERE event_time > {since:DateTime64(6, 'UTC')}"
            ),
            "models/raw/ch_events.yaml": PARTIAL_SIDECAR,
        },
    )
    code, out = run(capsys, folder)
    assert code == 1, out
    assert "INVALID_MODEL_SQL" in out
    assert "time_column read_at" in out
    assert not (folder / f".{SLUG}" / "lake").exists()  # nothing created


def test_a_date_time_column_with_a_month_filter_is_accepted(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # #155, accepted form: a Date time column, `{since:Date}` in its lower bound. The model's
    # own `identity(1)` is not taken for the check's mark (#162 Reviewer passes 1 and 2).
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": (
                "SELECT event_id, toString(toStartOfMonth(event_time)) AS month, "
                "toDate(event_time) AS day, identity(1) AS one FROM events "
                "WHERE day >= toStartOfMonth({since:Date})"
            ),
            "models/raw/ch_events.yaml": PARTIAL_SIDECAR.replace(
                "time_column: read_at", "time_column: day"
            ),
        },
    )
    code, out = run(capsys, folder)
    assert code == 0, out
    code, out = run(capsys, folder)  # the whole month again: nothing lost
    assert code == 0, out
    assert lake_value(folder, "SELECT count(*) FROM lake.raw.ch_events") == [(1000,)]


def test_a_since_bound_that_reads_another_column_is_refused(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # #162 Reviewer pass 1: a forged mark, and a bound that depends on a finer column.
    sidecar = PARTIAL_SIDECAR.replace("time_column: read_at", "time_column: day")
    for number, where in enumerate(
        (
            "day >= identity(identity({since:Date}))",
            "day >= identity(if(event_time > {since:DateTime64(3, 'UTC')}, "
            "toDate('1970-01-01'), toDate('2200-01-01')))",
        )
    ):
        folder = project(
            tmp_path / str(number),
            source,
            {
                "models/raw/ch_events.sql": (
                    "SELECT event_id, toString(toStartOfMonth(event_time)) AS month, "
                    f"toDate(event_time) AS day FROM events WHERE {where}"
                ),
                "models/raw/ch_events.yaml": sidecar,
            },
        )
        code, out = run(capsys, folder)
        assert code == 1, out
        assert "INVALID_MODEL_SQL" in out


@pytest.mark.parametrize(
    "sql",
    [
        # Reviewer pass 2: rows that depend on other rows or other data.
        "SELECT * FROM events WHERE day >= toStartOfMonth({since:Date}) "
        "AND event_id IN (SELECT event_id FROM events WHERE day >= {since:Date})",
        "SELECT * FROM events WHERE day >= toStartOfMonth({since:Date}) "
        "AND day NOT IN (SELECT day FROM events WHERE day >= {since:Date})",
        "SELECT * FROM events WHERE day >= toStartOfMonth({since:Date}) "
        "EXCEPT ALL SELECT * FROM events WHERE day >= {since:Date}",
        "SELECT e.* FROM events AS e JOIN events AS c USING (event_id) WHERE c.day >= {since:Date}",
        "SELECT * FROM events WHERE day >= toStartOfMonth({since:Date}) "
        "AND day < (SELECT min(day) + 3 FROM events WHERE day >= {since:Date})",
        "SELECT * FROM events WHERE EXISTS (SELECT 1 FROM events AS s WHERE s.day > {since:Date})",
        "SELECT * FROM events WHERE day > {since:Date} INTERSECT SELECT * FROM events",
        # Reviewer pass 3: a time column computed over the filtered rows, or rows dropped.
        "SELECT month, max(e.day) AS day, count() AS n FROM events AS e "
        "WHERE e.day >= {since:Date} GROUP BY month",
        "SELECT event_id, month, toDate(event_time) AS day FROM events AS e "
        "WHERE e.day >= {since:Date}",
        "SELECT *, count() OVER (PARTITION BY month) AS n FROM events WHERE day >= {since:Date}",
        "SELECT * FROM events WHERE day >= {since:Date} LIMIT 1 BY month",
        "SELECT * FROM events WHERE day >= {since:Date} ORDER BY day LIMIT 10",
        "SELECT * FROM events WHERE day >= {since:Date} - rand() % 30",
        # Reviewer pass 4: a LIMIT whose value is a name; a UNION with the column elsewhere.
        "WITH 25 AS n SELECT * FROM events WHERE day >= {since:Date} ORDER BY day LIMIT n",
        "WITH 25 AS n SELECT * FROM (SELECT * FROM events WHERE day >= {since:Date}) LIMIT n",
        "SELECT event_id, day, month FROM events WHERE day >= {since:Date} "
        "UNION ALL SELECT event_id, month, day FROM events WHERE day >= {since:Date}",
        "SELECT * FROM events PREWHERE event_id > 0 WHERE day >= {since:Date}",
        # Reviewer pass 5: a UNION; a LIMIT written as a condition.
        "SELECT * FROM events WHERE day >= {since:Date} UNION ALL SELECT * FROM events",
        "SELECT * FROM events WHERE day >= {since:Date} LIMIT 1 > 0",
        "SELECT * FROM (SELECT * FROM events WHERE day >= {since:Date}) LIMIT NOT 0",
        # Reviewer pass 6: the select list's `day` read as an alias defined elsewhere.
        "WITH toDate(event_time) AS day SELECT event_id, month, day FROM events AS e "
        "WHERE e.day >= {since:Date}",
        "SELECT event_id, month, day FROM events AS e WHERE e.day >= {since:Date} "
        "ORDER BY toDate(event_time) AS day",
        # Reviewer pass 7: a name with a space, read only up to it in `EXPLAIN AST`; a column
        # that differs only in letter case (ClickHouse names are case-sensitive).
        "SELECT event_id, month, day FROM (SELECT *, toDate(event_time) AS `day read` "
        "FROM events) WHERE `day read` >= {since:Date}",
        "SELECT event_id, month, Day AS day FROM (SELECT event_id, month, "
        "toDate(event_time) AS Day, toDate('2026-10-01') AS day FROM events) AS cs "
        "WHERE cs.day >= {since:Date}",
    ],
    ids=[
        "in",
        "not-in",
        "except-all",
        "join",
        "scalar-subquery",
        "exists",
        "intersect",
        "aggregate",
        "computed-qualified",
        "window",
        "limit-by",
        "limit",
        "random-bound",
        "limit-a-name",
        "limit-a-name-outside",
        "union-other-order",
        "prewhere-and-where",
        "union-all",
        "limit-a-condition",
        "limit-not-zero-outside",
        "with-alias",
        "order-by-alias",
        "spaced-name",
        "letter-case",
    ],
)
def test_a_since_bound_the_oldest_row_check_cannot_follow_is_refused(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str], sql: str
) -> None:
    # The source's `events` has no `day` or `month`: a subquery adds them, so each form reads
    # like a model over a dated table.
    dated = (
        "(SELECT event_id, event_time, toDate(event_time) AS day, "
        "toString(toStartOfMonth(event_time)) AS month FROM events)"
    )
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": sql.replace("FROM events", f"FROM {dated}"),
            "models/raw/ch_events.yaml": PARTIAL_SIDECAR.replace(
                "time_column: read_at", "time_column: day"
            ),
        },
    )
    code, out = run(capsys, folder)
    assert code == 1, out
    assert "INVALID_MODEL_SQL" in out
    assert not (folder / f".{SLUG}" / "lake").exists()  # nothing created


def zoned_events(tmp_path: Path, source: dict[str, str], declared: str) -> Path:
    """A reading every 6 hours of February, in a Tokyo-zoned column, partitioned by month."""
    return project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": (
                "SELECT n AS id, toString(toStartOfMonth(t)) AS month, t FROM (SELECT number AS n, "
                "toTimeZone(toDateTime64('2024-02-01 00:00:00', 3, 'UTC') + "
                "toIntervalHour(6 * number), 'Asia/Tokyo') AS t FROM numbers(111)) "
                f"WHERE t > {{since:{declared}}}"
            ),
            "models/raw/ch_events.yaml": (
                "version: 1\nconnection: analytics_ch\nmaterialized: incremental\n"
                "incremental:\n  strategy: partition_overwrite\n  time_column: t\n"
                "  partition_by: [month]\n  filter: manual\n"
            ),
        },
    )


@pytest.mark.parametrize("declared", ["Date", "Date32"])
def test_a_date_since_on_a_zoned_column_is_refused_before_anything_runs(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str], declared: str
) -> None:
    # ClickHouse compares a `{since:Date}` with a DateTime column at midnight in a zone the run
    # cannot see (here Tokyo, not the server's zone), so no boundary the partition check could
    # use is exact. A partition_overwrite model would risk deleting rows: refused at validation.
    folder = zoned_events(tmp_path, source, declared)
    code, out = run(capsys, folder)
    assert code == 1, out
    assert "INVALID_MODEL_SQL" in out
    assert "ch_events" in out
    assert not (folder / f".{SLUG}" / "lake").exists()  # nothing created


def test_a_time_since_on_a_zoned_column_is_exact(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The supported form: a time parameter as precise as the column. The second run reads the
    # readings after the watermark only (none), and the month is kept whole.
    folder = zoned_events(tmp_path, source, "DateTime64(3, 'Asia/Tokyo')")
    code, out = run(capsys, folder)
    assert code == 0, out
    count = "SELECT count(*) FROM lake.raw.ch_events"
    assert lake_value(folder, count) == [(111,)]
    code, out = run(capsys, folder)  # nothing changed at the source
    assert code == 0, out
    assert record(folder)[0:4] == ("success", None, None, "0")
    assert lake_value(folder, count) == [(111,)]


def reconciled(tmp_path: Path, source: dict[str, str], rows: int) -> Path:
    """The §3.5 extraction model with reconciliation checks against a Parquet copy of the
    source's first `rows` events (DuckDB cannot read ClickHouse, so the copy stands in)."""
    folder = project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": EXAMPLE,
            "models/raw/ch_events.yaml": EXTRACTION
            + "reconciliation:\n"
            + "  - {name: rows, measure: count(*), source: feed.data}\n"
            + "  - {name: total, measure: sum(value), source: feed.data}\n",
        },
    )
    (folder / "data").mkdir()
    copy = duckdb.connect()
    copy.execute(
        "COPY (SELECT i::UBIGINT AS event_id, CAST(i / 8 AS DECIMAL(18,4)) AS value "
        f"FROM range({rows}) AS r(i)) TO '{folder / 'data' / 'feed.parquet'}'"
    )
    copy.close()
    with (folder / "connections.yaml").open("a", encoding="utf-8") as connections:
        connections.write("  feed:\n    type: parquet\n    path: data/feed.parquet\n")
    return folder


def reconciliation_results(folder: Path) -> list[dict[str, object]]:
    (row,) = lake_value(
        folder,
        "SELECT reconciliation FROM lake._meta.run_records WHERE model = 'raw.ch_events' "
        "ORDER BY started_at DESC LIMIT 1",
    )
    found: list[dict[str, object]] = json.loads(str(row[0]))
    return found


def test_an_extraction_model_is_reconciled_after_its_load(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The issue's goal: after the load, the usual tests and reconciliation apply.
    folder = reconciled(tmp_path, source, 1000)
    code, out = main_output(capsys, ["validate", str(folder)])
    assert code == 0, out
    code, out = run(capsys, folder)
    assert code == 0, out
    assert reconciliation_results(folder) == [
        {"name": "rows", "outcome": "passed", "failing_rows": "0"},
        {"name": "total", "outcome": "passed", "failing_rows": "0"},
    ]
    assert lake_value(folder, "SELECT count(*) FROM lake.raw.ch_events") == [(1000,)]


def test_a_failed_reconciliation_rolls_the_extraction_back(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    folder = reconciled(tmp_path, source, 999)  # the source copy lacks the last event
    code, out = run(capsys, folder)
    assert code == 1, out
    assert "RECONCILIATION_FAILED" in out
    assert "Traceback" not in out
    assert reconciliation_results(folder) == [
        {"name": "rows", "outcome": "failed", "failing_rows": "1"},
        {"name": "total", "outcome": "failed", "failing_rows": "1"},
    ]
    # Only the run records commit: the extracted rows are rolled back with the run.
    assert lake_value(
        folder,
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_catalog = 'lake' AND table_name = 'ch_events'",
    ) == [(0,)]


APPEND_SIDECAR = (
    "version: 1\nconnection: analytics_ch\nmaterialized: incremental\n"
    "incremental:\n  strategy: append\n  time_column: t\n  filter: manual\n"
)


def appended(tmp_path: Path, source: dict[str, str], column: str, bound: str) -> Path:
    """Three readings 100 ms apart (ids 1 to 3, or one a day for a `Date` column), in an
    `append` extraction model whose time column `t` is `column` and whose filter is `bound`."""
    times = {
        "DateTime64(3, 'UTC')": "toDateTime64('2024-01-01 00:00:00.100', 3, 'UTC') "
        "+ toIntervalMillisecond(100 * number)",
        "DateTime('UTC')": "toDateTime('2024-01-01 00:00:00', 'UTC') + toIntervalSecond(number)",
        "Date": "toDate('2024-01-01') + number",
    }[column]
    return project(
        tmp_path,
        source,
        {
            "models/raw/ch_events.sql": (
                f"SELECT n AS id, t FROM (SELECT number + 1 AS n, {times} AS t FROM numbers(3)) "
                f"WHERE {bound}"
            ),
            "models/raw/ch_events.yaml": APPEND_SIDECAR,
        },
    )


@pytest.mark.parametrize(
    ("column", "bound"),
    [
        ("DateTime('UTC')", "t > {since:Date}"),
        ("DateTime64(3, 'UTC')", "t > {since:DateTime64(0, 'UTC')}"),
        ("DateTime64(3, 'UTC')", "t > {since:DateTime('UTC')}"),
        ("DateTime64(3, 'UTC')", "t >= {since:DateTime64(3, 'UTC')}"),
        ("DateTime64(3, 'UTC')", "t > {since:DateTime64(3, 'UTC')} OR t IS NULL"),
        ("DateTime64(3, 'UTC')", "t > {since:DateTime64(3, 'UTC')} - INTERVAL 1 SECOND"),
        ("Date", "t > {since:DateTime('UTC')}"),
    ],
    ids=[
        "date-on-datetime",
        "seconds-on-milliseconds",
        "datetime-on-milliseconds",
        "inclusive",
        "or-null",
        "widened",
        "datetime-on-date",
    ],
)
def test_an_append_filter_that_reads_rows_again_is_refused_before_anything_runs(
    tmp_path: Path,
    source: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    column: str,
    bound: str,
) -> None:
    # #135: an append model inserts every row it reads, so a filter that reads a row twice
    # would insert it twice (Reviewer pass 4 on #129: ids [1, 1, 2, 2, 3]).
    folder = appended(tmp_path, source, column, bound)
    code, out = run(capsys, folder)
    assert code == 1, out
    assert "INVALID_MODEL_SQL" in out
    assert "ch_events" in out
    assert not (folder / f".{SLUG}" / "lake").exists()  # nothing created


@pytest.mark.parametrize(
    ("column", "bound"),
    [
        ("DateTime64(3, 'UTC')", "t > {since:DateTime64(3, 'UTC')}"),
        ("DateTime64(3, 'UTC')", "{since:DateTime64(6, 'UTC')} < t"),
        ("Date", "t > {since:Date}"),
    ],
    ids=["as-precise", "more-precise-reversed", "date-on-date"],
)
def test_an_append_filter_that_reads_each_row_once_inserts_it_once(
    tmp_path: Path,
    source: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    column: str,
    bound: str,
) -> None:
    folder = appended(tmp_path, source, column, bound)
    ids = "SELECT id FROM lake.raw.ch_events ORDER BY id"
    code, out = run(capsys, folder)
    assert code == 0, out
    assert lake_value(folder, ids) == [(1,), (2,), (3,)]
    code, out = run(capsys, folder)  # nothing changed at the source: nothing read again
    assert code == 0, out
    assert lake_value(folder, ids) == [(1,), (2,), (3,)]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT n AS id, now64(3, 'UTC') AS t FROM (SELECT number + 1 AS n FROM numbers(3)) "
        "WHERE t > {since:DateTime64(3, 'UTC')}",
        "SELECT * FROM (SELECT number + 1 AS id, now64(3, 'UTC') AS t FROM numbers(3)) "
        "WHERE t > {since:DateTime64(3, 'UTC')}",
        # Reviewer pass 2: a tuple's field, and REPLACE, are not traced: refused.
        "SELECT * FROM (SELECT id, tup.t AS t FROM (SELECT number + 1 AS id, "
        "tuple(now64(3, 'UTC')) AS tup FROM numbers(3))) WHERE t > {since:DateTime64(3, 'UTC')}",
        "SELECT * REPLACE (now64(3, 'UTC') AS t) FROM (SELECT number + 1 AS id, "
        "toDateTime64('2024-01-01', 3, 'UTC') AS t FROM numbers(3)) "
        "WHERE t > {since:DateTime64(3, 'UTC')}",
        # Reviewer pass 3: a tuple named like its subquery.
        "SELECT * FROM (SELECT id, x.t AS t FROM (SELECT number AS id, "
        "CAST(tuple(now64(3, 'UTC')), 'Tuple(t DateTime64(3, \\'UTC\\'))') AS x "
        "FROM numbers(3)) AS x) WHERE t > {since:DateTime64(3, 'UTC')}",
    ],
    ids=["computed-here", "computed-below", "tuple-field", "replace", "tuple-named-like-table"],
)
def test_an_append_time_column_that_changes_every_run_is_refused(
    tmp_path: Path, source: dict[str, str], capsys: pytest.CaptureFixture[str], sql: str
) -> None:
    # #135 Reviewer pass 1: every row would come back with a later time and be inserted again.
    folder = project(
        tmp_path,
        source,
        {"models/raw/ch_events.sql": sql, "models/raw/ch_events.yaml": APPEND_SIDECAR},
    )
    code, out = run(capsys, folder)
    assert code == 1, out
    assert "INVALID_MODEL_SQL" in out
    assert not (folder / f".{SLUG}" / "lake").exists()  # nothing created
