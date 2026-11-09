"""Time and memory of the server's `validate` and `run`, two versions compared (#246).

    python -m tools.benchmarks.server_baseline --baseline <python> --candidate <python> [--runs 5]

Each `<python>` is an interpreter whose `orvex.server` is the version to measure. The project is
the differential test's (`server_equivalence.prepare`): the synthetic generator's data (`small`,
`--seed`) with a Parquet, a CSV and a DuckDB source, and six models (tables, a view, an
incremental model with the automatic filter, a model landed from the database source, tests
and a reconciliation check). Every measurement runs in a fresh process (`python -I <this file>
measure …`) on a fresh copy of the project, the two versions alternating run by run on the same
machine, and reports the wall time of the command alone and the peak resident memory of the
process. The report gives the medians of `--runs` runs and the candidate's difference to the
baseline. Operations:

- `validate`: check the project (its models bound in a scratch lake), no lake yet;
- `run`: the project's first run, creating its lake and building every model.

Evidence for review, not a CI gate: no threshold is enforced here.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OPERATIONS = ("validate", "run")

# --- one measurement, in a process of its own -------------------------------------------------


def measure(operation: str, template: Path, work: Path) -> dict[str, float]:
    """The wall time of `operation` alone, and this process's peak resident memory."""
    from orvex.server import commands
    from orvex.server.identity import load_identity

    project = work / "project"
    shutil.copytree(template, project)
    identity, out = load_identity(), io.StringIO()
    if operation == "validate":
        started = time.perf_counter()
        code = commands.validate(project, out)
        seconds = time.perf_counter() - started
    elif operation == "run":
        started = time.perf_counter()
        code = commands.run(project, identity, out)
        seconds = time.perf_counter() - started
    else:
        raise SystemExit(f"unknown operation {operation}")
    if code != 0:
        raise SystemExit(f"{operation} failed:\n{out.getvalue()}")
    return {"seconds": seconds, "peak_mib": peak_mib()}


def peak_mib() -> float:
    """This process's peak resident memory (`VmHWM`). Not `ru_maxrss`: Linux keeps that value
    across `exec`, so a fresh process would report its parent's peak."""
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024  # kB
    raise SystemExit("no VmHWM in /proc/self/status (Linux only)")


# --- the orchestrator --------------------------------------------------------------------------


def run(python: str, operation: str, template: Path, work: Path) -> dict[str, float]:
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    done = subprocess.run(
        [python, "-I", Path(__file__).as_posix(), "measure", operation, str(template), str(work)],
        check=True,
        capture_output=True,
        text=True,
    )
    shutil.rmtree(work, ignore_errors=True)
    measured: dict[str, float] = json.loads(done.stdout.strip().splitlines()[-1])
    return measured


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "measure":
        print(json.dumps(measure(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]))))
        return 0
    from tools.benchmarks.server_equivalence import SEED, prepare, server_location
    from tools.benchmarks.store_baseline import report

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--baseline", required=True, help="interpreter with the baseline server")
    parser.add_argument("--candidate", required=True, help="interpreter with the candidate server")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=SEED)
    arguments = parser.parse_args()
    loaded = {side: server_location(getattr(arguments, side)) for side in ("baseline", "candidate")}
    if loaded["baseline"] == loaded["candidate"]:
        parser.error(f"both interpreters load the same server: {loaded['baseline']}")
    found: dict[str, dict[str, list[dict[str, float]]]] = {
        side: {operation: [] for operation in OPERATIONS} for side in ("baseline", "candidate")
    }
    with tempfile.TemporaryDirectory(prefix="server-baseline-") as temporary:
        root = Path(temporary)
        template = prepare(root / "data", arguments.seed)
        for _ in range(arguments.runs):
            for side in ("baseline", "candidate"):
                for operation in OPERATIONS:
                    python = getattr(arguments, side)
                    found[side][operation].append(run(python, operation, template, root / "work"))
    medians = report(found, OPERATIONS)
    print(
        json.dumps(
            {"runs": arguments.runs, "seed": arguments.seed, "servers": loaded, "medians": medians},
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
