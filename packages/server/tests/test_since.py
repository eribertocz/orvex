"""P1-14: `$since`, watermarks, `lookback` and `--full-refresh`."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import StatementRequest
from orvex.server import commands
from orvex.server.cli import main
from orvex.server.identity import load_identity

SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test\ntime_zone: America/La_Paz\n"
CONNECTIONS = "version: 1\nconnections:\n  feed:\n    type: parquet\n    path: data/feed.parquet\n"
MODEL = "SELECT id, kwh, read_at FROM feed.data WHERE read_at > $since"
APPEND = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: append, time_column: read_at, filter: manual}\n"
)
MERGE = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: merge, time_column: read_at, unique_key: [id], filter: manual}\n"
)


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def project(folder: Path, sidecar: str, model: str = MODEL) -> None:
    write(
        folder,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": model,
            "models/s/m.yaml": sidecar,
        },
    )


def feed(folder: Path, *rows: tuple[int, str, str]) -> None:
    """Replace the source rows: (id, kwh, read_at as TIMESTAMPTZ text)."""
    (folder / "data").mkdir(parents=True, exist_ok=True)
    values = ", ".join(f"({i}, '{kwh}', '{at}')" for i, kwh, at in rows)
    duckdb.connect().execute(
        "COPY (SELECT id::BIGINT AS id, kwh::DECIMAL(18,4) AS kwh, read_at::TIMESTAMPTZ AS read_at "
        f"FROM (VALUES {values}) AS t(id, kwh, read_at)) TO '{folder / 'data' / 'feed.parquet'}'"
    )


def command(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def lake_value(folder: Path, sql: str) -> list[tuple[object, ...]]:
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    catalog = folder / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    result = lake.execute(sql).fetchall()
    lake.close()
    return result


def last_record(folder: Path) -> tuple[object, ...]:
    """(trigger, since, watermark, rows_written) of the last run of s.m."""
    (row,) = lake_value(
        folder,
        "SELECT trigger, since, watermark, CAST(rows_written AS VARCHAR) "
        "FROM lake._meta.run_records WHERE model = 's.m' ORDER BY started_at DESC LIMIT 1",
    )
    return row


def ids(folder: Path) -> list[object]:
    return [row[0] for row in lake_value(folder, "SELECT id FROM lake.s.m ORDER BY id")]


def test_only_rows_newer_than_the_watermark_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, APPEND)
    feed(
        tmp_path,
        (1, "1.0000", "2024-01-01 10:00:00+00"),
        (2, "2.0000", "2024-01-02 10:00:00.000001+00"),
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert last_record(tmp_path) == (
        "manual",
        "-infinity",
        "2024-01-02T10:00:00.000001+00:00",
        "2",
    )
    # 3 newer rows and 2 older ones (one exactly at the watermark, which is not newer).
    feed(
        tmp_path,
        (3, "3.0000", "2024-01-03 10:00:00+00"),
        (4, "4.0000", "2024-01-05 08:30:00+00"),
        (5, "5.0000", "2024-01-04 10:00:00+00"),
        (6, "6.0000", "2024-01-01 12:00:00+00"),
        (7, "7.0000", "2024-01-02 10:00:00.000001+00"),
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "built s.m: 3 rows" in out
    assert ids(tmp_path) == [1, 2, 3, 4, 5]
    assert last_record(tmp_path) == (
        "manual",
        "2024-01-02T10:00:00.000001+00:00",
        "2024-01-05T08:30:00.000000+00:00",
        "3",
    )


def test_a_run_loading_no_rows_keeps_the_watermark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, APPEND)
    feed(tmp_path, (1, "1.0000", "2024-01-01 10:00:00+00"))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    feed(tmp_path, (2, "2.0000", "2023-12-31 10:00:00+00"))  # older: nothing loads
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert last_record(tmp_path)[2:] == ("2024-01-01T10:00:00.000000+00:00", "0")
    # And the next run still starts from it.
    feed(tmp_path, (3, "3.0000", "2024-01-01 10:00:01+00"))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    assert last_record(tmp_path)[1] == "2024-01-01T10:00:00.000000+00:00"
    assert ids(tmp_path) == [1, 3]


def test_lookback_re_reads_late_rows_and_merges_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, MERGE.replace("filter: manual", "filter: manual, lookback: 1h"))
    feed(
        tmp_path,
        (1, "1.0000", "2024-01-01 09:00:00+00"),
        (2, "2.0000", "2024-01-01 12:00:00+00"),
    )
    assert command(capsys, "run", str(tmp_path))[0] == 0
    feed(
        tmp_path,
        (2, "2.0000", "2024-01-01 12:00:00+00"),  # re-read inside the window: replaces itself
        (3, "3.0000", "2024-01-01 11:30:00+00"),  # 30 minutes older than the watermark: loads
        (4, "4.0000", "2024-01-01 10:00:00+00"),  # 2 hours older: outside the window
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert last_record(tmp_path)[1:] == (
        "2024-01-01T11:00:00.000000+00:00",
        "2024-01-01T12:00:00.000000+00:00",
        "2",
    )
    assert ids(tmp_path) == [1, 2, 3]


def test_the_watermark_comes_from_the_data_not_the_clock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "_now", lambda: "2000-01-01T00:00:00.000000+00:00")
    project(tmp_path, APPEND)
    feed(tmp_path, (1, "1.0000", "2099-06-30 23:59:59.5+00"))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    assert last_record(tmp_path)[2] == "2099-06-30T23:59:59.500000+00:00"


@pytest.mark.parametrize(
    ("cast", "watermark"),
    [
        ("CAST(read_at AS DATE)", "2024-01-02"),
        ("CAST(read_at AS TIMESTAMP)", "2024-01-02T10:00:00.250000"),
        ("read_at", "2024-01-02T10:00:00.250000+00:00"),
    ],
)
def test_every_temporal_time_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], cast: str, watermark: str
) -> None:
    model = f"SELECT id, {cast} AS read_at FROM feed.data WHERE {cast} > $since"
    project(tmp_path, APPEND, model)
    feed(
        tmp_path,
        (1, "1.0000", "2024-01-01 10:00:00+00"),
        (2, "2.0000", "2024-01-02 10:00:00.25+00"),
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert last_record(tmp_path)[1:3] == ("-infinity", watermark)
    feed(tmp_path, (3, "3.0000", "2024-01-03 10:00:00+00"))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    assert last_record(tmp_path)[1] == watermark
    assert ids(tmp_path) == [1, 2, 3]


def test_full_refresh_rebuilds_from_every_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, APPEND)
    feed(tmp_path, (1, "1.0000", "2024-01-01 10:00:00+00"), (2, "2.0000", "2024-01-02 10:00:00+00"))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    # The source was corrected: row 2 is gone and an old row appeared.
    feed(tmp_path, (1, "1.5000", "2024-01-01 10:00:00+00"), (9, "9.0000", "2023-06-01 10:00:00+00"))
    code, out = command(capsys, "run", str(tmp_path), "--full-refresh")
    assert code == 0, out
    assert lake_value(tmp_path, "SELECT id, CAST(kwh AS VARCHAR) FROM lake.s.m ORDER BY id") == [
        (1, "1.5000"),
        (9, "9.0000"),
    ]
    assert last_record(tmp_path) == (
        "full_refresh",
        "-infinity",
        "2024-01-01T10:00:00.000000+00:00",
        "2",
    )
    code, out = command(capsys, "run", str(tmp_path), "--full-refresh", "s.nothing")
    assert code == 1
    assert out.startswith("MODEL_REFERENCE_UNKNOWN . --full-refresh s.nothing")


def test_since_rules_are_checked_before_anything_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, APPEND, "SELECT id, kwh, read_at FROM feed.data")
    code, out = command(capsys, "validate", str(tmp_path))
    assert (code, out.split(" ")[:2]) == (1, ["MISSING_SINCE_PARAMETER", "models/s/m.sql"])
    project(
        tmp_path,
        APPEND.replace(", filter: manual", ""),
        "SELECT id, kwh, read_at FROM feed.data",
    )
    code, out = command(capsys, "validate", str(tmp_path))
    assert (code, out.split(" ")[:2]) == (1, ["INCREMENTAL_FILTER_NOT_PROVABLE", "models/s/m.yaml"])
    assert not (tmp_path / f".{SLUG}").exists()


def broken_records(*_: object) -> list[StatementRequest]:
    """Run-record statements that fail: the records transaction rolls back."""
    return [StatementRequest(kind="statement", sql="INSERT INTO nowhere VALUES (1)", parameters=[])]


@pytest.mark.parametrize("first_run_fails", [True, False])
def test_a_failed_record_write_never_reloads_rows(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    first_run_fails: bool,
) -> None:
    """The watermark is stored with the rows, not in the run records written afterwards."""
    project(tmp_path, APPEND)
    feed(tmp_path, (1, "1.0000", "2024-01-01 10:00:00+00"))
    if not first_run_fails:
        assert command(capsys, "run", str(tmp_path))[0] == 0
        feed(
            tmp_path,
            (1, "1.0000", "2024-01-01 10:00:00+00"),
            (2, "2.0000", "2024-01-02 10:00:00+00"),
        )
    with monkeypatch.context() as patch:
        patch.setattr(commands, "record_statements", broken_records)
        code, out = command(capsys, "run", str(tmp_path))
    assert code == 1  # the rows committed; the records could not be written
    assert "run records were not written" in out
    expected = [1] if first_run_fails else [1, 2]
    assert ids(tmp_path) == expected
    code, out = command(capsys, "run", str(tmp_path))  # same source again
    assert code == 0, out
    assert "built s.m: 0 rows" in out
    assert ids(tmp_path) == expected


def test_watermarks_progress_with_a_frozen_clock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(commands, "_now", lambda: "2000-01-01T00:00:00.000000+00:00")
    project(tmp_path, APPEND)
    rows: list[tuple[int, str, str]] = []
    for day in range(1, 5):
        rows.append((day, f"{day}.0000", f"2024-01-0{day} 10:00:00+00"))
        feed(tmp_path, *rows)  # the source grows by one row each run
        code, out = command(capsys, "run", str(tmp_path))
        assert code == 0, out
        assert "built s.m: 1 rows" in out
    assert ids(tmp_path) == [1, 2, 3, 4]
    (stored,) = lake_value(tmp_path, "SELECT watermark FROM lake._meta.watermarks")
    assert stored == ("2024-01-04T10:00:00.000000+00:00",)


def test_a_table_without_a_stored_watermark_continues_from_its_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, APPEND)
    feed(tmp_path, (1, "1.0000", "2024-01-01 10:00:00+00"))
    assert command(capsys, "run", str(tmp_path))[0] == 0
    # A table written before watermarks were stored: no stored row.
    lake = duckdb.connect()
    lake.execute("LOAD ducklake")
    catalog = tmp_path / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake")
    lake.execute("DELETE FROM lake._meta.watermarks")
    lake.close()
    feed(tmp_path, (1, "1.0000", "2024-01-01 10:00:00+00"), (2, "2.0000", "2024-01-02 10:00:00+00"))
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert last_record(tmp_path)[1] == "2024-01-01T10:00:00.000000+00:00"
    assert ids(tmp_path) == [1, 2]
