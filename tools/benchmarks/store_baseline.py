"""Time and memory of the store's representative operations, two versions compared (#246).

    python -m tools.benchmarks.store_baseline --baseline <python> --candidate <python> [--runs 5]

Each `<python>` is an interpreter whose `orvex.store` is the version to measure. The data is the
synthetic generator's (`small`, `--seed`), with one source of each kind made from it
(`store_equivalence.prepare`). Every measurement runs in a fresh process
(`python -I <this file> measure …`), the two versions alternating run by run on the same
machine, and reports the wall time of the operation alone and the peak resident memory of the
process. The report gives the medians of `--runs` runs and the candidate's difference to the
baseline. Operations, each on a fresh local lake:

- `open`: `open_lake` with no source;
- `attach`: `open_lake` with a Parquet, a CSV, a DuckDB and a SQLite source;
- `read`: a guarded aggregate read with typed parameters over 240,720 readings;
- `transaction`: one transaction creating a table from a source, landing a DuckDB source,
  updating and deleting rows.

Evidence for review, not a CI gate: no threshold is enforced here.
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from orvex.contracts.store.v1 import (
    FileSourceAttachment,
    LocalLakeLocation,
    Parameter,
    ReadRequest,
    SqliteSourceAttachment,
    StatementRequest,
)
from orvex.store.lake import Lake, Limits, open_lake

OPERATIONS = ("open", "attach", "read", "transaction")
LIMITS = Limits(
    memory_limit="1GB", threads=2, max_concurrent=4, queue_size=16, default_timeout_ms=600_000
)
TOTALS = (
    "SELECT count(*) AS n, sum(kwh) AS kwh, min(read_at) AS first, max(period) AS last "
    "FROM readings WHERE period >= $since AND kwh > $least"
)

# --- one measurement, in a process of its own -------------------------------------------------


def _sources(document: str) -> list[Any]:
    found: list[Any] = []
    for item in json.loads(document):
        kind = SqliteSourceAttachment if item["type"] == "sqlite" else FileSourceAttachment
        found.append(kind.model_validate(item))
    return found


def _statement(sql: str) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[])


def measure(operation: str, sources: list[Any], work: Path) -> dict[str, float]:
    """The wall time of `operation` alone, and this process's peak resident memory."""
    location = LocalLakeLocation(kind="lake_location", catalog="local", path=work.as_posix())
    lake: Lake | None = None
    started = time.perf_counter()
    if operation == "open":
        started = time.perf_counter()
        lake = open_lake(location, LIMITS)
        seconds = time.perf_counter() - started
    elif operation == "attach":
        started = time.perf_counter()
        lake = open_lake(location, LIMITS, sources)
        seconds = time.perf_counter() - started
    elif operation == "read":
        lake = open_lake(location, LIMITS, sources)
        with lake.transaction() as transaction:
            transaction.execute(
                _statement("CREATE TABLE readings AS SELECT * FROM readings_source.data")
            )
        request = ReadRequest(
            kind="read",
            sql=TOTALS,
            parameters=[
                Parameter.model_validate({"name": "since", "type": "DATE", "value": "2025-01-01"}),
                Parameter.model_validate(
                    {"name": "least", "type": "DECIMAL(12,3)", "value": "0.500"}
                ),
            ],
            snapshot=lake.current_snapshot().id,
            batch_size=65_536,
        )
        started = time.perf_counter()
        rows = lake.read(request).read_all()
        seconds = time.perf_counter() - started
        if rows.num_rows != 1:
            raise SystemExit("the aggregate read returned no row")
    elif operation == "transaction":
        lake = open_lake(location, LIMITS, sources)
        started = time.perf_counter()
        with lake.transaction() as transaction:
            transaction.execute(
                _statement("CREATE TABLE readings AS SELECT * FROM readings_source.data")
            )
            transaction.land("billing", "SELECT * FROM invoices", [], "main", "invoices")
            transaction.execute(_statement("UPDATE readings SET status = 'checked' WHERE kwh > 1"))
            transaction.execute(_statement("DELETE FROM invoices WHERE estimated"))
        seconds = time.perf_counter() - started
    else:
        raise SystemExit(f"unknown operation {operation}")
    lake.close()
    return {"seconds": seconds, "peak_mib": peak_mib()}


def peak_mib() -> float:
    """This process's peak resident memory (`VmHWM`). Not `ru_maxrss`: Linux keeps that value
    across `exec`, so a fresh process would report its parent's peak."""
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024  # kB
    raise SystemExit("no VmHWM in /proc/self/status (Linux only)")


# --- the orchestrator --------------------------------------------------------------------------


def run(python: str, operation: str, sources: Path, work: Path) -> dict[str, float]:
    shutil.rmtree(work, ignore_errors=True)
    done = subprocess.run(
        [python, "-I", Path(__file__).as_posix(), "measure", operation, str(sources), str(work)],
        check=True,
        capture_output=True,
        text=True,
    )
    shutil.rmtree(work, ignore_errors=True)
    measured: dict[str, float] = json.loads(done.stdout.strip().splitlines()[-1])
    return measured


def report(
    found: dict[str, dict[str, list[dict[str, float]]]], operations: Sequence[str] = OPERATIONS
) -> dict[str, object]:
    """Medians per operation and side, and the candidate's change in percent."""
    result: dict[str, object] = {}
    for operation in operations:
        row: dict[str, object] = {}
        for metric in ("seconds", "peak_mib"):
            medians = {
                side: statistics.median(m[metric] for m in found[side][operation])
                for side in ("baseline", "candidate")
            }
            change = 100 * (medians["candidate"] - medians["baseline"]) / medians["baseline"]
            row[metric] = {
                **{k: round(v, 4) for k, v in medians.items()},
                "change_%": round(change, 1),
            }
        result[operation] = row
    return result


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "measure":
        sources = _sources(Path(sys.argv[3]).read_text(encoding="utf-8"))
        print(json.dumps(measure(sys.argv[2], sources, Path(sys.argv[4]))))
        return 0
    from tools.benchmarks.store_equivalence import SEED, prepare
    from tools.benchmarks.store_equivalence import sources as source_list

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--baseline", required=True, help="interpreter with the baseline store")
    parser.add_argument("--candidate", required=True, help="interpreter with the candidate store")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=SEED)
    arguments = parser.parse_args()
    found: dict[str, dict[str, list[dict[str, float]]]] = {
        side: {operation: [] for operation in OPERATIONS} for side in ("baseline", "candidate")
    }
    with tempfile.TemporaryDirectory(prefix="store-baseline-") as temporary:
        root = Path(temporary)
        folder = prepare(root / "data", arguments.seed)
        listed = root / "sources.json"
        listed.write_text(
            json.dumps([s.model_dump(mode="json", by_alias=True) for s in source_list(folder)]),
            encoding="utf-8",
        )
        for _ in range(arguments.runs):
            for side in ("baseline", "candidate"):
                for operation in OPERATIONS:
                    python = getattr(arguments, side)
                    found[side][operation].append(run(python, operation, listed, root / "work"))
    print(
        json.dumps(
            {"runs": arguments.runs, "seed": arguments.seed, "medians": report(found)}, indent=1
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
