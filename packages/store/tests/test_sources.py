"""P1-06: file sources (Parquet, CSV, DuckDB) attached read-only and readable only by name."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    PostgresLakeLocation,
    ReadRequest,
    SourceAttachment,
    StatementRequest,
)
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, open_lake

LIMITS = Limits(
    memory_limit="1GB", threads=2, max_concurrent=8, queue_size=64, default_timeout_ms=600_000
)
REPOSITORY = Path(__file__).resolve().parents[3]

type Location = LocalLakeLocation | PostgresLakeLocation


def source(name: str, kind: str, path: Path | str) -> SourceAttachment:
    return SourceAttachment(
        kind="source_attachment",
        name=name,
        type=kind,  # type: ignore[arg-type]
        path=str(path),
    )


def statement(sql: str) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[])


def write(lake: Lake, *sqls: str) -> None:
    with lake.transaction() as transaction:
        for sql in sqls:
            transaction.execute(statement(sql))


def read(lake: Lake, sql: str) -> list[dict[str, object]]:
    request = ReadRequest(
        kind="read",
        sql=sql,
        parameters=[],
        snapshot=lake.current_snapshot().id,
        batch_size=65536,
    )
    rows: list[dict[str, object]] = lake.read(request).read_all().to_pylist()
    return rows


def code_of(action: object) -> str:
    assert callable(action)
    with pytest.raises(StoreError) as raised:
        action()
    return raised.value.code


@pytest.fixture
def files(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Source files in their own folder, outside the lake's folder (which the sandbox allows)."""
    folder = tmp_path_factory.mktemp("sources")
    (folder / "readings").mkdir(parents=True)
    (folder / "private").mkdir()
    builder = duckdb.connect()
    builder.sql(
        f"COPY (SELECT 1::BIGINT AS reading_id, 12.50::DECIMAL(12,2) AS kwh) "
        f"TO '{folder / 'readings' / 'a.parquet'}'"
    )
    builder.sql(
        f"COPY (SELECT 2::BIGINT AS reading_id, 'estimated' AS status) "
        f"TO '{folder / 'readings' / 'b.parquet'}'"
    )
    builder.sql(f"COPY (SELECT 'T1' AS tariff, 0.85 AS price) TO '{folder / 'tariffs.csv'}'")
    builder.sql(f"COPY (SELECT 'T2' AS tariff) TO '{folder / 'sibling.csv'}'")
    builder.sql(f"COPY (SELECT 42 AS hidden) TO '{folder / 'private' / 'secret.parquet'}'")
    builder.sql(f"COPY (SELECT 'SECRET' AS s) TO '{folder / 'readings' / 'undeclared.csv'}'")
    builder.close()
    legacy = duckdb.connect(str(folder / "legacy.duckdb"))
    legacy.sql("CREATE TABLE consumers AS SELECT 'C1' AS consumer_id, 7::HUGEINT AS units")
    legacy.sql("CREATE SCHEMA billing")
    legacy.sql("CREATE TABLE billing.invoices AS SELECT 'I1' AS invoice_id")
    legacy.close()
    return folder


@pytest.fixture
def sourced(location: Location, files: Path) -> Iterator[Lake]:
    opened = open_lake(
        location,
        LIMITS,
        [
            source("archive", "parquet", files / "readings" / "*.parquet"),
            source("tariffs", "csv", files / "tariffs.csv"),
            source("legacy", "duckdb", files / "legacy.duckdb"),
        ],
    )
    yield opened
    opened.close()


def test_writes_read_parquet_csv_and_duckdb_sources(sourced: Lake) -> None:
    write(
        sourced,
        "CREATE TABLE readings AS SELECT * FROM archive.data ORDER BY reading_id",
        "CREATE TABLE tariffs AS SELECT * FROM tariffs.data",
        "CREATE TABLE consumers AS SELECT * FROM legacy.consumers",
        "CREATE TABLE consumers_main AS SELECT * FROM legacy.main.consumers",
        "CREATE TABLE invoices AS SELECT * FROM legacy.billing.invoices",
    )
    rows = read(sourced, "SELECT reading_id, CAST(kwh AS VARCHAR) AS kwh, status FROM readings")
    assert rows == [
        {"reading_id": 1, "kwh": "12.50", "status": None},
        {"reading_id": 2, "kwh": None, "status": "estimated"},
    ]
    assert read(sourced, "SELECT tariff FROM tariffs") == [{"tariff": "T1"}]
    assert read(sourced, "SELECT consumer_id FROM consumers") == [{"consumer_id": "C1"}]
    assert read(sourced, "SELECT consumer_id FROM consumers_main") == [{"consumer_id": "C1"}]
    assert read(sourced, "SELECT invoice_id FROM invoices") == [{"invoice_id": "I1"}]


def test_exact_types_survive_from_sources(sourced: Lake) -> None:
    write(sourced, "CREATE TABLE c AS SELECT * FROM legacy.consumers")
    write(sourced, "CREATE TABLE r AS SELECT * FROM archive.data")
    assert read(sourced, "SELECT typeof(units) AS t FROM c") == [{"t": "HUGEINT"}]
    assert read(sourced, "SELECT DISTINCT typeof(kwh) AS t FROM r") == [{"t": "DECIMAL(12,2)"}]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO legacy.consumers VALUES ('C2', 1)",
        "CREATE TABLE legacy.extra AS SELECT 1 AS x",
        "DROP TABLE legacy.consumers",
        "CREATE TABLE archive.extra AS SELECT 1 AS x",
        "DROP VIEW archive.data",
    ],
)
def test_sources_are_read_only(sourced: Lake, files: Path, sql: str) -> None:
    before = hashlib.sha256((files / "legacy.duckdb").read_bytes()).hexdigest()
    code = code_of(lambda: write(sourced, sql))
    assert code in {"STORE_QUERY_FAILED", "STORE_STATEMENT_INVALID"}
    if sql.startswith(("INSERT", "CREATE TABLE legacy")):
        assert code == "STORE_QUERY_FAILED"
    assert hashlib.sha256((files / "legacy.duckdb").read_bytes()).hexdigest() == before


def test_files_outside_the_declared_paths_stay_unreadable(sourced: Lake, files: Path) -> None:
    # The lake would read these files if they were reachable; each attempt is refused.
    sibling = files / "sibling.csv"
    attempts = [
        f"CREATE TABLE x AS SELECT * FROM read_csv('{sibling}')",
        f"CREATE TABLE x AS SELECT * FROM '{sibling}'",
        f'CREATE TABLE x AS SELECT * FROM "{sibling}"',
        f"CREATE TABLE x AS SELECT * FROM read_parquet('{files}/readings/../private/*.parquet')",
        f"CREATE TABLE x AS SELECT * FROM read_parquet('{files / 'readings' / 'a.parquet'}')",
        f"CREATE TABLE x AS SELECT * FROM archive.data, '{files / 'readings' / 'a.parquet'}'",
    ]
    undeclared = files / "readings" / "undeclared.csv"
    attempts += [
        f"CREATE TABLE x AS SELECT * FROM archive.data AS a, '{undeclared}'",
        f"CREATE TABLE x AS SELECT * FROM archive.data a JOIN tariffs.data b ON true, "
        f"'{undeclared}'",
        f"MERGE INTO x USING '{undeclared}' AS u ON false WHEN NOT MATCHED THEN INSERT VALUES (1)",
        f"CREATE TABLE x AS SELECT * FROM ('{undeclared}')",
        f"CREATE TABLE x AS PIVOT '{undeclared}' ON s",
        f"CREATE TABLE x AS SELECT * FROM (SUMMARIZE '{undeclared}')",
        f"CREATE TABLE x AS SELECT * FROM (DESCRIBE '{undeclared}')",
        f"CREATE TABLE x AS SELECT * FROM '{files}/readings/*.csv'",
    ]
    for sql in attempts:
        assert code_of(lambda sql=sql: write(sourced, sql)) == "STORE_STATEMENT_INVALID", sql


def test_sandbox_allows_only_declared_files(location: Location, files: Path) -> None:
    """Behind the statement checks, DuckDB itself refuses every file that is not declared,
    including files next to a glob's matches and paths leaving a declared folder."""
    (files.parent / "elsewhere.csv").write_text("x\n1\n", encoding="utf-8")
    lake = open_lake(
        location,
        LIMITS,
        [
            source("archive", "parquet", files / "readings" / "*.parquet"),
            source("tariffs", "csv", files / "tariffs.csv"),
        ],
    )
    try:
        connection = lake._connection  # the raw connection: the sandbox, not the guard, decides
        allowed = connection.execute(f"SELECT count(*) FROM read_csv('{files / 'tariffs.csv'}')")
        assert allowed.fetchall() == [(1,)]
        for path in [
            files / "sibling.csv",
            files / "readings" / "undeclared.csv",
            files / "readings" / ".." / "sibling.csv",
            files / "readings" / ".." / "private" / "secret.parquet",
            files.parent / "elsewhere.csv",
        ]:
            with pytest.raises(duckdb.Error):
                connection.execute(f"SELECT * FROM '{path}'").fetchall()
        # A file added after the lake opened is not readable either.
        late = files / "readings" / "late.parquet"
        duckdb.connect().sql(f"COPY (SELECT 3 AS reading_id) TO '{late}'")
        with pytest.raises(duckdb.Error):
            connection.execute(f"SELECT * FROM '{late}'").fetchall()
    finally:
        lake.close()


@pytest.mark.parametrize("name", ["archive.data", "legacy.consumers", "legacy.main.consumers"])
def test_reads_cannot_reference_sources(sourced: Lake, name: str) -> None:
    assert code_of(lambda: read(sourced, f"SELECT * FROM {name}")) == "STORE_STATEMENT_INVALID"


@pytest.mark.parametrize(
    ("kind", "relative"),
    [
        ("parquet", "missing/*.parquet"),
        ("parquet", "nothing.parquet"),
        ("csv", "nothing.csv"),
        ("duckdb", "nothing.duckdb"),
    ],
)
def test_missing_source_is_reported_at_open(
    location: Location, files: Path, kind: str, relative: str
) -> None:
    with pytest.raises(StoreError) as raised:
        open_lake(location, LIMITS, [source("archive", kind, files / relative)])
    assert raised.value.code == "SOURCE_NOT_FOUND"
    assert raised.value.details["source"] == "archive"


def test_duplicate_source_names_are_refused(location: Location, files: Path) -> None:
    sources = [
        source("tariffs", "csv", files / "tariffs.csv"),
        source("tariffs", "csv", files / "sibling.csv"),
    ]
    with pytest.raises(StoreError) as raised:
        open_lake(location, LIMITS, sources)
    assert raised.value.code == "STORE_SOURCE_INVALID"


@pytest.mark.parametrize("name", ["lake", "main", "system", "temp", "memory"])
def test_reserved_source_names_are_refused(location: Location, files: Path, name: str) -> None:
    with pytest.raises(StoreError) as raised:
        open_lake(location, LIMITS, [source(name, "csv", files / "tariffs.csv")])
    assert raised.value.code == "STORE_SOURCE_INVALID"


@pytest.mark.parametrize(
    "definition",
    ["CREATE MACRO twice(x) AS x * 2", "CREATE VIEW everything AS SELECT 1 AS x"],
    ids=["macro", "view"],
)
def test_duckdb_source_with_macros_or_views_is_refused(
    location: Location, files: Path, definition: str
) -> None:
    with_object = files / "objects.duckdb"
    connection = duckdb.connect(str(with_object))
    connection.sql(definition)
    connection.close()
    with pytest.raises(StoreError) as raised:
        open_lake(location, LIMITS, [source("legacy", "duckdb", with_object)])
    assert raised.value.code == "STORE_SOURCE_INVALID"


def test_synthetic_dataset_loads_through_declared_sources(
    location: Location, tmp_path: Path
) -> None:
    out = tmp_path / "small"
    subprocess.run(
        [sys.executable, "-m", "tools.synthetic.generate", "--size", "small", "--out", str(out)],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
    )
    tables = ["consumers", "meters", "readings", "invoices", "payments", "credit_notes"]
    lake = open_lake(
        location, LIMITS, [source(f"src_{t}", "parquet", out / f"{t}.parquet") for t in tables]
    )
    try:
        write(lake, *[f"CREATE TABLE {t} AS SELECT * FROM src_{t}.data" for t in tables])
        counts = {t: read(lake, f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in tables}
    finally:
        lake.close()
    assert counts == {
        "consumers": 10_000,
        "meters": 10_300,
        "readings": 240_720,
        "invoices": 240_000,
        "payments": 236_400,
        "credit_notes": 100,
    }
