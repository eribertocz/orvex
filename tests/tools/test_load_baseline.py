"""#69: the load-performance baseline script, end to end on the `small` dataset (CI: it opens a
lake), and its pieces that need no lake."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from tools.benchmarks.load_baseline import (
    APPENDED,
    Step,
    append_day,
    attribution,
    command_for,
    markdown,
    project,
    sampled,
    summary,
)
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


FAKE = (
    "import sys, time\n"
    "print('built small', flush=True)\n"
    "time.sleep(0.6)\n"
    "print('built big', flush=True)\n"
    "block = bytearray(300 * 1024 * 1024)\n"
    "block[::4096] = b'x' * len(block[::4096])\n"
    "time.sleep(1.0)\n"
    "del block\n"
    "print('built after', flush=True)\n"
    "time.sleep(0.6)\n"
)


def test_the_peak_is_attributed_to_the_line_printed_before_it() -> None:
    # #168: a fake command allocates 300 MiB after printing "built big".
    run = sampled([sys.executable, "-c", FAKE])
    assert run.exit_code == 0
    assert [line for _, line in run.lines] == ["built small", "built big", "built after"]
    median, peak_at, after = attribution(run)
    assert after == "built big"
    assert run.peak_rss_bytes >= 300 * 1024 * 1024
    assert max(value for _, value in run.samples) >= 300 * 1024 * 1024
    assert 0 < median < run.peak_rss_bytes
    assert run.lines[1][0] <= peak_at < run.lines[2][0]


def test_the_memory_limit_is_set_only_by_the_experiment() -> None:
    # #168: no seam set, the lake's limit is unchanged; the experiment's limit reaches only the
    # traced process.
    from orvex.server import commands

    assert commands._limits().memory_limit == commands._memory_limit()
    plain = command_for(["run", "x"])
    assert plain[1] == "-c"
    assert plain[3:] == ["", "run", "x"]
    limited = command_for(["run", "x"], "4GB")
    assert limited[3:] == ["4GB", "run", "x"]
    for command, expected in ((plain, commands._memory_limit()), (limited, "4GB")):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                command[2].replace(
                    "from orvex.server.cli import main\nsys.exit(main(sys.argv[2:]))\n",
                    "print(commands._limits().memory_limit)\n",
                ),
                command[3],
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert probe.stdout.strip() == expected
