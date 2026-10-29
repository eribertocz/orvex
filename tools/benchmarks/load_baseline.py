"""P1 load-performance baseline (#69): load a synthetic dataset into a lake and measure it.

Builds a project of models over a dataset written by `tools.synthetic.generate`, then, for each
of `--runs` rounds, on a fresh lake:

1. the full first load (`run`): wall time, peak resident memory, lake size on disk;
2. one incremental cycle (`run`) after one more day of readings is appended to the source;
3. one maintenance run (`maintain`).

Each step runs the real command in its own process; its peak memory is that process's own
(`os.wait4`). The numbers are written as JSON (and a Markdown table) with the commit, the DuckDB
and DuckLake versions and the machine. Nothing is estimated: a step that fails is recorded as
failed, with its exit code and the end of its output.

    python -m tools.benchmarks.load_baseline --data data/large --work /tmp/baseline --out out
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
import orvex.server.cli as cli
from orvex.server.identity import load_identity

# The source tables the project reads, and the column each incremental model is cut on.
TABLES = ("consumers", "meters", "readings", "invoices", "payments")
INCREMENTAL = {"readings": "read_at", "invoices": "issued_at", "payments": "paid_at"}
APPENDED = "readings_appended.parquet"  # the extra day of readings, removed after each round
OUTPUT_LINES = 40  # how much of a failed step's output is kept

PROJECT = "version: 1\nname: Load baseline\ntime_zone: America/La_Paz\n"

MODELS = {
    "models/staging/consumers.sql": "SELECT * FROM consumers.data",
    "models/staging/meters.sql": "SELECT * FROM meters.data",
    **{
        f"models/staging/{table}.sql": f"SELECT * FROM {table}.data WHERE {column} > $since"
        for table, column in INCREMENTAL.items()
    },
    **{
        f"models/staging/{table}.yaml": (
            "version: 1\nmaterialized: incremental\n"
            f"incremental: {{strategy: append, time_column: {column}, filter: manual}}\n"
        )
        for table, column in INCREMENTAL.items()
    },
    # Energy per zone and month: readings joined to their meter and consumer.
    "models/marts/zone_monthly_energy.sql": (
        "SELECT c.zone_id, c.tariff, r.period, sum(r.kwh) AS kwh, count(*) AS readings, "
        "count(r.kwh) AS measured FROM staging.readings AS r "
        "JOIN staging.meters AS m USING (meter_id) "
        "JOIN staging.consumers AS c USING (consumer_id) "
        "WHERE r.status <> 'voided' GROUP BY ALL"
    ),
    # Billed and collected per month and tariff.
    "models/marts/monthly_collections.sql": (
        "WITH paid AS (SELECT invoice_id, sum(amount) AS paid FROM staging.payments "
        "GROUP BY invoice_id) "
        "SELECT i.period, c.tariff, sum(i.amount) AS billed, "
        "sum(coalesce(p.paid, 0)) AS collected, count(*) AS invoices "
        "FROM staging.invoices AS i JOIN staging.consumers AS c USING (consumer_id) "
        "LEFT JOIN paid AS p USING (invoice_id) WHERE i.status = 'issued' GROUP BY ALL"
    ),
}


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    exit_code: int
    seconds: float
    peak_rss_bytes: int
    lake_bytes: int
    output_tail: str  # empty when the step succeeded


def project(folder: Path, data: Path) -> None:
    """Write the benchmark project: one source per table, the models above."""
    connections = ["version: 1", "connections:"]
    for table in TABLES:
        # The readings glob also matches the appended day, when it is there.
        name = f"{table}*.parquet" if table == "readings" else f"{table}.parquet"
        connections += [f"  {table}:", "    type: parquet", f"    path: {data / name}"]
    files = {
        "project.yaml": PROJECT,
        "connections.yaml": "\n".join(connections) + "\n",
        **MODELS,
    }
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def append_day(data: Path) -> int:
    """Append one more day of readings: the readings of the last period's first reading day
    (in the project's zone), one month later. Returns the number of rows appended."""
    source = str(data / "readings.parquet").replace("'", "''")
    target = str(data / APPENDED).replace("'", "''")
    connection = duckdb.connect()
    try:
        connection.execute("SET TimeZone = 'America/La_Paz'")
        last = connection.execute(f"SELECT max(period) FROM read_parquet('{source}')").fetchone()
        assert last is not None
        day = connection.execute(
            f"SELECT min(CAST(read_at AS DATE)) FROM read_parquet('{source}') "
            f"WHERE period = DATE '{last[0]}'"
        ).fetchone()
        assert day is not None
        connection.execute(
            f"COPY (SELECT regexp_replace(reading_id, '-[0-9]{{6}}$', "
            f"strftime(period + INTERVAL 1 MONTH, '-%Y%m')) AS reading_id, meter_id, "
            f"CAST(period + INTERVAL 1 MONTH AS DATE) AS period, "
            f"read_at + INTERVAL 1 MONTH AS read_at, kwh, status "
            f"FROM read_parquet('{source}') WHERE period = DATE '{last[0]}' "
            f"AND CAST(read_at AS DATE) = DATE '{day[0]}') TO '{target}' (FORMAT parquet)"
        )
        row = connection.execute(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def size(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())


def measure(name: str, arguments: list[str], lake: Path) -> Step:
    """Run one command in its own process; its wall time and its own peak memory."""
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, "-m", cli.__name__.rpartition(".")[0], *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    lines = process.stdout.read().splitlines()
    _, status, usage = os.wait4(process.pid, 0)
    seconds = time.monotonic() - started
    process.returncode = os.waitstatus_to_exitcode(status)
    code = process.returncode
    return Step(
        name=name,
        exit_code=code,
        seconds=round(seconds, 3),
        peak_rss_bytes=usage.ru_maxrss * 1024,  # kilobytes on Linux
        lake_bytes=size(lake) if lake.exists() else 0,
        output_tail="" if code == 0 else "\n".join(lines[-OUTPUT_LINES:]),
    )


def versions() -> dict[str, str]:
    connection = duckdb.connect()
    try:
        connection.execute("INSTALL ducklake")
        connection.execute("LOAD ducklake")
        row = connection.execute(
            "SELECT extension_version FROM duckdb_extensions() WHERE extension_name = 'ducklake'"
        ).fetchone()
    finally:
        connection.close()
    return {"duckdb": duckdb.__version__, "ducklake": str(row[0]) if row else "unknown"}


def machine() -> dict[str, object]:
    memory = 0
    with open("/proc/meminfo", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("MemTotal:"):
                memory = int(line.split()[1]) * 1024
    return {
        "cpus": os.cpu_count(),
        "memory_bytes": memory,
        "system": platform.platform(),
        "python": platform.python_version(),
        "runner": os.environ.get("RUNNER_NAME", ""),
        "runner_image": os.environ.get("RUNNER_OS", ""),
    }


def summary(steps: list[Step]) -> dict[str, dict[str, float | int]]:
    """Median and maximum of each measure, per step name, over the successful rounds."""
    found: dict[str, dict[str, float | int]] = {}
    for name in dict.fromkeys(step.name for step in steps):
        done = [step for step in steps if step.name == name and step.exit_code == 0]
        if not done:
            continue
        found[name] = {"rounds": len(done)}
        for measure_name in ("seconds", "peak_rss_bytes", "lake_bytes"):
            values = [getattr(step, measure_name) for step in done]
            found[name][f"{measure_name}_median"] = statistics.median(values)
            found[name][f"{measure_name}_max"] = max(values)
    return found


def markdown(result: dict[str, object]) -> str:
    rows = [
        "| Step | Rounds | Wall time (s), median / max | Peak memory (MiB), median / max "
        "| Lake size (MiB), median / max |",
        "|---|---|---|---|---|",
    ]
    totals = result["summary"]
    assert isinstance(totals, dict)
    for name, values in totals.items():
        mib = 1024 * 1024
        rows.append(
            f"| {name} | {values['rounds']} | {values['seconds_median']:.1f} / "
            f"{values['seconds_max']:.1f} | {values['peak_rss_bytes_median'] / mib:.0f} / "
            f"{values['peak_rss_bytes_max'] / mib:.0f} | {values['lake_bytes_median'] / mib:.0f}"
            f" / {values['lake_bytes_max'] / mib:.0f} |"
        )
    steps = result["steps"]
    assert isinstance(steps, list)
    failed = [step for step in steps if isinstance(step, dict) and step["exit_code"]]
    if failed:
        rows += ["", "Failed steps:", ""]
        rows += [f"- {step['name']}: exit {step['exit_code']}" for step in failed]
    return "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="a generated dataset folder")
    parser.add_argument("--work", type=Path, required=True, help="where the project is built")
    parser.add_argument("--out", type=Path, required=True, help="where the results are written")
    parser.add_argument("--runs", type=int, default=3)
    arguments = parser.parse_args()
    if arguments.runs < 1:
        print("--runs must be at least 1", file=sys.stderr)
        return 2
    data = arguments.data.resolve()
    folder = arguments.work.resolve() / "project"
    lake = folder / f".{load_identity().slug}"
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    steps: list[Step] = []
    appended = 0
    for _ in range(arguments.runs):
        shutil.rmtree(folder, ignore_errors=True)
        (data / APPENDED).unlink(missing_ok=True)
        project(folder, data)
        first = measure("full first load", ["run", str(folder)], lake)
        steps.append(first)
        print(json.dumps(asdict(first)), flush=True)
        if first.exit_code != 0:
            continue  # nothing to add to or maintain: recorded as failed
        appended = append_day(data)
        for name, command in (("incremental cycle", "run"), ("maintenance", "maintain")):
            step = measure(name, [command, str(folder)], lake)
            steps.append(step)
            print(json.dumps(asdict(step)), flush=True)
    (data / APPENDED).unlink(missing_ok=True)
    result: dict[str, object] = {
        "commit": os.environ.get("GITHUB_SHA", ""),
        "run_url": (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
            if "GITHUB_RUN_ID" in os.environ
            else ""
        ),
        "dataset": {key: manifest[key] for key in ("size", "seed", "rows") if key in manifest},
        "appended_readings": appended,
        "versions": versions(),
        "machine": machine(),
        "models": sorted(path for path in MODELS if path.endswith(".sql")),
        "steps": [asdict(step) for step in steps],
        "summary": summary(steps),
    }
    arguments.out.mkdir(parents=True, exist_ok=True)
    (arguments.out / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    (arguments.out / "results.md").write_text(markdown(result))
    print(markdown(result))
    return 0 if all(step.exit_code == 0 for step in steps) else 1


if __name__ == "__main__":
    sys.exit(main())
