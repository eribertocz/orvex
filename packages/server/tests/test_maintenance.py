"""P1-20 (#68): the `maintain` command, its record, and lake maintenance in `schedule`.

Clocks are fake where time matters: a lake ages two days in a moment.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.maintenance_record.v1 import MaintenanceRecord
from orvex.server import commands, maintenance
from orvex.server.cli import main
from orvex.server.identity import load_identity
from orvex.server.scheduler import schedule
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

IDENTITY = load_identity()
SLUG = IDENTITY.slug
CONNECTIONS = "version: 1\nconnections:\n  feed:\n    type: parquet\n    path: data/f.parquet\n"
INCREMENTAL = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: append, time_column: read_at, filter: manual}\n"
)


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def feed(folder: Path, first: int, last: int) -> None:
    """Readings `first`..`last`, one hour apart from 2024-01-01."""
    (folder / "data").mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.execute(
        "COPY (SELECT i::BIGINT AS id, TIMESTAMPTZ '2024-01-01 00:00:00+00' + "
        f"INTERVAL (i) HOUR AS read_at FROM range({first}, {last} + 1) AS r(i)) "
        f"TO '{folder / 'data' / 'f.parquet'}'"
    )
    connection.close()


def project(folder: Path, retention: str = "1d") -> Path:
    write(
        folder,
        {
            "project.yaml": (
                f"version: 1\nname: Test\ntime_zone: UTC\nretention:\n  snapshots: {retention}\n"
            ),
            "connections.yaml": CONNECTIONS,
            "models/raw/r.sql": "SELECT * FROM feed.data WHERE read_at > $since",
            "models/raw/r.yaml": INCREMENTAL,
        },
    )
    return folder


def grow(folder: Path, runs: int) -> None:
    """`runs` incremental runs, each appending 1,000 new readings (one small file each)."""
    for run in range(runs):
        feed(folder, 0, (run + 1) * 1000 - 1)
        out = io.StringIO()
        assert commands.run(folder, IDENTITY, out) == 0, out.getvalue()


def lake_rows(folder: Path, sql: str) -> list[tuple[object, ...]]:
    connection = duckdb.connect()
    connection.execute("INSTALL ducklake")
    connection.execute("LOAD ducklake")
    catalog = folder / f".{SLUG}" / "lake" / "catalog.ducklake"
    connection.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    rows = connection.execute(sql).fetchall()
    connection.close()
    return rows


def model_files(folder: Path) -> list[Path]:
    data = folder / f".{SLUG}" / "lake" / "data"
    return [path for path in data.rglob("*.parquet") if path.parent.name == "r"]


def later(days: float) -> maintenance.Clock:
    moment = dt.datetime.now(dt.UTC) + dt.timedelta(days=days)
    return lambda: moment


def test_an_aged_lake_is_maintained_and_recorded(tmp_path: Path) -> None:
    folder = project(tmp_path)
    grow(folder, 6)
    before = lake_rows(folder, "SELECT count(*), sum(id) FROM lake.raw.r")
    assert before == [(6000, sum(range(6000)))]
    assert len(model_files(folder)) >= 6
    data = folder / f".{SLUG}" / "lake" / "data"
    on_disk = set(data.rglob("*.parquet"))
    out = io.StringIO()
    # Two days later, one day of retention: everything but the current snapshot expires.
    assert maintenance.maintain(folder, IDENTITY, out, clock=later(2)) == 0, out.getvalue()
    assert out.getvalue().startswith("maintenance done: ")
    assert lake_rows(folder, "SELECT count(*), sum(id) FROM lake.raw.r") == before
    assert len(model_files(folder)) == 1
    (row,) = lake_rows(
        folder,
        "SELECT run_id, trigger, retention, report, outcome, error_code "
        "FROM lake._meta.maintenance_runs",
    )
    assert row[1:3] == ("manual", "1d")
    assert row[4] == "succeeded"
    assert row[5] is None
    report = json.loads(str(row[3]))
    assert report["files_merged"] >= 6
    # Exactly the files that left the disk, and every snapshot below the one kept.
    assert report["files_deleted"] == len(on_disk - set(data.rglob("*.parquet")))
    assert report["files_deleted"] >= 6
    assert report["snapshots_expired"] == report["oldest_snapshot_id"]
    history = io.StringIO()
    assert maintenance.history(folder, IDENTITY, 5, history) == 0
    assert history.getvalue().startswith(f"{row[0]}  ")
    assert "succeeded" in history.getvalue()
    # Through the command line too (a second maintenance, now with nothing old enough).
    assert main(["maintain", str(folder)]) == 0
    assert main(["runs", str(folder), "--maintenance"]) == 0


def test_a_failed_maintenance_is_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = project(tmp_path)
    grow(folder, 1)

    def failing(_lake: Lake, _expire: dt.datetime, _orphans: dt.datetime) -> None:
        raise StoreError("STORE_QUERY_FAILED", "Lake maintenance failed.", {})

    monkeypatch.setattr(Lake, "maintain", failing)
    out = io.StringIO()
    assert maintenance.maintain(folder, IDENTITY, out) == 1
    assert out.getvalue().startswith("STORE_QUERY_FAILED . lake maintenance: ")
    ((outcome, code, report),) = lake_rows(
        folder, "SELECT outcome, error_code, report FROM lake._meta.maintenance_runs"
    )
    assert (outcome, code, report) == ("failed", "STORE_QUERY_FAILED", None)
    assert lake_rows(folder, "SELECT count(*) FROM lake.raw.r") == [(1000,)]  # still readable


def test_records_validate_against_the_contract(tmp_path: Path) -> None:
    folder = project(tmp_path)
    grow(folder, 1)
    assert maintenance.maintain(folder, IDENTITY, io.StringIO()) == 0
    (row,) = lake_rows(
        folder,
        "SELECT run_id, trigger, retention, "
        "strftime(timezone('UTC', cutoff), '%Y-%m-%dT%H:%M:%S.%f+00:00'), versions, report, "
        "strftime(timezone('UTC', started_at), '%Y-%m-%dT%H:%M:%S.%f+00:00'), "
        "strftime(timezone('UTC', finished_at), '%Y-%m-%dT%H:%M:%S.%f+00:00'), outcome "
        "FROM lake._meta.maintenance_runs",
    )
    MaintenanceRecord.model_validate(
        {
            "run_id": row[0],
            "trigger": row[1],
            "retention": row[2],
            "cutoff": row[3],
            "versions": json.loads(str(row[4])),
            "report": json.loads(str(row[5])),
            "started_at": row[6],
            "finished_at": row[7],
            "outcome": row[8],
            "error": None,
        }
    )


def test_maintenance_waits_for_the_project_lock(tmp_path: Path) -> None:
    folder = project(tmp_path)
    grow(folder, 1)
    out = io.StringIO()
    with commands.project_lock(folder / f".{SLUG}"):
        assert maintenance.maintain(folder, IDENTITY, out) == 1
    assert out.getvalue().startswith(f"PROJECT_LOCKED .{SLUG}/lake.lock ")
    assert lake_rows(
        folder,
        "SELECT count(*) FROM duckdb_tables() WHERE database_name = 'lake' "
        "AND table_name = 'maintenance_runs'",
    ) == [(0,)]


def test_the_scheduler_maintains_when_nothing_is_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = project(tmp_path, retention="90d")
    grow(folder, 1)  # a lake to maintain; the model has no freshness: never due by itself
    start = dt.datetime(2026, 10, 28, 9, 0, tzinfo=dt.UTC)
    now = [start]
    failing = {"on": False}
    real = Lake.maintain

    def maintain(lake: Lake, expire: dt.datetime, orphans: dt.datetime) -> object:
        if failing["on"]:
            raise StoreError("STORE_QUERY_FAILED", "Lake maintenance failed.", {})
        return real(lake, expire, orphans)

    monkeypatch.setattr(Lake, "maintain", maintain)
    # A shorter day, so the scenario needs fewer cycles: every 2 hours, retried after 30 minutes.
    monkeypatch.setattr(maintenance, "INTERVAL", dt.timedelta(hours=2))
    monkeypatch.setattr(maintenance, "RETRY", dt.timedelta(minutes=30))
    monkeypatch.setattr(commands, "_now", lambda: now[0].isoformat(timespec="microseconds"))

    def sleep(seconds: float) -> None:
        now[0] += dt.timedelta(seconds=max(seconds, 1.0))
        # The second maintenance (an interval later) fails; after the retry delay it succeeds.
        failing["on"] = start + dt.timedelta(hours=2) <= now[0] < start + dt.timedelta(hours=2.5)

    out = io.StringIO()
    until = start + dt.timedelta(hours=3)
    assert (
        schedule(
            folder,
            IDENTITY,
            out,
            sleep=sleep,
            should_stop=lambda: now[0] >= until,
            monotonic=lambda: (now[0] - start).total_seconds(),
        )
        == 0
    )
    rows = lake_rows(
        folder,
        "SELECT strftime(timezone('UTC', started_at), '%d %H:%M'), outcome, trigger "
        "FROM lake._meta.maintenance_runs ORDER BY started_at",
    )
    starts = [(str(moment), str(outcome)) for moment, outcome, _ in rows]
    assert {trigger for _, _, trigger in rows} == {"schedule"}
    # At once, an interval later (fails), the retry delay after the failure: three, not more.
    assert [outcome for _, outcome in starts] == ["succeeded", "failed", "succeeded"]
    assert starts[0][0] == "28 09:00"
    assert starts[1][0] == "28 11:00"
    assert starts[2][0] == "28 11:30"
    assert out.getvalue().count("running lake maintenance") == 3


def test_a_due_model_runs_before_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = project(tmp_path, retention="90d")
    grow(folder, 1)  # the lake exists; no maintenance yet, so maintenance is due at once
    write(folder, {"models/raw/r.yaml": INCREMENTAL + "freshness: 10m\n"})
    # A day after that run: the model is stale, so both are due in the first cycle.
    start = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    now = [start]
    monkeypatch.setattr(commands, "_now", lambda: now[0].isoformat(timespec="microseconds"))

    def sleep(seconds: float) -> None:
        now[0] += dt.timedelta(seconds=max(seconds, 1.0))

    out = io.StringIO()
    until = start + dt.timedelta(minutes=3)
    assert (
        schedule(
            folder,
            IDENTITY,
            out,
            sleep=sleep,
            should_stop=lambda: now[0] >= until,
            monotonic=lambda: (now[0] - start).total_seconds(),
        )
        == 0
    )
    text = out.getvalue()
    assert "running raw.r (freshness)" in text, text
    assert "running lake maintenance" in text, text
    assert text.index("running raw.r (freshness)") < text.index("running lake maintenance")
    ((run_start, maintenance_start),) = lake_rows(
        folder,
        "SELECT (SELECT strftime(timezone('UTC', max(started_at)), '%Y-%m-%dT%H:%M:%S.%f') "
        "FROM lake._meta.run_records WHERE trigger = 'freshness'), "
        "(SELECT strftime(timezone('UTC', min(started_at)), '%Y-%m-%dT%H:%M:%S.%f') "
        "FROM lake._meta.maintenance_runs)",
    )
    assert run_start is not None
    assert maintenance_start is not None
    assert str(run_start) < str(maintenance_start)
