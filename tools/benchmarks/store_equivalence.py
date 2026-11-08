"""Differential test of the store (#246, equivalence item 2): one fixed set of operations run by
two versions of the store's code on the same synthetic data, their observations compared.

    python -m tools.benchmarks.store_equivalence --baseline <python> --candidate <python>

Each `<python>` is an interpreter whose `orvex.store` is the version to run (a virtual
environment with that checkout installed); the data and the source files are made once, here,
from the synthetic generator (`tools.synthetic`, size `small`, `--seed`). Each version runs the
scenario in a process of its own (`python -I <this file> scenario …`), in the same work folder,
and writes what it observed as JSON: returned values and Arrow schemas (field metadata
included), error codes, messages and details, and the lake's persisted state (its tables and
columns, every snapshot id, file statistics, and the data files per table folder, on disk or in
object storage). Paths, ports, commit times and random names are written as placeholders, so
only what the store decides is compared. Exit 0 when both observations are identical.

The operations, on every catalog and storage kind available on one machine (a DuckDB-file
catalog, a SQLite catalog, an encrypted lake, data on S3 through a local `moto` server): open
the lake with one source of each file and database kind (Parquet, CSV, a DuckDB file, a SQLite
file), land and load into it, guarded reads with typed parameters, every kind of sandbox and
guard refusal, committed, rolled-back, refused and timed-out transactions, interrupted reads
(time limit and cancel), the catalog questions, maintenance, and a reopen. A PostgreSQL catalog
or source needs a server: not run here, the CI suite covers it. A store lake keeps no run
history (that is the server's, #246 PR 3).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import duckdb
import pyarrow as pa
from orvex.contracts.store.v1 import (
    ChangesRequest,
    FileSourceAttachment,
    LocalLakeLocation,
    ObjectStorage,
    Parameter,
    ReadRequest,
    S3Storage,
    SqliteLakeLocation,
    SqliteSourceAttachment,
    StatementRequest,
    StatisticsRequest,
    TablesRequest,
)
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, open_lake

SEED = 42
LIMITS = Limits(
    memory_limit="1GB", threads=2, max_concurrent=4, queue_size=16, default_timeout_ms=600_000
)
KEY_ENV, SECRET_ENV = "STORE_EQUIVALENCE_S3_KEY", "STORE_EQUIVALENCE_S3_SECRET"
SLOW = "SELECT sum(a.range * b.range) AS s FROM range(200000) AS a, range(200000) AS b"
TOTALS = (
    "SELECT count(*) AS n, sum(kwh) AS kwh, min(read_at) AS first, max(period) AS last "
    "FROM readings WHERE period >= $since AND kwh > $least"
)

# --- the data, made once by the orchestrator --------------------------------------------------


def prepare(folder: Path, seed: int = SEED) -> Path:
    """The synthetic data (`small`, `seed`) and one source file of each kind made from it."""
    from tools.synthetic.generate import generate

    data = folder / "synthetic"
    generate("small", seed, data)
    sources = folder / "sources"
    sources.mkdir()
    with duckdb.connect() as connection:
        consumers = (data / "consumers.parquet").as_posix()
        connection.execute(
            f"COPY (SELECT * FROM '{consumers}' ORDER BY consumer_id) "
            f"TO '{(sources / 'consumers.csv').as_posix()}' (HEADER)"
        )
        invoices = (data / "invoices.parquet").as_posix()
        connection.execute(f"ATTACH '{(sources / 'billing.duckdb').as_posix()}' AS billing")
        connection.execute(f"CREATE TABLE billing.invoices AS SELECT * FROM '{invoices}'")
        connection.execute("DETACH billing")
        meters = (data / "meters.parquet").as_posix()
        connection.execute("INSTALL sqlite")
        connection.execute("LOAD sqlite")
        connection.execute(
            f"ATTACH '{(sources / 'field.sqlite').as_posix()}' AS field (TYPE sqlite)"
        )
        connection.execute(
            "CREATE TABLE field.meters AS SELECT meter_id, consumer_id, "
            f"CAST(installed_on AS VARCHAR) AS installed_on FROM '{meters}'"
        )
        connection.execute("DETACH field")
    return folder


# --- the scenario, run by each version ---------------------------------------------------------


def statement(sql: str, *parameters: Parameter, timeout_ms: int | None = None) -> StatementRequest:
    return StatementRequest(
        kind="statement", sql=sql, parameters=list(parameters), timeout_ms=timeout_ms
    )


def parameter(name: str, kind: str, value: str | None) -> Parameter:
    return Parameter.model_validate({"name": name, "type": kind, "value": value})


def arrow(table: pa.Table) -> dict[str, object]:
    """An Arrow table as compared: every field with its type and metadata, and the rows."""
    return {
        "schema": [
            [
                field.name,
                str(field.type),
                field.nullable,
                {k.decode(): v.decode() for k, v in (field.metadata or {}).items()},
            ]
            for field in table.schema
        ],
        "rows": [[repr(value) for value in row.values()] for row in table.to_pylist()],
    }


class Observer:
    """Every step's outcome, in order: its value or the store error it raised."""

    def __init__(self) -> None:
        self.steps: list[dict[str, object]] = []

    def step(self, name: str, work: Callable[[], object]) -> object:
        try:
            value = work()
        except StoreError as error:
            self.steps.append(
                {
                    "step": name,
                    "error": {
                        "code": error.code,
                        "message": error.message,
                        "details": error.details,
                    },
                }
            )
            return None
        except Exception as error:  # noqa: BLE001 - any other outcome is an observation too
            self.steps.append({"step": name, "raised": [type(error).__name__, str(error)]})
            return None
        self.steps.append(
            {"step": name, "value": "an open lake" if isinstance(value, Lake) else value}
        )
        return value


def read(lake: Lake, sql: str, *parameters: Parameter, timeout_ms: int | None = None) -> object:
    request = ReadRequest(
        kind="read",
        sql=sql,
        parameters=list(parameters),
        snapshot=lake.current_snapshot().id,
        batch_size=4096,
        timeout_ms=timeout_ms,
    )
    return arrow(lake.read(request).read_all())


def write(lake: Lake, *statements: StatementRequest) -> object:
    with lake.transaction() as transaction:
        for item in statements:
            transaction.execute(item)
    result = transaction.result
    return None if result is None or result.snapshot is None else result.snapshot.id


REFUSED_READS = [
    "SELECT * FROM read_csv('{sources}/consumers.csv')",
    "SELECT * FROM '{synthetic}/readings.parquet'",
    "SELECT * FROM '{synthetic}/*.parquet'",
    'SELECT * FROM "{synthetic}/readings".parquet',
    "SELECT * FROM 's3://bucket/x.parquet'",
    "SELECT * FROM 'file:///etc/passwd'",
    "SELECT * FROM __ducklake_metadata_lake.ducklake_table",
    "SELECT current_setting('memory_limit') AS m",
    "SELECT * FROM duckdb_settings()",
    "SELECT * FROM readings AT (VERSION => 1)",
    "SUMMARIZE readings",
    "SELECT * FROM (DESCRIBE readings)",
    "SELECT 1 AS a; SELECT 2 AS b",
    "SELEC 1",
    "SELECT * FROM postgres_query('a', 'b')",
    "SELECT json_serialize_plan('SELECT 1') AS p",
    "SELECT * FROM set_option('encrypted', true)",
    "SELECT * FROM readings_source.data",
    "SELECT * FROM missing_table",
    "SELECT 'a\x00b' AS nul",
    "INSERT INTO readings SELECT * FROM readings",
]
REFUSED_STATEMENTS = [
    "COPY readings TO '{work}/out.csv'",
    "ATTACH ':memory:' AS other",
    "CREATE VIEW v AS SELECT 1 AS a",
    "CREATE MACRO m(a) AS a + 1",
    "INSTALL httpfs",
    "SET memory_limit = '64GB'",
    "CALL ducklake_set_option('lake', 'encrypted', true)",
    "CREATE TABLE opts (a INTEGER) WITH (x = 1)",
    "CREATE TABLE f AS SELECT * FROM '{synthetic}/payments.parquet'",
    "CREATE TABLE g AS SELECT * FROM read_parquet('{synthetic}/payments.parquet')",
    "CREATE SEQUENCE s",
    "CREATE TABLE memory.main.outside AS SELECT 1 AS a",
    "DROP TABLE missing_table",
]


def sources(folder: Path) -> list[Any]:
    data, files = folder / "synthetic", folder / "sources"
    return [
        FileSourceAttachment(
            kind="source_attachment",
            name="readings_source",
            type="parquet",
            path=(data / "readings.parquet").as_posix(),
        ),
        FileSourceAttachment(
            kind="source_attachment",
            name="consumers_source",
            type="csv",
            path=(files / "consumers.csv").as_posix(),
        ),
        FileSourceAttachment(
            kind="source_attachment",
            name="billing",
            type="duckdb",
            path=(files / "billing.duckdb").as_posix(),
        ),
        SqliteSourceAttachment(
            kind="source_attachment",
            name="field",
            type="sqlite",
            path=(files / "field.sqlite").as_posix(),
        ),
    ]


def files_of(root: Path | None, keys: list[str] | None) -> dict[str, int]:
    """Data files per table folder (the names are DuckLake's random ones)."""
    names = keys if keys is not None else [p.as_posix() for p in (root or Path()).rglob("*")]
    counted: dict[str, int] = {}
    for name in names:
        if name.endswith((".parquet", ".bin")):
            folder = re.sub(r"[^/]*$", "", name).removeprefix((root or Path()).as_posix())
            counted[folder] = counted.get(folder, 0) + 1
    return dict(sorted(counted.items()))


def state(lake: Lake, files: Callable[[], dict[str, int]]) -> dict[str, object]:
    """The lake's persisted state: tables, columns, snapshot ids, file statistics, files."""
    current = lake.current_snapshot().id
    tables = sorted(lake.tables())
    names = [f"{schema}.{table}" for schema, table in tables]
    statistics = (
        lake.statistics(
            StatisticsRequest.model_validate(
                {"kind": "statistics", "tables": names, "snapshot": current}
            )
        )
        if names
        else None
    )
    return {
        "tables": tables,
        "columns": lake.table_columns(TablesRequest(kind="tables", snapshot=current)).model_dump(
            mode="json"
        ),
        "snapshots": [i for i in range(current + 2) if lake.snapshot(i) is not None],
        "statistics": None
        if statistics is None
        else [
            {
                "table": table.table,
                "complete": table.complete,
                "rows": [f.rows for f in table.files],
                "inlined": table.inlined_rows,
                "columns": [c.model_dump(mode="json") for f in table.files for c in f.columns],
            }
            for table in statistics.tables
        ],
        "files": files(),
    }


def operations(observe: Observer, lake: Lake, folder: Path, work: Path) -> None:
    """The fixed operations on one open lake."""
    paths = {"sources": folder / "sources", "synthetic": folder / "synthetic", "work": work}
    text = {key: value.as_posix() for key, value in paths.items()}
    observe.step(
        "create from file sources",
        lambda: write(
            lake,
            statement(
                "CREATE TABLE readings AS SELECT * FROM readings_source.data ORDER BY reading_id"
            ),
            statement("CREATE TABLE consumers AS SELECT * FROM consumers_source.data"),
            statement("CREATE SCHEMA billing_copy"),
        ),
    )

    def land() -> object:
        with lake.transaction() as transaction:
            invoices = transaction.land(
                "billing",
                "SELECT * FROM invoices WHERE amount > $least ORDER BY invoice_id",
                [parameter("least", "DECIMAL(14,2)", "10.00")],
                "billing_copy",
                "invoices",
            )
            meters = transaction.land("field", "SELECT * FROM meters", [], "main", "meters")
        return [invoices, meters]

    observe.step("land database sources", land)

    def load() -> object:
        stream = pa.RecordBatchReader.from_batches(
            pa.schema([("id", pa.int64()), ("label", pa.string())]),
            [pa.record_batch([pa.array([1, 2, 3]), pa.array(["a", None, "c"])], ["id", "label"])],
        )
        with lake.transaction() as transaction:
            return transaction.load_arrow(
                "main", "loaded", stream, [("id", "HUGEINT"), ("label", "VARCHAR")]
            )

    observe.step("load arrow", load)
    observe.step(
        "write exact types",
        lambda: write(
            lake,
            statement(
                "CREATE TABLE exact AS SELECT CAST($big AS UHUGEINT) AS big, "
                "CAST('2026-03-01 10:00:00+00' AS TIMESTAMPTZ) AS at, $d AS d, $t AS t",
                parameter("big", "UHUGEINT", "340282366920938463463374607431768211455"),
                parameter("d", "DECIMAL(38,10)", "-12345678901234567890.0123456789"),
                parameter("t", "TIMESTAMPTZ", "2026-01-01T00:00:00+05:00"),
            ),
        ),
    )
    since = parameter("since", "DATE", "2026-01-01")
    least = parameter("least", "DECIMAL(12,3)", "0.500")
    observe.step("guarded aggregate read", lambda: read(lake, TOTALS, since, least))
    observe.step(
        "guarded detail read",
        lambda: read(
            lake,
            "WITH c AS (SELECT * FROM consumers) SELECT r.reading_id, r.kwh, r.read_at, c.tariff "
            "FROM readings AS r JOIN meters AS m USING (meter_id) JOIN c USING (consumer_id) "
            "ORDER BY r.reading_id LIMIT 25",
        ),
    )
    observe.step(
        "landed totals",
        lambda: read(
            lake,
            "SELECT count(*) AS n, sum(amount) AS amount, bool_or(estimated) AS e "
            "FROM billing_copy.invoices",
        ),
    )
    observe.step("exact read", lambda: read(lake, "SELECT * FROM exact"))
    observe.step("loaded read", lambda: read(lake, "SELECT * FROM lake.main.loaded ORDER BY id"))
    observe.step(
        "describe",
        lambda: lake.describe(
            "SELECT kwh * 2 AS twice, $p AS p FROM readings", [parameter("p", "DATE", None)]
        ),
    )
    observe.step(
        "describe source",
        lambda: lake.describe_source(
            "billing", "SELECT invoice_id, amount, issued_at FROM invoices"
        ),
    )
    for index, sql in enumerate(REFUSED_READS):
        observe.step(f"refused read {index}", partial(read, lake, sql.format(**text)))
    for index, sql in enumerate(REFUSED_STATEMENTS):
        observe.step(
            f"refused statement {index}",
            partial(write, lake, statement(sql.format(**text))),
        )
    for name, value in [("DECIMAL(4,2)", "123.45"), ("DATE", "2026-02-30"), ("BIGINT", "1e3")]:
        observe.step(
            f"invalid parameter {name}",
            partial(read, lake, "SELECT $p AS p", parameter("p", name, value)),
        )
    observe.step(
        "parameters that do not match",
        lambda: read(lake, "SELECT $a AS a", parameter("b", "INTEGER", "1")),
    )
    before = lake.current_snapshot().id

    def rolled_back() -> object:
        class Stop(Exception):
            pass

        try:
            with lake.transaction() as transaction:
                transaction.execute(statement("DELETE FROM readings"))
                raise Stop
        except Stop:
            pass
        return [before, lake.current_snapshot().id]

    observe.step("rolled-back transaction", rolled_back)
    observe.step(
        "rows after the rollback", lambda: read(lake, "SELECT count(*) AS n FROM readings")
    )
    observe.step(
        "transaction refused at commit",
        lambda: write(lake, statement("DELETE FROM readings WHERE kwh < 1"), statement("SELECT 1")),
    )
    observe.step(
        "transaction that times out",
        lambda: write(lake, statement(f"CREATE TABLE slow AS {SLOW}", timeout_ms=300)),
    )
    observe.step("snapshot after the refusals", lambda: [before, lake.current_snapshot().id])
    observe.step(
        "committed changes",
        lambda: write(
            lake,
            statement(
                "DELETE FROM readings WHERE kwh < $low", parameter("low", "DECIMAL(12,3)", "1")
            ),
            statement("UPDATE consumers SET status = 'audited' WHERE tariff = 'industrial'"),
            statement("INSERT INTO loaded VALUES (4, 'd')"),
        ),
    )
    observe.step("read that times out", lambda: read(lake, SLOW, timeout_ms=300))

    def cancelled() -> object:
        handle = lake.start_read(
            ReadRequest(
                kind="read",
                sql=SLOW,
                parameters=[],
                snapshot=lake.current_snapshot().id,
                batch_size=1024,
            )
        )
        threading.Timer(0.3, handle.cancel).start()
        return arrow(handle.reader.read_all())

    observe.step("cancelled read", cancelled)
    observe.step(
        "changes",
        lambda: lake.changes(
            ChangesRequest.model_validate(
                {
                    "kind": "changes",
                    "tables": ["readings", "consumers", "loaded", "billing_copy.invoices"],
                    "since": before,
                    "snapshot": lake.current_snapshot().id,
                }
            )
        ).model_dump(mode="json"),
    )
    observe.step(
        "missing snapshot",
        lambda: lake.table_columns(TablesRequest(kind="tables", snapshot=10_000)),
    )
    observe.step(
        "maintenance",
        lambda: lake.maintain(
            dt.datetime.now(dt.UTC) + dt.timedelta(days=1),
            dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
        ).model_dump(mode="json", exclude={"oldest_snapshot": {"committed_at"}}),
    )
    observe.step("expired snapshot", lambda: read_at(lake, before))
    observe.step("aggregate after maintenance", lambda: read(lake, TOTALS, since, least))


def read_at(lake: Lake, snapshot: int) -> object:
    request = ReadRequest(
        kind="read", sql="SELECT 1 AS one", parameters=[], snapshot=snapshot, batch_size=1
    )
    return arrow(lake.read(request).read_all())


def s3(endpoint: str, method: str, path: str) -> bytes:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"http://{endpoint}/{path}", method=method)
    with opener.open(request, timeout=30) as response:
        body: bytes = response.read()
        return body


def locations(
    work: Path, endpoint: str | None
) -> Iterator[tuple[str, Any, Callable[[], dict[str, int]]]]:
    """Every catalog and storage kind available here, with how its data files are listed."""
    local = work / "local"
    yield (
        "duckdb-file catalog",
        LocalLakeLocation(kind="lake_location", catalog="local", path=local.as_posix()),
        lambda: files_of(local, None),
    )
    sqlite = work / "sqlite"
    yield (
        "sqlite catalog",
        SqliteLakeLocation(
            kind="lake_location",
            catalog="sqlite",
            path=(sqlite / "catalog.sqlite").as_posix(),
            data_path=(sqlite / "data").as_posix() + "/",
        ),
        lambda: files_of(sqlite / "data", None),
    )
    encrypted = work / "encrypted"
    yield (
        "encrypted lake",
        LocalLakeLocation(
            kind="lake_location", catalog="local", path=encrypted.as_posix(), encrypted=True
        ),
        lambda: files_of(encrypted, None),
    )
    if endpoint is None:
        return
    s3(endpoint, "PUT", "lake")
    s3_lake = work / "s3"

    def keys() -> dict[str, int]:
        listed = ElementTree.fromstring(s3(endpoint, "GET", "lake?list-type=2"))
        found = [e.text or "" for e in listed.iter() if e.tag.endswith("}Key")]
        return files_of(None, found)

    yield (
        "s3 data",
        LocalLakeLocation(
            kind="lake_location",
            catalog="local",
            path=s3_lake.as_posix(),
            data_path="s3://lake/billing/",
            storage=ObjectStorage(
                S3Storage(
                    type="s3",
                    region="us-east-1",
                    endpoint=endpoint,
                    url_style="path",
                    use_ssl=False,
                    key_id_env=KEY_ENV,
                    secret_env=SECRET_ENV,
                )
            ),
        ),
        keys,
    )


def scenario(folder: Path, work: Path, out: Path) -> None:
    """Every operation on every location, written to `out` as JSON."""
    os.environ[KEY_ENV], os.environ[SECRET_ENV] = "equivalence-key", "equivalence-secret"
    endpoint: str | None = None
    server: Any = None
    try:
        from moto.server import ThreadedMotoServer

        logging.getLogger("werkzeug").setLevel(logging.ERROR)  # one line per request otherwise

        server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
        server.start()
        host, port = server.get_host_and_port()
        endpoint = f"{host}:{port}"
    except ImportError:
        server = None
    observed: dict[str, object] = {}
    try:
        for name, location, files in locations(work, endpoint):
            observe = Observer()
            lake = observe.step("open", partial(open_lake, location, LIMITS, sources(folder)))
            if isinstance(lake, Lake):
                try:
                    operations(observe, lake, folder, work)
                    observe.step("state", partial(state, lake, files))
                finally:
                    lake.close()
                reopened = observe.step("reopen", partial(open_lake, location, LIMITS))
                if isinstance(reopened, Lake):
                    try:
                        observe.step("state after reopen", partial(state, reopened, files))
                    finally:
                        reopened.close()
            observed[name] = observe.steps
        refusal = Observer()
        refusal.step(
            "a reserved source name",
            lambda: open_lake(
                LocalLakeLocation(
                    kind="lake_location", catalog="local", path=(work / "r").as_posix()
                ),
                LIMITS,
                [
                    FileSourceAttachment(
                        kind="source_attachment",
                        name="lake",
                        type="parquet",
                        path=(folder / "synthetic" / "readings.parquet").as_posix(),
                    )
                ],
            ),
        )
        refusal.step(
            "limits missing",
            lambda: open_lake(
                LocalLakeLocation(
                    kind="lake_location", catalog="local", path=(work / "l").as_posix()
                ),
                Limits(memory_limit=None, threads=1),
            ),
        )
        observed["refused location"] = refusal.steps
    finally:
        if server is not None:
            server.stop()
    text = json.dumps(observed, indent=1, sort_keys=True, default=str)
    out.write_text(normalized(text, work, folder, endpoint), encoding="utf-8")


def normalized(text: str, work: Path, folder: Path, endpoint: str | None) -> str:
    """Placeholders for what differs between two runs and is not the store's decision."""
    text = text.replace(work.as_posix(), "<work>").replace(folder.as_posix(), "<data>")
    if endpoint is not None:
        text = text.replace(endpoint, "<endpoint>")
    text = re.sub(r"/tmp/[A-Za-z0-9_.-]+", "/tmp/<tmp>", text)
    text = re.sub(
        r"(lake-spill-|lake-workers-|lake-sources-|source-)[A-Za-z0-9_]+", r"\1<tmp>", text
    )
    text = re.sub(
        r"(?=[0-9]*[a-f])[0-9a-f]{32}|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", "<id>", text
    )
    # Commit times (today's, as ISO instants): the moment a step ran, not a store decision.
    today = dt.datetime.now(dt.UTC).date().isoformat()
    text = re.sub(rf"{today}T[0-9:.]+\+00:00", "<commit-time>", text)
    return text


# --- the orchestrator --------------------------------------------------------------------------


def store_location(python: str) -> str:
    """The folder of the `orvex.store` that `python` loads, so each side's version is on record."""
    found = subprocess.run(
        [python, "-I", "-c", "import orvex.store as s, os; print(os.path.dirname(s.__file__))"],
        check=True,
        capture_output=True,
        text=True,
    )
    return found.stdout.strip()


def run(python: str, folder: Path, work: Path, out: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    subprocess.run(
        [python, "-I", Path(__file__).as_posix(), "scenario", str(folder), str(work), str(out)],
        check=True,
    )
    shutil.rmtree(work, ignore_errors=True)


def differences(baseline: object, candidate: object, where: str = "") -> list[str]:
    """Every place two observations differ, as `path: baseline != candidate`."""
    if isinstance(baseline, dict) and isinstance(candidate, dict):
        found: list[str] = []
        for key in sorted(set(baseline) | set(candidate), key=str):
            if key not in baseline or key not in candidate:
                found.append(
                    f"{where}/{key}: only in {'candidate' if key in candidate else 'baseline'}"
                )
            else:
                found += differences(baseline[key], candidate[key], f"{where}/{key}")
        return found
    if isinstance(baseline, list) and isinstance(candidate, list):
        if len(baseline) != len(candidate):
            return [f"{where}: {len(baseline)} items != {len(candidate)}"]
        found = []
        for index, (left, right) in enumerate(zip(baseline, candidate, strict=True)):
            found += differences(left, right, f"{where}[{index}]")
        return found
    return [] if baseline == candidate else [f"{where}: {baseline!r} != {candidate!r}"]


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "scenario":
        scenario(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        return 0
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--baseline", required=True, help="interpreter with the baseline store")
    parser.add_argument("--candidate", required=True, help="interpreter with the candidate store")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--keep", type=Path, help="folder for both observations (JSON)")
    arguments = parser.parse_args()
    loaded = {side: store_location(getattr(arguments, side)) for side in ("baseline", "candidate")}
    if loaded["baseline"] == loaded["candidate"]:
        parser.error(f"both interpreters load the same store: {loaded['baseline']}")
    with tempfile.TemporaryDirectory(prefix="store-equivalence-") as temporary:
        root = Path(temporary)
        folder = prepare(root / "data", arguments.seed)
        kept = arguments.keep or root
        kept.mkdir(parents=True, exist_ok=True)
        observed = {}
        for side in ("baseline", "candidate"):
            out = kept / f"{side}.json"
            run(getattr(arguments, side), folder, root / "work", out)
            observed[side] = json.loads(out.read_text(encoding="utf-8"))
    found = differences(observed["baseline"], observed["candidate"])
    steps = sum(len(v) for v in observed["candidate"].values())
    errors = sum(1 for v in observed["candidate"].values() for s in v if "error" in s)
    print(
        json.dumps(
            {
                "locations": sorted(observed["candidate"]),
                "stores": loaded,
                "steps": steps,
                "store_errors": errors,
                "differences": len(found),
            }
        )
    )
    for line in found[:50]:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
