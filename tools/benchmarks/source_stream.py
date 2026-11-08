"""Source isolation cost (#372 §5): landing one database-source table three ways.

    python -m tools.benchmarks.source_stream --rows 10000000 --rows 50000000

- `direct`: the source attached to the reading DuckDB instance and copied with
  `CREATE TABLE … AS`, the path a lake used before #372 (no isolation);
- `stream`: a second DuckDB instance in the same process, its result streamed as Arrow
  batches into the reading instance (in-process isolation, rejected by ADR-0031 §6);
- `worker`: the product's path, `source_worker.start`, an Arrow IPC stream from a worker
  process (ADR-0031).

Each path runs `--runs` times per size (default 3) on the same synthetic table (BIGINT,
DECIMAL(18,2), TIMESTAMPTZ, VARCHAR); the report gives the median wall time, the rows per
second and the peak resident memory of this (the reading) process. Every run checks the
landed row count and the exact sum of the decimal column against the source's. Nothing is
estimated: a failed check fails the benchmark.
"""

from __future__ import annotations

import argparse
import json
import platform
import resource
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import duckdb
from orvex.contracts.store.v1 import FileSourceAttachment
from orvex.store import source_worker

MEMORY = "4GB"
THREADS = 4
BATCH = 65_536


def build(path: Path, rows: int) -> tuple[int, str]:
    """The synthetic source table, and its row count and exact sum, computed by DuckDB."""
    with duckdb.connect(str(path)) as writer:
        writer.sql(
            "CREATE TABLE readings AS SELECT range AS id, "
            "CAST((range % 100000) / 100 AS DECIMAL(18,2)) AS amount, "
            "TIMESTAMPTZ '2026-01-01 00:00:00+00' + to_seconds(range % 31536000) AS at, "
            f"'meter-' || CAST(range % 1000 AS VARCHAR) AS meter FROM range({int(rows)})"
        )
        row = writer.execute(
            "SELECT count(*), CAST(sum(amount) AS VARCHAR) FROM readings"
        ).fetchone()
    assert row is not None
    return int(row[0]), str(row[1])


def target() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(config={"memory_limit": MEMORY, "threads": THREADS})


def check(connection: duckdb.DuckDBPyConnection, expected: tuple[int, str]) -> None:
    row = connection.execute("SELECT count(*), CAST(sum(amount) AS VARCHAR) FROM landed").fetchone()
    if row is None or (int(row[0]), str(row[1])) != expected:
        raise SystemExit(f"wrong landing: {row} != {expected}")


def direct(path: Path, expected: tuple[int, str]) -> None:
    with target() as connection:
        connection.sql(f"ATTACH '{path}' AS source (READ_ONLY)")
        connection.sql("CREATE TABLE landed AS SELECT * FROM source.readings")
        check(connection, expected)


def stream(path: Path, expected: tuple[int, str]) -> None:
    with duckdb.connect(config={"memory_limit": MEMORY, "threads": THREADS}) as source:
        source.sql("SET arrow_lossless_conversion = true")
        source.sql(f"ATTACH '{path}' AS source (READ_ONLY)")
        reader = source.sql("SELECT * FROM source.readings").to_arrow_reader(BATCH)
        with target() as connection:
            connection.register("incoming", reader)
            connection.sql("CREATE TABLE landed AS SELECT * FROM incoming")
            check(connection, expected)


def worker(path: Path, expected: tuple[int, str]) -> None:
    attachment = FileSourceAttachment(
        kind="source_attachment", name="source", type="duckdb", path=str(path)
    )
    started = source_worker.start(
        attachment,
        "",
        "SELECT * FROM source.readings",
        [],
        memory_limit=MEMORY,
        threads=THREADS,
        timeout_ms=3_600_000,
        batch_size=BATCH,
        folder=path.parent,
    ).open()
    try:
        with target() as connection:
            connection.register("incoming", started.reader())
            connection.sql("CREATE TABLE landed AS SELECT * FROM incoming")
            check(connection, expected)
    finally:
        started.close()


PATHS: dict[str, Callable[[Path, tuple[int, str]], None]] = {
    "direct": direct,
    "stream": stream,
    "worker": worker,
}


def peak_mib() -> float:
    """Peak resident memory of this process (the reading side), in MiB. A worker's own peak is
    not reported: a spawned child's `ru_maxrss` counts the parent's pages from before `exec`."""
    scale = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale / 2**20, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, action="append", required=True)
    parser.add_argument("--runs", type=int, default=3)
    arguments = parser.parse_args()
    report: dict[str, object] = {
        "python": platform.python_version(),
        "duckdb": duckdb.__version__,
        "machine": platform.platform(),
        "cpus": len(__import__("os").sched_getaffinity(0)),
        "memory_limit": MEMORY,
        "threads": THREADS,
        "batch_size": BATCH,
        "results": [],
    }
    results: list[dict[str, object]] = []
    for rows in arguments.rows:
        with tempfile.TemporaryDirectory(prefix="source-stream-") as folder:
            path = Path(folder) / "source.duckdb"
            expected = build(path, rows)
            for name, land in PATHS.items():
                times = []
                for _ in range(arguments.runs):
                    started = time.perf_counter()
                    land(path, expected)
                    times.append(time.perf_counter() - started)
                median = statistics.median(times)
                own = peak_mib()
                results.append(
                    {
                        "rows": rows,
                        "path": name,
                        "seconds": [round(t, 3) for t in times],
                        "median_s": round(median, 3),
                        "rows_per_s": round(rows / median),
                        "peak_rss_mib": own,
                    }
                )
                print(json.dumps(results[-1]), flush=True)
    report["results"] = results
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
