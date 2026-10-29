"""#109: a `partition_overwrite` run whose `$since` filter cuts a partition fails, writing nothing.

Every run goes through the command line and the lake, as a Builder would run it.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from orvex.server.cli import main
from orvex.server.identity import load_identity

SLUG = load_identity().slug
PROJECT = "version: 1\nname: Test\ntime_zone: UTC\n"
CONNECTIONS = "version: 1\nconnections:\n  feed:\n    type: parquet\n    path: data/feed.parquet\n"
SIDECAR = (
    "version: 1\nmaterialized: incremental\n"
    "incremental: {strategy: partition_overwrite, time_column: read_at, partition_by: [month], "
    "filter: manual}\n"
)
CUTTING = "SELECT *, strftime(read_at, '%Y-%m') AS month FROM feed.data WHERE read_at > $since"
WHOLE = (
    "SELECT *, strftime(read_at, '%Y-%m') AS month FROM feed.data "
    "WHERE read_at >= date_trunc('month', $since)"
)


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def project(folder: Path, model: str, sidecar: str = SIDECAR) -> None:
    write(
        folder,
        {
            "project.yaml": PROJECT,
            "connections.yaml": CONNECTIONS,
            "models/s/m.sql": model,
            "models/s/m.yaml": sidecar,
        },
    )


def feed(folder: Path, sql: str) -> None:
    """Replace the source with the rows `sql` selects."""
    (folder / "data").mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute(f"COPY ({sql}) TO '{folder / 'data' / 'feed.parquet'}'")
    connection.close()


def october(first: int, last: int, kind: str = "TIMESTAMPTZ") -> str:
    """One reading per day of October 2026, days `first` to `last`, at noon UTC."""
    return (
        f"SELECT i::BIGINT AS id, 'n' AS region, CAST(TIMESTAMPTZ '2026-10-01 12:00:00+00' + "
        f"INTERVAL (i - 1) DAY AS {kind}) AS read_at FROM range({first}, {last} + 1) AS r(i)"
    )


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


ROWS = "SELECT id, region, CAST(read_at AS VARCHAR), month FROM lake.s.m ORDER BY id"


def days_by_month(folder: Path) -> list[tuple[object, ...]]:
    return lake_value(
        folder,
        "SELECT month, count(*), min(id), max(id) FROM lake.s.m GROUP BY month ORDER BY month",
    )


def last_record(folder: Path) -> tuple[object, ...]:
    (row,) = lake_value(
        folder,
        "SELECT outcome, error_code, error_message FROM lake._meta.run_records "
        "WHERE model = 's.m' ORDER BY started_at DESC LIMIT 1",
    )
    return row


def test_a_cut_partition_fails_and_keeps_the_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, CUTTING)
    feed(tmp_path, october(1, 20))
    code, out = run(capsys, tmp_path)
    assert code == 0, out
    before = lake_value(tmp_path, ROWS)
    assert days_by_month(tmp_path) == [("2026-10", 20, 1, 20)]
    feed(tmp_path, october(1, 25))
    code, out = run(capsys, tmp_path)
    assert code == 1
    assert out.splitlines()[-1].startswith("PARTITION_INCOMPLETE ")
    assert "'2026-10' (1 in total)" in out
    outcome, error_code, message = last_record(tmp_path)
    assert (outcome, error_code) == ("failed", "PARTITION_INCOMPLETE")
    assert "'2026-10' (1 in total)" in str(message)
    # Nothing written: the table is exactly what the first run wrote.
    assert lake_value(tmp_path, ROWS) == before


def test_a_whole_partition_filter_loses_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, WHOLE)
    feed(tmp_path, october(1, 20))
    assert run(capsys, tmp_path)[0] == 0
    feed(tmp_path, october(1, 25))
    code, out = run(capsys, tmp_path)
    assert code == 0, out
    assert days_by_month(tmp_path) == [("2026-10", 25, 1, 25)]


def test_rows_of_a_new_partition_only_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, CUTTING)
    feed(tmp_path, october(1, 31))
    assert run(capsys, tmp_path)[0] == 0
    feed(tmp_path, october(1, 33))  # November 1 and 2
    code, out = run(capsys, tmp_path)
    assert code == 0, out
    assert days_by_month(tmp_path) == [("2026-10", 31, 1, 31), ("2026-11", 2, 32, 33)]


def test_a_cut_null_partition_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    by_region = SIDECAR.replace("[month]", "[region]")
    project(tmp_path, "SELECT * FROM feed.data WHERE read_at > $since", by_region)
    undated_region = october(1, 3).replace("'n' AS region", "CAST(NULL AS VARCHAR) AS region")
    feed(tmp_path, undated_region)
    assert run(capsys, tmp_path)[0] == 0
    feed(tmp_path, october(1, 5).replace("'n' AS region", "CAST(NULL AS VARCHAR) AS region"))
    code, out = run(capsys, tmp_path)
    assert code == 1
    assert "PARTITION_INCOMPLETE" in out
    assert ": NULL (1 in total)." in out
    assert lake_value(tmp_path, "SELECT count(*) FROM lake.s.m") == [(3,)]


def test_a_full_refresh_skips_the_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project(tmp_path, CUTTING)
    feed(tmp_path, october(1, 20))
    assert run(capsys, tmp_path)[0] == 0
    feed(tmp_path, october(1, 25))
    code, out = run(capsys, tmp_path, "--full-refresh")
    assert code == 0, out
    assert days_by_month(tmp_path) == [("2026-10", 25, 1, 25)]


@pytest.mark.parametrize("kind", ["DATE", "TIMESTAMP", "TIMESTAMPTZ"])
def test_every_temporal_time_column(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    project(tmp_path, CUTTING)
    feed(tmp_path, october(1, 20, kind))
    assert run(capsys, tmp_path)[0] == 0
    feed(tmp_path, october(1, 25, kind))
    code, out = run(capsys, tmp_path)
    assert code == 1, kind
    assert "PARTITION_INCOMPLETE" in out
    assert "'2026-10' (1 in total)" in out
    write(tmp_path, {"models/s/m.sql": WHOLE})
    code, out = run(capsys, tmp_path)
    assert code == 0, out
    assert days_by_month(tmp_path) == [("2026-10", 25, 1, 25)]


def test_the_message_names_five_partitions_and_the_total(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    by_region = SIDECAR.replace("[month]", "[region]")
    project(tmp_path, "SELECT * FROM feed.data WHERE read_at > $since", by_region)
    regions = "SELECT chr(CAST(97 + j AS INTEGER)) AS region FROM range(7) AS g(j)"
    seven = october(1, 3).replace("'n' AS region", "regions.region") + f", ({regions}) AS regions"
    feed(tmp_path, seven)
    assert run(capsys, tmp_path)[0] == 0
    feed(
        tmp_path,
        october(1, 5).replace("'n' AS region", "regions.region") + f", ({regions}) AS regions",
    )
    code, out = run(capsys, tmp_path)
    assert code == 1
    assert ": 'a', 'b', 'c', 'd', 'e' (7 in total)." in out


def test_a_source_that_dropped_old_rows_fails_until_a_full_refresh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The known limit (decisions #95 and #128): the check cannot tell a cut from a source that
    no longer has a partition's older rows; the run fails, and a full refresh rebuilds it."""
    project(tmp_path, WHOLE)
    feed(tmp_path, october(1, 20))
    assert run(capsys, tmp_path)[0] == 0
    feed(tmp_path, october(21, 25))  # the source dropped October 1 to 20
    code, out = run(capsys, tmp_path)
    assert code == 1
    assert "PARTITION_INCOMPLETE" in out
    assert "--full-refresh" in out
    assert days_by_month(tmp_path) == [("2026-10", 20, 1, 20)]
    code, out = run(capsys, tmp_path, "--full-refresh")
    assert code == 0, out
    assert days_by_month(tmp_path) == [("2026-10", 5, 21, 25)]


def test_a_partition_column_with_a_non_ascii_name_is_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # #130: names and values after non-ASCII text bind correctly in the check's statements.
    sidecar = SIDECAR.replace("[month]", '["año"]')
    model = (
        "SELECT *, 'mañana ' || strftime(read_at, '%Y-%m') AS \"año\" FROM feed.data "
        "WHERE read_at > $since"
    )
    project(tmp_path, model, sidecar)
    feed(tmp_path, october(1, 20))
    assert run(capsys, tmp_path)[0] == 0
    feed(tmp_path, october(1, 25))
    code, out = run(capsys, tmp_path)
    assert code == 1
    assert "PARTITION_INCOMPLETE" in out
    assert "'mañana 2026-10' (1 in total)" in out
    assert last_record(tmp_path)[0:2] == ("failed", "PARTITION_INCOMPLETE")


# --- #155: the oldest-row rule and the same time column (decision #128, option B) ------------


@pytest.mark.parametrize(
    "model",
    [
        CUTTING.replace("read_at > $since", "read_at >= $since"),
        CUTTING.replace("read_at > $since", "read_at > $since - INTERVAL 3 DAY"),
    ],
    ids=["at-or-after-since", "hand-written-lookback"],
)
def test_a_partial_reload_fails_and_keeps_the_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], model: str
) -> None:
    # The #128 measurements: under the #95 rule these runs passed, reloading 6 and 8 rows of
    # October and deleting the rest of its 20.
    project(tmp_path, model)
    feed(tmp_path, october(1, 20))
    assert run(capsys, tmp_path)[0] == 0
    before = lake_value(tmp_path, ROWS)
    feed(tmp_path, october(1, 25))
    code, out = run(capsys, tmp_path)
    assert code == 1, out
    assert "PARTITION_INCOMPLETE" in out
    assert "older than the oldest row this run loaded" in out
    assert lake_value(tmp_path, ROWS) == before  # all 20 rows kept, nothing written
    outcome, error_code, _ = last_record(tmp_path)
    assert (outcome, error_code) == ("failed", "PARTITION_INCOMPLETE")


def test_a_source_that_dropped_its_oldest_row_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, WHOLE)
    feed(tmp_path, october(1, 20))
    assert run(capsys, tmp_path)[0] == 0
    feed(tmp_path, october(2, 25))  # the source no longer has October 1
    code, out = run(capsys, tmp_path)
    assert code == 1, out
    assert "PARTITION_INCOMPLETE" in out
    assert "--full-refresh" in out
    code, out = run(capsys, tmp_path, "--full-refresh")
    assert code == 0, out
    assert days_by_month(tmp_path) == [("2026-10", 24, 2, 25)]


BY_DAY = SIDECAR.replace("time_column: read_at", "time_column: day")
FINER = (
    "SELECT *, CAST(read_at AS DATE) AS day, strftime(read_at, '%Y-%m') AS month "
    "FROM feed.data WHERE read_at > $since"
)


def test_a_filter_on_a_finer_column_is_refused_before_anything_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The #128 case of 72 rows becoming 67: the filter compares read_at, the time column is day.
    project(tmp_path, FINER, BY_DAY)
    feed(tmp_path, october(1, 20))
    code = main(["validate", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 1, out
    assert "INVALID_MODEL_SQL" in out
    assert "time_column day" in out
    code, out = run(capsys, tmp_path)
    assert code == 1, out
    assert "INVALID_MODEL_SQL" in out


def test_the_same_model_filtering_its_time_column_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project(tmp_path, FINER.replace("WHERE read_at > $since", "WHERE day >= $since"), BY_DAY)
    feed(tmp_path, october(1, 20))
    code, out = run(capsys, tmp_path)
    assert code == 0, out
