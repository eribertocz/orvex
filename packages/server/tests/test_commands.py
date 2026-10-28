"""P1-09: `validate` and `run` from the command line."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
from orvex.server.cli import main
from orvex.server.commands import project_lock
from orvex.server.identity import load_identity

REPOSITORY = Path(__file__).resolve().parents[3]
SLUG = load_identity().slug

PROJECT = "version: 1\nname: Test\ntime_zone: America/La_Paz\n"


def write(folder: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return folder


def sources(folder: Path) -> Path:
    """Parquet readings (exact decimals) and CSV tariffs, outside the project's lake."""
    data = folder / "data"
    data.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    connection.execute(
        f"COPY (SELECT * FROM (VALUES (1, 'C1', 12.3456::DECIMAL(18,4)), "
        f"(2, 'C1', 0.0001::DECIMAL(18,4)), (3, 'C2', -1.0000::DECIMAL(18,4)), "
        f"(4, 'C2', 7.5000::DECIMAL(18,4))) AS t(reading_id, consumer_id, kwh)) "
        f"TO '{data / 'readings.parquet'}'"
    )
    connection.execute(
        f"COPY (SELECT * FROM (VALUES ('C1', 'residential'), ('C2', 'commercial')) "
        f"AS t(consumer_id, tariff)) TO '{data / 'consumers.csv'}'"
    )
    connection.close()
    return data


CONNECTIONS = """\
version: 1
connections:
  archive:
    type: parquet
    path: data/readings.parquet
  registry:
    type: csv
    path: data/consumers.csv
"""

MODELS = {
    "models/staging/readings.sql": "SELECT reading_id, consumer_id, kwh FROM archive.data",
    "models/staging/consumers.sql": "SELECT consumer_id, tariff FROM registry.data",
    "models/staging/active_consumers.sql": (
        "SELECT r.consumer_id, sum(r.kwh) AS kwh FROM staging.readings AS r "
        "WHERE r.kwh > 0 GROUP BY r.consumer_id"
    ),
    "models/staging/active_consumers.yaml": "version: 1\nmaterialized: view\n",
    "models/marts/consumption.sql": (
        "SELECT a.consumer_id, c.tariff, a.kwh FROM staging.active_consumers AS a "
        "JOIN staging.consumers AS c USING (consumer_id)"
    ),
}


def full_project(folder: Path) -> Path:
    sources(folder)
    return write(folder, {"project.yaml": PROJECT, "connections.yaml": CONNECTIONS, **MODELS})


def command(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


# --- validate (no lake) ------------------------------------------------------------------------


def test_validate_reports_every_problem_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    full_project(tmp_path)
    write(
        tmp_path,
        {
            "models/c/a.sql": "SELECT * FROM c.b",
            "models/c/b.sql": "SELECT * FROM c.a",
            "models/s/unknown.sql": "SELECT *\nFROM nowhere.t",
            "models/s/itself.sql": "SELECT x + 1 AS x FROM s.itself",
            "models/s/orphan.yaml": "version: 1\n",
        },
    )
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    lines = out.splitlines()
    assert any(line.startswith("ORPHAN_MODEL_CONFIG models/s/orphan.yaml") for line in lines)
    assert any(line.startswith("MODEL_REFERENCE_UNKNOWN models/s/unknown.sql:2") for line in lines)
    assert any(line.startswith("DEPENDENCY_CYCLE models/c/a.sql") for line in lines)
    assert "c.a → c.b → c.a" in out
    assert "s.itself → s.itself" in out
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert after == before
    assert not (tmp_path / f".{SLUG}").exists()


def test_validate_valid_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    full_project(tmp_path)
    code, out = command(capsys, "validate", str(tmp_path))
    assert (code, out.strip()) == (0, "4 models, no problems.")


def test_validate_reports_project_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = command(capsys, "validate", str(tmp_path))
    assert code == 1
    assert out.startswith("PROJECT_NOT_FOUND project.yaml")


def test_incremental_model_is_refused_before_opening_the_lake(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    full_project(tmp_path)
    write(
        tmp_path,
        {
            "models/staging/readings.yaml": (
                "version: 1\nmaterialized: incremental\n"
                "incremental: {strategy: append, time_column: read_at}\n"
            )
        },
    )
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert out.startswith("UNSUPPORTED_MODEL_KIND models/staging/readings.yaml")
    assert "staging.readings" in out
    assert not (tmp_path / f".{SLUG}").exists()


def hold_lock(home: Path) -> subprocess.Popen[str]:
    """Another process holding the project's lake lock until it is killed."""
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from orvex.server.commands import project_lock\n"
        "with project_lock(Path(sys.argv[1])):\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(120)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(home)], stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout is not None
    assert holder.stdout.readline().strip() == "locked"
    return holder


def kill(holder: subprocess.Popen[str]) -> None:
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait()
    if holder.stdout is not None:
        holder.stdout.close()


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="needs SIGKILL")
def test_second_runner_gets_project_locked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    full_project(tmp_path)
    holder = hold_lock(tmp_path / f".{SLUG}")
    try:
        code, out = command(capsys, "run", str(tmp_path))
        assert code == 1
        assert out.startswith(f"PROJECT_LOCKED .{SLUG}/lake.lock")
    finally:
        kill(holder)
    # The lock died with its process.
    with project_lock(tmp_path / f".{SLUG}"):
        pass


# --- run (needs the lake: DuckLake extension) ----------------------------------------------------


def test_run_builds_models_in_order_all_or_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    full_project(tmp_path)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    assert "built staging.consumers: 2 rows" in out
    assert "built staging.readings: 4 rows" in out
    assert "built marts.consumption: 2 rows" in out
    assert "active_consumers" not in out
    lake = duckdb.connect()
    lake.execute("INSTALL ducklake")
    lake.execute("LOAD ducklake")
    catalog = tmp_path / f".{SLUG}" / "lake" / "catalog.ducklake"
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    tables = lake.execute(
        "SELECT schema_name || '.' || table_name FROM duckdb_tables() "
        "WHERE database_name = 'lake' AND schema_name <> '_meta' ORDER BY 1"
    ).fetchall()
    assert tables == [("marts.consumption",), ("staging.consumers",), ("staging.readings",)]
    built = lake.execute(
        "SELECT consumer_id, tariff, CAST(kwh AS VARCHAR) FROM lake.marts.consumption ORDER BY 1"
    ).fetchall()
    data = tmp_path / "data"
    expected = (
        duckdb.connect()
        .execute(
            "SELECT r.consumer_id, c.tariff, CAST(sum(r.kwh) AS VARCHAR) "
            f"FROM '{data / 'readings.parquet'}' AS r "
            f"JOIN '{data / 'consumers.csv'}' AS c USING (consumer_id) "
            "WHERE r.kwh > 0 GROUP BY r.consumer_id, c.tariff ORDER BY 1"
        )
        .fetchall()
    )
    assert built == expected == [("C1", "residential", "12.3457"), ("C2", "commercial", "7.5000")]
    snapshots_before = lake.execute("SELECT count(*) FROM ducklake_snapshots('lake')").fetchall()
    lake.close()

    # A model that fails at run time: no snapshot, earlier tables unchanged.
    write(tmp_path, {"models/marts/broken.sql": "SELECT CAST('x' AS INTEGER) AS n"})
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 1
    assert "marts.broken" in out
    lake = duckdb.connect()
    lake.execute("LOAD ducklake")
    lake.execute(f"ATTACH 'ducklake:{catalog}' AS lake (READ_ONLY)")
    # The failed run adds no data snapshot; only its run records commit (P1-10).
    assert lake.execute("SELECT count(*) FROM ducklake_snapshots('lake')").fetchall() == [
        (snapshots_before[0][0] + 1,)
    ]
    changes = lake.execute(
        "SELECT changes FROM ducklake_snapshots('lake') ORDER BY snapshot_id DESC LIMIT 1"
    ).fetchall()
    # Only inserts into the existing run-records table: no table created, replaced or dropped.
    assert set(changes[0][0]) <= {"inlined_insert", "tables_inserted_into"}
    assert lake.execute("SELECT count(*) FROM lake.staging.readings").fetchall() == [(4,)]
    lake.close()


def test_run_after_a_killed_runner(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    full_project(tmp_path)
    holder = hold_lock(tmp_path / f".{SLUG}")
    kill(holder)
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out


def test_synthetic_dataset_runs_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_folder = tmp_path / "data"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "tools.synthetic.generate",
            "--size",
            "small",
            "--out",
            str(out_folder),
        ],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
    )
    tables = ["consumers", "meters", "readings", "invoices", "payments", "credit_notes"]
    connections = "version: 1\nconnections:\n" + "".join(
        f"  src_{t}:\n    type: parquet\n    path: data/{t}.parquet\n" for t in tables
    )
    files = {f"models/raw/{t}.sql": f"SELECT * FROM src_{t}.data" for t in tables}
    write(tmp_path, {"project.yaml": PROJECT, "connections.yaml": connections, **files})
    code, out = command(capsys, "run", str(tmp_path))
    assert code == 0, out
    for table, rows in {
        "consumers": 10_000,
        "meters": 10_300,
        "readings": 240_720,
        "invoices": 240_000,
        "payments": 236_400,
        "credit_notes": 100,
    }.items():
        assert f"built raw.{table}: {rows} rows" in out
