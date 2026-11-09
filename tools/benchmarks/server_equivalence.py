"""Differential test of the server's commands (#246, equivalence item 2): one fixed set of
`validate`, `run`, `runs` and lineage operations run by two versions of the server's code on the
same synthetic project, their observations compared.

    python -m tools.benchmarks.server_equivalence --baseline <python> --candidate <python>

Each `<python>` is an interpreter whose `orvex.server` is the version to run (a virtual
environment with that checkout installed). The project is made once, here, from the synthetic
generator (`tools.synthetic`, size `small`, `--seed`): a Parquet, a CSV and a DuckDB source, and
models of every kind (tables, a view, an incremental model with the automatic filter, a model
landed from the database source, tests and a reconciliation check), in a Git repository with
fixed dates. Each version runs the scenario in a process of its own (`python -I <this file>
scenario …`) on its own copy of the project, with a local lineage collector, and writes what it
observed as JSON: every command's exit code and printed lines, and the lake's persisted state
(its tables with their columns, rows and a checksum of their content, the current snapshot, the
run records and watermarks, the files under the project's home, the lineage files and the
events the collector received). Paths, ports, run ids and today's times are written as
placeholders, so only what the commands decide is compared. Exit 0 when both are identical.

The operations: `validate` (no problem, `--show-sql`, an unknown model, a broken model); `runs`
before any run, while locked and at the end; `run` (the first, a second with nothing new, a full
refresh of every incremental model and of an unknown one, a scheduled partial selection, a
selection asking to drop leftovers, a run while the project is locked, a run cancelled before
it starts, a failing test, a failing statement, an unreachable source, a leftover kept, then
dropped); and `evidence` without a semantic model.
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from orvex.contracts.store.v1 import ReadRequest
from orvex.server import commands, lake_place
from orvex.server.identity import load_identity
from orvex.server.project import load_project
from orvex.store.lake import Lake

SEED = 42
GIT_DATE = "2025-01-01T00:00:00+00:00"
PROJECT = "version: 1\nname: Equivalence\ntime_zone: America/La_Paz\n"
CONNECTIONS = (
    "version: 1\nconnections:\n"
    "  readings:\n    type: parquet\n    path: data/readings.parquet\n"
    "  registry:\n    type: csv\n    path: data/consumers.csv\n"
    "  billing:\n    type: duckdb\n    path: data/billing.duckdb\n    read_only: true\n"
)
TESTS = "tests:\n  - accepted_values: {column: status, values: [%s]}\n"
MODELS = {
    "models/staging/readings.sql": (
        "SELECT reading_id, meter_id, period, read_at, kwh, status FROM readings.data"
    ),
    "models/staging/readings.yaml": (
        "version: 1\n"
        + TESTS % "valid, estimated, voided"
        + "reconciliation:\n  - {name: rows, measure: count(*), source: readings.data}\n"
    ),
    "models/staging/monthly.sql": (
        "SELECT period, count(*) AS n, sum(kwh) AS kwh FROM staging.readings GROUP BY period"
    ),
    "models/staging/recent.sql": "SELECT reading_id, read_at, kwh FROM readings.data",
    "models/staging/recent.yaml": (
        "version: 1\nmaterialized: incremental\n"
        "incremental: {strategy: append, time_column: read_at}\n"
    ),
    "models/marts/active.sql": "SELECT period, n FROM staging.monthly WHERE n > 0",
    "models/marts/active.yaml": "version: 1\nmaterialized: view\n",
    "models/marts/consumers.sql": "SELECT consumer_id, status FROM registry.data",
    "models/marts/invoices.sql": "SELECT * FROM billing.invoices",
}

# --- the project, made once by the orchestrator --------------------------------------------


def git(folder: Path, *arguments: str) -> None:
    dated = {"GIT_AUTHOR_DATE": GIT_DATE, "GIT_COMMITTER_DATE": GIT_DATE}
    subprocess.run(
        ["git", "-c", "user.name=equivalence", "-c", "user.email=e@example.invalid", *arguments],
        cwd=folder,
        check=True,
        capture_output=True,
        env={**os.environ, **dated},
    )


def prepare(folder: Path, seed: int = SEED) -> Path:
    """The synthetic project (`small`, `seed`), committed with fixed dates: the same commit id
    on every machine."""
    from tools.benchmarks.store_equivalence import prepare as store_data

    made = store_data(folder / "made", seed)
    project = folder / "project"
    data = project / "data"
    data.mkdir(parents=True)
    shutil.copy(made / "synthetic" / "readings.parquet", data / "readings.parquet")
    shutil.copy(made / "sources" / "consumers.csv", data / "consumers.csv")
    shutil.copy(made / "sources" / "billing.duckdb", data / "billing.duckdb")
    shutil.rmtree(made)
    write(project, {"project.yaml": PROJECT, "connections.yaml": CONNECTIONS, **MODELS})
    git(project, "init", "-q", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "project")
    return project


def write(folder: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


# --- the scenario, run by each version -----------------------------------------------------


class Collector(http.server.BaseHTTPRequestHandler):
    """A lineage collector on this machine: every event it receives, in order."""

    received: list[object] = []  # noqa: RUF012 - one collector per scenario process

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        Collector.received.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return None


class Observer:
    """Every command's exit code and printed lines, in order."""

    def __init__(self) -> None:
        self.steps: list[dict[str, object]] = []

    def step(self, name: str, work: Callable[[io.StringIO], object]) -> None:
        out = io.StringIO()
        try:
            value = work(out)
        except Exception as error:  # noqa: BLE001 - any other outcome is an observation too
            value = {"raised": [type(error).__name__, str(error)]}
        self.steps.append({"step": name, "value": value, "out": out.getvalue().splitlines()})


def cell(value: object) -> str:
    """A value as compared: times in ISO form (so a run's own moments become placeholders)."""
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return repr(value)


def table_state(lake: Lake, schema: str, table: str) -> dict[str, object]:
    """A table's columns and row count; its rows when small or run metadata (`_meta`), else a
    checksum of its content."""
    relation = f'"{schema}"."{table}"'

    def rows(sql: str) -> list[dict[str, str]]:
        request = ReadRequest(
            kind="read",
            sql=sql,
            parameters=[],
            snapshot=lake.current_snapshot().id,
            batch_size=65_536,
        )
        found: list[dict[str, object]] = lake.read(request).read_all().to_pylist()
        return [{key: cell(value) for key, value in row.items()} for row in found]

    count = rows(f"SELECT count(*) AS n FROM {relation}")[0]["n"]
    state: dict[str, object] = {"columns": lake.describe(f"SELECT * FROM {relation}"), "n": count}
    if schema == "_meta" or int(count) <= 200:
        # Sorted once normalized (`sorted_rows`): a run id or a time decides no order.
        state["rows"] = rows(f"SELECT * FROM {relation}")
    else:
        sql = f"SELECT bit_xor(hash(t)) AS checksum FROM {relation} AS t"
        state["checksum"] = rows(sql)[0]["checksum"]
    return state


def sorted_rows(observed: dict[str, Any]) -> dict[str, Any]:
    for state in observed["persisted"]["tables"].values():
        if "rows" in state:
            state["rows"].sort(key=lambda row: json.dumps(row, sort_keys=True))
    return observed


def persisted(project: Path) -> dict[str, object]:
    """The lake's tables, snapshot and records, and every file under the project's home."""
    home = project / f".{load_identity().slug}"
    lake = commands.open_existing(home, commands.lake_location(load_project(project), home))
    try:
        tables = {
            f"{schema}.{table}": table_state(lake, schema, table)
            for schema, table in sorted(lake.tables())
        }
        snapshot = lake.current_snapshot().id
    finally:
        lake.close()
    files: dict[str, int] = {}
    for path in sorted(home.rglob("*")):
        if path.is_file():
            folder = path.parent.relative_to(home).as_posix()
            files[folder] = files.get(folder, 0) + 1
    lineage = sorted(
        path.read_text(encoding="utf-8").splitlines()
        for path in (home / commands.LINEAGE_FOLDER).glob("*.ndjson")
    )
    record = (home / lake_place.RECORD_FILE).read_text(encoding="utf-8")
    return {
        "tables": tables,
        "snapshot": snapshot,
        "files per folder": files,
        "lineage files": lineage,
        "lake record": record,
    }


def operations(observe: Observer, project: Path) -> None:
    identity = load_identity()
    home = project / f".{identity.slug}"
    names = tuple(sorted(f"{p.parent.name}.{p.stem}" for p in project.glob("models/*/*.sql")))

    def run(
        full_refresh: Sequence[str] | None = None,
        selection: commands.Selection | None = None,
        drop_leftovers: bool = False,
    ) -> Callable[[io.StringIO], object]:
        return lambda out: commands.run(
            project, identity, out, full_refresh, selection, drop_leftovers
        )

    def runs(out: io.StringIO) -> object:
        return commands.runs(project, identity, 20, out)

    def locked(work: Callable[[io.StringIO], object]) -> Callable[[io.StringIO], object]:
        def held(out: io.StringIO) -> object:
            with commands.project_lock(home):
                return work(out)

        return held

    def cancelled(out: io.StringIO) -> object:
        control = commands.RunControl()
        control.cancel()
        selection = commands.Selection(names, "freshness", control=control)
        return commands.run(project, identity, out, selection=selection)

    observe.step("validate", lambda out: commands.validate(project, out))
    observe.step(
        "validate --show-sql", lambda out: commands.validate(project, out, "staging.recent")
    )
    observe.step("validate --show-sql unknown", lambda out: commands.validate(project, out, "x.y"))
    observe.step("runs before any run", runs)
    observe.step("run", run())
    observe.step("runs", runs)
    observe.step("run, nothing new", run())
    observe.step("run --full-refresh unknown", run(full_refresh=["nowhere.t"]))
    observe.step("run --full-refresh", run(full_refresh=[]))
    selected = commands.Selection(("staging.monthly",), "schedule", frozenset({"staging.recent"}))
    observe.step("run, scheduled selection", run(selection=selected))
    observe.step("run, selection drops", run(selection=selected, drop_leftovers=True))
    observe.step("run, locked", locked(run()))
    observe.step("runs, locked", locked(runs))
    observe.step("run, cancelled", cancelled)
    write(project, {"models/staging/readings.yaml": "version: 1\n" + TESTS % "valid"})
    observe.step("run, failing test", run())
    write(project, {"models/staging/readings.yaml": MODELS["models/staging/readings.yaml"]})
    write(project, {"models/marts/broken.sql": "SELECT CAST(status AS INTEGER) AS s FROM t.x"})
    observe.step("validate, broken model", lambda out: commands.validate(project, out))
    write(
        project,
        {"models/marts/broken.sql": "SELECT CAST(status AS INTEGER) AS s FROM staging.readings"},
    )
    observe.step("run, failing statement", run())
    (project / "models/marts/broken.sql").unlink()
    (project / "data/consumers.csv").rename(project / "data/consumers.away")
    observe.step("run, unreachable source", run())
    (project / "data/consumers.away").rename(project / "data/consumers.csv")
    (project / "models/marts/consumers.sql").unlink()
    observe.step("run, leftover kept", run())
    observe.step("run --drop-leftovers", run(drop_leftovers=True))
    observe.step(
        "evidence, no semantic model", lambda out: commands.evidence(project, identity, out)
    )
    observe.step("runs, at the end", runs)


def scenario(template: Path, work: Path, out: Path) -> None:
    project = work / "project"
    shutil.copytree(template, project)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    endpoint = f"127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = f"{PROJECT}lineage:\n  url: http://{endpoint}/api/v1/lineage\n"
    write(project, {"project.yaml": settings})
    observe = Observer()
    try:
        operations(observe, project)
        observed: dict[str, object] = {
            "commands": observe.steps,
            "persisted": persisted(project),
            "collector": Collector.received,
        }
    finally:
        server.shutdown()
    text = normalized(json.dumps(observed, sort_keys=True, default=repr), work, endpoint)
    out.write_text(json.dumps(sorted_rows(json.loads(text)), indent=1), encoding="utf-8")


def normalized(text: str, work: Path, endpoint: str | None) -> str:
    """Placeholders for what differs between two runs and is not the commands' decision."""
    text = text.replace(work.as_posix(), "<work>")
    if endpoint is not None:
        text = text.replace(endpoint, "<endpoint>")
    text = re.sub(r"/tmp/[A-Za-z0-9_.-]+", "/tmp/<tmp>", text)
    text = re.sub(
        r"(bind-|lake-spill-|lake-workers-|lake-sources-|source-)[A-Za-z0-9_]+", r"\1<tmp>", text
    )
    text = re.sub(
        r"(?=[0-9]*[a-f])[0-9a-f]{32}|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", "<id>", text
    )
    # The moments a run started, finished or committed (today's, UTC or the project's zone):
    # when it ran, not what it decided.
    today = dt.datetime.now(dt.UTC).date()
    days = "|".join((today + dt.timedelta(days=d)).isoformat() for d in (-1, 0, 1))
    return re.sub(
        rf"(?:{days})[ T][0-9:]{{8}}(?:\.[0-9]+)?(?:Z|[+-][0-9:]{{2,5}})?", "<time>", text
    )


# --- the orchestrator ----------------------------------------------------------------------


def server_location(python: str) -> str:
    """The folder of the `orvex.server` that `python` loads, so each side's version is on
    record."""
    found = subprocess.run(
        [python, "-I", "-c", "import orvex.server as s, os; print(os.path.dirname(s.__file__))"],
        check=True,
        capture_output=True,
        text=True,
    )
    return found.stdout.strip()


def run(python: str, template: Path, work: Path, out: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    subprocess.run(
        [python, "-I", Path(__file__).as_posix(), "scenario", str(template), str(work), str(out)],
        check=True,
    )
    shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "scenario":
        scenario(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        return 0
    from tools.benchmarks.store_equivalence import differences

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--baseline", required=True, help="interpreter with the baseline server")
    parser.add_argument("--candidate", required=True, help="interpreter with the candidate server")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--keep", type=Path, help="folder for both observations (JSON)")
    arguments = parser.parse_args()
    loaded = {side: server_location(getattr(arguments, side)) for side in ("baseline", "candidate")}
    if loaded["baseline"] == loaded["candidate"]:
        parser.error(f"both interpreters load the same server: {loaded['baseline']}")
    with tempfile.TemporaryDirectory(prefix="server-equivalence-") as temporary:
        root = Path(temporary)
        template = prepare(root / "data", arguments.seed)
        kept = arguments.keep or root
        kept.mkdir(parents=True, exist_ok=True)
        observed = {}
        for side in ("baseline", "candidate"):
            out = kept / f"{side}.json"
            run(getattr(arguments, side), template, root / "work", out)
            observed[side] = json.loads(out.read_text(encoding="utf-8"))
    found = differences(observed["baseline"], observed["candidate"])
    steps = observed["candidate"]["commands"]
    print(
        json.dumps(
            {
                "servers": loaded,
                "commands": len(steps),
                "failed commands": sum(1 for s in steps if s["value"] != 0),
                "tables": len(observed["candidate"]["persisted"]["tables"]),
                "collector events": len(observed["candidate"]["collector"]),
                "differences": len(found),
            }
        )
    )
    for line in found[:50]:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
