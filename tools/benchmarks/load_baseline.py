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
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
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
    # The resident-memory curve, sampled every SAMPLE_SECONDS (#168): its median, when its peak
    # came (seconds from the start), and the last line the command printed before the peak.
    rss_median_bytes: int = 0
    peak_at_seconds: float = 0.0
    peak_after_line: str = ""
    memory_limit: str = ""  # the lake's memory limit when the experiment set one (#168)
    catalog: dict[str, int] | None = None  # the lake's files after the step (#168)


SAMPLE_SECONDS = 0.25
# Runs the real command, printing a line as each run step starts so a memory peak can be placed
# (#168), and with the lake's memory limit set for this process only when an experiment asks
# (an empty limit leaves it as is). A seam of the benchmark: not reachable from the command
# line, the API or the project files.
TRACED = (
    "import sys\n"
    "import orvex.server.commands as commands\n"
    "limit = sys.argv[1]\n"
    "if limit:\n"
    "    commands._memory_limit = lambda: limit\n"
    "step_of = commands._run_step\n"
    "def traced(transaction, step, item, connections):\n"
    "    text = str(getattr(getattr(step, 'request', step), 'sql', ''))[:60]\n"
    "    kind = type(step).__name__\n"
    "    print(f'step {item.model.name} {kind} {text}'.replace(chr(10), ' '), flush=True)\n"
    "    return step_of(transaction, step, item, connections)\n"
    "commands._run_step = traced\n"
    "from orvex.server.cli import main\n"
    "sys.exit(main(sys.argv[2:]))\n"
)


@dataclass(frozen=True, slots=True)
class Sampled:
    exit_code: int
    seconds: float
    peak_rss_bytes: int  # the process's own maximum resident set (`wait4`)
    lines: list[tuple[float, str]]  # each printed line, with seconds from the start
    samples: list[tuple[float, int]]  # resident memory (bytes), with seconds from the start


def _resident(pid: int) -> int | None:
    """The process's resident memory now (`VmRSS`), or None once it has gone."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def sampled(command: list[str]) -> Sampled:
    """Run `command`, timestamping each line it prints and sampling its resident memory."""
    started = time.monotonic()
    # Unbuffered, so each line arrives when it is printed and the peak can be placed among them.
    environment = {**os.environ, "PYTHONUNBUFFERED": "1"}
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment
    )
    assert process.stdout is not None
    stream = process.stdout
    lines: list[tuple[float, str]] = []

    def read() -> None:
        for line in stream:
            lines.append((time.monotonic() - started, line.rstrip("\n")))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    samples: list[tuple[float, int]] = []
    while True:
        finished = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if finished is not None:
            break
        resident = _resident(process.pid)
        if resident is not None:
            samples.append((time.monotonic() - started, resident))
        time.sleep(SAMPLE_SECONDS)
    _, status, usage = os.wait4(process.pid, 0)
    seconds = time.monotonic() - started
    process.returncode = os.waitstatus_to_exitcode(status)
    reader.join()
    stream.close()
    return Sampled(
        exit_code=process.returncode,
        seconds=seconds,
        peak_rss_bytes=usage.ru_maxrss * 1024,  # kilobytes on Linux
        lines=lines,
        samples=samples,
    )


def attribution(run: Sampled) -> tuple[int, float, str]:
    """The memory curve's median, when its highest sample came, and the last line printed
    before it ("" when nothing was printed yet)."""
    if not run.samples:
        return 0, 0.0, ""
    peak_at, _ = max(run.samples, key=lambda sample: sample[1])
    before = [line for moment, line in run.lines if moment <= peak_at]
    median = int(statistics.median(value for _, value in run.samples))
    return median, round(peak_at, 2), before[-1] if before else ""


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


def command_for(arguments: list[str], memory_limit: str = "") -> list[str]:
    """The real command, in its own process, traced; with the experiment's memory limit when
    one is set."""
    return [sys.executable, "-c", TRACED, memory_limit, *arguments]


def measure(name: str, arguments: list[str], lake: Path, memory_limit: str = "") -> Step:
    """Run one command in its own process; its wall time, its own peak memory, and when that
    peak came (#168)."""
    run = sampled(command_for(arguments, memory_limit))
    median, peak_at, after = attribution(run)
    return Step(
        name=name,
        exit_code=run.exit_code,
        seconds=round(run.seconds, 3),
        peak_rss_bytes=run.peak_rss_bytes,
        lake_bytes=size(lake) if lake.exists() else 0,
        output_tail=(
            "" if run.exit_code == 0 else "\n".join(line for _, line in run.lines[-OUTPUT_LINES:])
        ),
        rss_median_bytes=median,
        peak_at_seconds=peak_at,
        peak_after_line=after,
        memory_limit=memory_limit,
        catalog=catalog_files(lake),
    )


def catalog_files(lake: Path) -> dict[str, int] | None:
    """What the default DuckDB catalog lists, against what is on disk (#168): the bytes of the
    data and delete files the current snapshot uses, of every file the catalog lists, of the
    files scheduled for deletion, and of the data folder. None without a catalog."""
    catalog = lake / "lake" / "catalog.ducklake"
    if not catalog.exists():
        return None
    connection = duckdb.connect()
    try:
        connection.execute(
            f"ATTACH '{str(catalog).replace(chr(39), chr(39) * 2)}' AS c (READ_ONLY)"
        )
        row = connection.execute(
            "SELECT "
            "(SELECT coalesce(sum(file_size_bytes), 0) FROM c.ducklake_data_file "
            " WHERE end_snapshot IS NULL) + "
            "(SELECT coalesce(sum(file_size_bytes), 0) FROM c.ducklake_delete_file "
            " WHERE end_snapshot IS NULL), "
            "(SELECT coalesce(sum(file_size_bytes), 0) FROM c.ducklake_data_file) + "
            "(SELECT coalesce(sum(file_size_bytes), 0) FROM c.ducklake_delete_file), "
            "(SELECT count(*) FROM c.ducklake_files_scheduled_for_deletion), "
            "(SELECT count(*) FROM c.ducklake_data_file WHERE end_snapshot IS NULL), "
            "(SELECT count(*) FROM c.ducklake_data_file), "
            "(SELECT count(*) FROM c.ducklake_snapshot)"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    data = lake / "lake" / "data"
    return {
        "current_snapshot_bytes": int(row[0]),
        "listed_bytes": int(row[1]),
        "scheduled_for_deletion": int(row[2]),
        "current_data_files": int(row[3]),
        "listed_data_files": int(row[4]),
        "snapshots": int(row[5]),
        "on_disk_bytes": size(data) if data.exists() else 0,
    }


def notice(step: Step) -> None:
    """The step as a GitHub annotation of its own, when run in GitHub Actions: each stays under
    the annotation's size limit, which the whole results file may not (#168)."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    compact = {key: value for key, value in asdict(step).items() if key != "output_tail"}
    text = json.dumps(compact, separators=(",", ":"))
    print(f"::notice title=load-baseline step::{text}", flush=True)


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
    peaks = [
        f"- {step['name']}: peak {step['peak_rss_bytes'] / (1024 * 1024):.0f} MiB, highest sample "
        f"at {step.get('peak_at_seconds', 0):.1f} s, after `{step.get('peak_after_line', '')}`"
        + (f" (memory limit {step['memory_limit']})" if step.get("memory_limit") else "")
        for step in steps
        if isinstance(step, dict) and not step["exit_code"]
    ]
    if peaks:
        rows += ["", "Peaks (#168):", "", *peaks]
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
    parser.add_argument(
        "--memory-limit",
        action="append",
        default=[],
        metavar="STEP=LIMIT",
        help="an experiment (#168): run STEP (full, incremental, maintenance) with the lake's "
        "memory limit at LIMIT (for example incremental=4GB); repeatable",
    )
    arguments = parser.parse_args()
    limits: dict[str, str] = {}
    for item in arguments.memory_limit:
        step, _, limit = item.partition("=")
        if step not in {"full", "incremental", "maintenance"} or not limit:
            print(f"--memory-limit {item!r}: write STEP=LIMIT", file=sys.stderr)
            return 2
        limits[step] = limit
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
        first = measure("full first load", ["run", str(folder)], lake, limits.get("full", ""))
        steps.append(first)
        print(json.dumps(asdict(first)), flush=True)
        notice(first)
        if first.exit_code != 0:
            continue  # nothing to add to or maintain: recorded as failed
        appended = append_day(data)
        for name, command, key in (
            ("incremental cycle", "run", "incremental"),
            ("maintenance", "maintain", "maintenance"),
        ):
            step = measure(name, [command, str(folder)], lake, limits.get(key, ""))
            steps.append(step)
            print(json.dumps(asdict(step)), flush=True)
            notice(step)
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
