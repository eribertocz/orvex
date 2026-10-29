"""#69: the load-performance baseline script, end to end on the `small` dataset (CI: it opens a
lake), and its pieces that need no lake."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from tools.benchmarks.load_baseline import APPENDED, Step, append_day, markdown, project, summary
from tools.synthetic.generate import generate

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("small")
    generate("small", 42, out)
    return out


def test_one_more_day_of_readings_is_appended_after_the_last_period(dataset: Path) -> None:
    try:
        appended = append_day(dataset)
        connection = duckdb.connect()
        connection.execute("SET TimeZone = 'America/La_Paz'")
        path = str(dataset / APPENDED)
        row = connection.execute(
            f"SELECT count(*), min(period), max(period), count(DISTINCT CAST(read_at AS DATE)), "
            f"count(*) FILTER (WHERE reading_id NOT LIKE '%-202610') FROM read_parquet('{path}')"
        ).fetchone()
        connection.close()
    finally:
        (dataset / APPENDED).unlink(missing_ok=True)
    assert row is not None
    count, first, last, days, other_ids = row
    assert count == appended > 0
    assert str(first) == str(last) == "2026-10-01"  # one period after the dataset's last one
    assert days == 1  # one day of readings
    assert other_ids == 0  # new reading ids, not duplicates of the last period's


def test_the_project_reads_every_table_and_the_appended_day(dataset: Path, tmp_path: Path) -> None:
    project(tmp_path, dataset)
    connections = (tmp_path / "connections.yaml").read_text(encoding="utf-8")
    assert f"path: {dataset / 'readings*.parquet'}" in connections
    assert sorted(path.name for path in (tmp_path / "models").rglob("*.sql")) == [
        "consumers.sql",
        "invoices.sql",
        "meters.sql",
        "monthly_collections.sql",
        "payments.sql",
        "readings.sql",
        "zone_monthly_energy.sql",
    ]


def test_the_summary_keeps_only_successful_rounds() -> None:
    steps = [
        Step("full first load", 0, 10.0, 100, 1000, ""),
        Step("full first load", 0, 30.0, 300, 1000, ""),
        Step("full first load", 0, 20.0, 200, 1000, ""),
        Step("incremental cycle", 1, 1.0, 1, 1, "STORE_TIMEOUT"),
    ]
    found = summary(steps)
    assert found == {
        "full first load": {
            "rounds": 3,
            "seconds_median": 20.0,
            "seconds_max": 30.0,
            "peak_rss_bytes_median": 200,
            "peak_rss_bytes_max": 300,
            "lake_bytes_median": 1000,
            "lake_bytes_max": 1000,
        }
    }
    text = markdown({"summary": found, "steps": [{"name": "x", "exit_code": 1}]})
    assert "| full first load | 3 | 20.0 / 30.0 |" in text
    assert "- x: exit 1" in text


def test_the_baseline_runs_end_to_end_on_the_small_dataset(dataset: Path, tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.benchmarks.load_baseline",
            "--data",
            str(dataset),
            "--work",
            str(tmp_path / "work"),
            "--out",
            str(tmp_path / "out"),
            "--runs",
            "1",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads((tmp_path / "out" / "results.json").read_text(encoding="utf-8"))
    assert [step["name"] for step in result["steps"]] == [
        "full first load",
        "incremental cycle",
        "maintenance",
    ]
    assert all(step["exit_code"] == 0 for step in result["steps"])
    assert result["steps"][0]["lake_bytes"] > 0
    assert result["steps"][0]["peak_rss_bytes"] > 0
    assert result["appended_readings"] > 0
    assert result["versions"]["duckdb"] == duckdb.__version__
    assert not (dataset / APPENDED).exists()  # the source is left as generated
    # The incremental cycle added exactly the appended day to the readings.
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    catalog = next((tmp_path / "work" / "project").glob(".*/lake/catalog.ducklake"))
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    loaded = lake.execute("SELECT count(*) FROM lake.staging.readings").fetchone()
    lake.close()
    source = duckdb.connect()
    generated = source.execute(
        f"SELECT count(*) FROM read_parquet('{dataset / 'readings.parquet'}')"
    ).fetchone()
    source.close()
    assert loaded is not None
    assert generated is not None
    assert loaded[0] == generated[0] + result["appended_readings"]
