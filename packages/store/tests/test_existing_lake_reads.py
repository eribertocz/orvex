"""Reads of an existing DuckLake and its statement guard (P2-30 PR 3, #399; ADR-0027 §3, §5).

The lake lives on a file system of its own (`other_device`), so a protected set in `tmp_path`
shares none with it. PostgreSQL cases use the fixtures of `test_existing_lake`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import (
    AzureStorage,
    ExistingLakeAttachment,
    LocalLakeLocation,
    ObjectStorage,
    Parameter,
    ProtectedLocation,
    ReadRequest,
    RolesInUseItem,
    Snapshot,
    SqliteLakeLocation,
    StatementRequest,
)
from orvex.store.errors import StoreError
from orvex.store.existing_lake import ExistingLake
from orvex.store.existing_lake_reads import SNAPSHOT_CHANGED, LakeReads
from orvex.store.lake import Limits, open_lake

from .test_existing_lake import MEMORY, Catalog
from .test_existing_lake_files import attachment, edit_catalog

TIMEOUT_MS = 60_000


def make_lake(root: Path) -> tuple[Path, Path]:
    """`catalog.sqlite` and `data/` at `root`: table `main.readings` (exact decimals), a view
    over it and a second schema `ops` with a table."""
    catalog, data = root / "catalog.sqlite", root / "data"
    data.mkdir(parents=True)
    writer = duckdb.connect()
    for extension in ("ducklake", "sqlite"):
        writer.install_extension(extension)
        writer.load_extension(extension)
    writer.execute(
        f"ATTACH 'ducklake:sqlite:{catalog}' AS l "
        f"(DATA_PATH '{data.as_posix()}/', DATA_INLINING_ROW_LIMIT 0)"
    )
    writer.execute("CREATE TABLE l.readings (reading_id BIGINT, kwh DECIMAL(18, 4))")
    writer.execute("INSERT INTO l.readings VALUES (1, 1.5000), (2, 2.2500), (3, NULL)")
    writer.execute("CREATE VIEW l.big_readings AS SELECT * FROM l.readings WHERE kwh > 2")
    writer.execute("CREATE SCHEMA l.ops")
    writer.execute("CREATE TABLE l.ops.meters (meter_id BIGINT)")
    writer.execute("INSERT INTO l.ops.meters VALUES (7)")
    writer.close()
    connection = sqlite3.connect(catalog)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    connection.close()
    return catalog, data


@pytest.fixture
def postgres(tmp_path: Path) -> Iterator[Catalog]:
    """A PostgreSQL catalog of its own (`test_existing_lake.Catalog`)."""
    found = Catalog(tmp_path)
    yield found
    found.close()


@pytest.fixture
def made(other_device: Path) -> tuple[Path, Path]:
    return make_lake(other_device / "owner")


@pytest.fixture
def reads(made: tuple[Path, Path]) -> Iterator[LakeReads]:
    with ExistingLake(attachment(*made), memory_limit=MEMORY, threads=1) as lake:
        yield LakeReads(lake, timeout_ms=TIMEOUT_MS)


def request(sql: str, snapshot: Snapshot, *parameters: Parameter, **extra: object) -> ReadRequest:
    return ReadRequest(
        kind="read",
        sql=sql,
        parameters=list(parameters),
        snapshot=snapshot.id,
        batch_size=1024,
        **extra,  # type: ignore[arg-type]
    )


def rows(reads: LakeReads, sql: str, *parameters: Parameter) -> list[dict[str, object]]:
    snapshot = reads.latest()
    found = reads.read(request(sql, snapshot, *parameters), snapshot.committed_at)
    return found.read_all().to_pylist()  # type: ignore[no-any-return]


def refused(reads: LakeReads, sql: str) -> StoreError:
    snapshot = reads.latest()
    with pytest.raises(StoreError) as caught:
        reads.read(request(sql, snapshot), snapshot.committed_at)
    return caught.value


# --- reads the adapter generates -------------------------------------------------------------


def test_a_read_names_lake_tables_unqualified_and_gets_exact_values(reads: LakeReads) -> None:
    found = rows(
        reads,
        'SELECT SUM("kwh") AS "total", COUNT("kwh") AS "n" FROM "readings" '
        'WHERE "reading_id" >= $low',
        Parameter.model_validate({"name": "low", "type": "BIGINT", "value": "1"}),
    )
    assert found == [{"total": Decimal("3.7500"), "n": 2}]
    assert rows(reads, 'SELECT "meter_id" FROM "ops"."meters"') == [{"meter_id": 7}]
    for named in ("lake.readings", "lake.main.readings", "main.readings"):
        assert rows(reads, f"SELECT count(*) AS c FROM {named}") == [{"c": 3}], named
    assert rows(reads, "SELECT count(*) AS c FROM lake.ops.meters") == [{"c": 1}]
    assert rows(reads, "WITH r AS (SELECT * FROM readings) SELECT count(*) AS c FROM r") == [
        {"c": 3}
    ]


def test_a_read_carries_duckdb_types_as_the_managed_lake_does(reads: LakeReads) -> None:
    snapshot = reads.latest()
    table = reads.read(
        request('SELECT "kwh" FROM "readings"', snapshot), snapshot.committed_at
    ).read_all()
    assert table.schema.field("kwh").metadata == {b"duckdb_type": b"DECIMAL(18,4)"}


# Every kind whose Arrow form DuckDB exports in two ways (lossless or not), and an exact one.
ARROW_KINDS = (
    'SELECT "kwh" > 2 AS high, '
    "'2b9b6b8e-2f6c-4a51-9a35-6f6d7a4b8c11'::UUID AS id, "
    "INTERVAL 1 DAY AS span, 170141183460469231731687303715884105727::HUGEINT AS huge, "
    '\'{"a": 1}\'::JSON AS doc, [1, 2] AS list, "kwh" FROM "readings"'
)


def test_a_reads_arrow_types_are_the_managed_lakes(reads: LakeReads, tmp_path: Path) -> None:
    """Corpus parity (#399): booleans and every other kind arrive as the managed lake's reads
    them, so the semantic engine finds the plan's own types (no lossless extension types)."""
    snapshot = reads.latest()
    found = reads.read(request(ARROW_KINDS, snapshot), snapshot.committed_at).read_all()
    location = LocalLakeLocation(kind="lake_location", catalog="local", path=str(tmp_path / "m"))
    managed = open_lake(location, Limits(MEMORY, 1, 1, 0, 60_000))
    try:
        with managed.transaction() as transaction:
            transaction.execute(
                StatementRequest(
                    kind="statement",
                    sql="CREATE TABLE readings AS SELECT 2.5::DECIMAL(18,4) AS kwh",
                    parameters=[],
                )
            )
        current = managed.current_snapshot()
        expected = managed.read(request(ARROW_KINDS, current)).read_all()
    finally:
        managed.close()
    assert found.schema.field("high").type == "bool"
    assert [(f.name, f.type, f.metadata) for f in found.schema] == [
        (f.name, f.type, f.metadata) for f in expected.schema
    ]


def test_a_read_is_pinned_to_its_snapshot_whatever_the_owner_commits_after(
    made: tuple[Path, Path], reads: LakeReads
) -> None:
    catalog, data = made
    before = reads.latest()
    writer = duckdb.connect()
    for extension in ("ducklake", "sqlite"):
        writer.load_extension(extension)
    writer.execute(f"ATTACH 'ducklake:sqlite:{catalog}' AS l (DATA_PATH '{data.as_posix()}/')")
    writer.execute("INSERT INTO l.readings VALUES (4, 10.0000)")
    writer.close()
    assert reads.latest().id > before.id
    found = reads.read(
        request("SELECT count(*) AS c FROM readings", before), before.committed_at
    ).read_all()
    assert found.to_pylist() == [{"c": 3}]


# --- the statement guard (ADR-0027 §3): only the adapter's shape runs ------------------------


@pytest.mark.parametrize(
    ("sql", "why"),
    [
        ("SELECT * FROM postgres_query('lake', 'SELECT 1')", "postgres_query"),
        ("CALL postgres_execute('lake', 'DROP TABLE x')", "postgres_execute"),
        ("SELECT postgres_execute('lake', 'DROP TABLE x')", "postgres_execute scalar"),
        ("COPY (SELECT 1) TO '{data}/written.parquet'", "COPY into the data folder"),
        ("COPY readings TO '{data}/written.csv'", "COPY a table"),
        ("ATTACH ':memory:' AS m", "ATTACH memory"),
        ("SELECT 1; ATTACH ':memory:' AS m", "two statements"),
        ("INSERT INTO readings VALUES (9, 1)", "a write"),
        ("SELECT * FROM big_readings", "a view of the lake"),
        ("SELECT * FROM lake.main.big_readings", "a qualified view"),
        ("SELECT lake.main.abs(-1) AS a", "a function of the lake's catalog"),
        ("SELECT main.abs(-1) AS a", "a function of a named schema"),
        ("SELECT * FROM read_parquet('{data}/*.parquet')", "a file reader"),
        ("SELECT * FROM '{data}/x.parquet'", "a file name"),
        ("SELECT * FROM memory.main.anything", "another catalog"),
        ("SELECT * FROM readings AT (VERSION => 1)", "time travel in the SQL"),
        ("SELECT * FROM __ducklake_metadata_lake.ducklake_snapshot", "the catalog database"),
        ("SELECT * FROM ducklake_snapshots('lake')", "a DuckLake table function"),
        ("SELECT * FROM query('SELECT 1')", "SQL as text"),
        ("SELECT current_setting('enable_external_access') AS s", "a setting"),
    ],
)
def test_the_guard_refuses_whatever_the_adapter_does_not_generate(
    made: tuple[Path, Path], reads: LakeReads, sql: str, why: str
) -> None:
    _, data = made
    error = refused(reads, sql.format(data=data.as_posix()))
    assert error.code in {"STORE_STATEMENT_INVALID", "STORE_QUERY_FAILED"}, why
    assert not list(data.glob("written*")), why


def test_the_guard_refuses_before_the_instance_sees_the_statement(
    made: tuple[Path, Path], reads: LakeReads
) -> None:
    # A COPY into the allow-listed data folder would succeed on the instance itself.
    _, data = made
    error = refused(reads, f"COPY (SELECT 1 AS x) TO '{data.as_posix()}/written.parquet'")
    assert error.code == "STORE_STATEMENT_INVALID"
    assert not (data / "written.parquet").exists()


def test_the_lake_is_not_on_the_search_path(reads: LakeReads) -> None:
    assert rows(reads, "SELECT current_database() AS d") == [{"d": "memory"}]


# --- the commit time, in the read transaction (ADR-0027 §5) ----------------------------------


def test_a_read_of_a_snapshot_whose_commit_time_changed_is_refused(
    made: tuple[Path, Path], reads: LakeReads
) -> None:
    catalog, _ = made
    snapshot = reads.latest()
    edit_catalog(
        catalog,
        "UPDATE ducklake_snapshot SET snapshot_time = '2001-01-01 00:00:00+00' "
        "WHERE snapshot_id = ?",
        snapshot.id,
    )
    with pytest.raises(StoreError) as caught:
        reads.read(request("SELECT count(*) AS c FROM readings", snapshot), snapshot.committed_at)
    assert caught.value.code == "STORE_SOURCE_INVALID"
    assert caught.value.details["reason"] == SNAPSHOT_CHANGED


def test_a_read_of_a_snapshot_the_catalog_no_longer_lists_is_refused(reads: LakeReads) -> None:
    snapshot = reads.latest()
    gone = Snapshot(kind="snapshot", id=snapshot.id + 50, committed_at=snapshot.committed_at)
    with pytest.raises(StoreError) as caught:
        reads.read(request("SELECT 1 AS one", gone), gone.committed_at)
    assert caught.value.details["reason"] == SNAPSHOT_CHANGED


# --- budgets (law 7) -------------------------------------------------------------------------


def test_a_read_past_its_time_limit_is_interrupted(reads: LakeReads) -> None:
    snapshot = reads.latest()
    slow = "SELECT count(*) AS c FROM range(100000000000) AS a WHERE a.range % 7 = 3"
    with pytest.raises(StoreError) as caught:
        reads.read(request(slow, snapshot, timeout_ms=200), snapshot.committed_at)
    assert caught.value.code == "STORE_TIMEOUT"
    assert rows(reads, "SELECT count(*) AS c FROM readings") == [{"c": 3}]  # usable after


def test_a_read_over_the_instance_memory_limit_fails_and_leaves_it_usable(
    made: tuple[Path, Path],
) -> None:
    with ExistingLake(attachment(*made), memory_limit="64MB", threads=1) as lake:
        reads = LakeReads(lake, timeout_ms=TIMEOUT_MS)
        snapshot = reads.latest()
        heavy = "SELECT list(range) AS l FROM range(100000000)"
        with pytest.raises(StoreError) as caught:
            reads.read(request(heavy, snapshot), snapshot.committed_at)
        assert caught.value.code == "STORE_QUERY_FAILED"
        assert "memory" in str(caught.value.details.get("duckdb", "")).lower()
        assert rows(reads, "SELECT count(*) AS c FROM readings") == [{"c": 3}]


# --- what the model is compiled against ------------------------------------------------------


def test_the_tables_and_views_at_a_snapshot(reads: LakeReads) -> None:
    snapshot = reads.latest()
    tables = reads.table_columns(snapshot)
    found = {(t.schema_, t.table): [(c.name, c.type) for c in t.columns] for t in tables.tables}
    assert found == {
        ("main", "readings"): [("reading_id", "BIGINT"), ("kwh", "DECIMAL(18,4)")],
        ("ops", "meters"): [("meter_id", "BIGINT")],
    }
    assert reads.views(snapshot) == frozenset({"main.big_readings"})


# --- verification against the set as it stands now (PR 2, finding 4) ------------------------


def test_a_protected_location_registered_after_the_attach_refuses_the_next_verification(
    made: tuple[Path, Path], reads: LakeReads, tmp_path: Path
) -> None:
    catalog, data = made
    snapshot = reads.latest()
    assert reads.verify(snapshot, attachment(catalog, data)) == snapshot
    later = attachment(catalog, data, [data / "managed-of-a-new-project"])
    with pytest.raises(StoreError) as caught:
        reads.verify(snapshot, later)
    assert caught.value.code == "STORE_SOURCE_INVALID"


def test_verification_with_another_lakes_attachment_or_no_protected_set_is_refused(
    made: tuple[Path, Path], reads: LakeReads, tmp_path: Path
) -> None:
    catalog, data = made
    snapshot = reads.latest()
    other = attachment(catalog, data).model_copy(update={"name": "collections"})
    missing = attachment(catalog, data).model_copy(update={"protected_locations": None})
    for current in (other, missing):
        with pytest.raises(StoreError) as caught:
            reads.verify(snapshot, current)
        assert caught.value.code == "STORE_SOURCE_INVALID"


def test_a_catalog_role_used_by_a_connection_added_later_refuses_the_next_verification(
    postgres: Catalog,
) -> None:
    made = postgres.attachment()
    with ExistingLake(made, memory_limit=MEMORY, threads=1) as lake:
        reads = LakeReads(lake, timeout_ms=TIMEOUT_MS)
        snapshot = reads.latest()
        assert reads.verify(snapshot, made) == snapshot
        later = made.model_copy(update={"roles_in_use": [RolesInUseItem(postgres.reader)]})
        with pytest.raises(StoreError) as caught:
            reads.verify(snapshot, later)
        assert caught.value.code == "STORE_SOURCE_INVALID"
        found = reads.read(
            request('SELECT SUM("kwh") AS t FROM "readings"', snapshot), snapshot.committed_at
        ).read_all()
        assert found.to_pylist() == [{"t": Decimal("3.7500")}]


def test_a_postgres_lake_refuses_postgres_query_on_its_catalog_connection(
    postgres: Catalog,
) -> None:
    with ExistingLake(postgres.attachment(), memory_limit=MEMORY, threads=1) as lake:
        reads = LakeReads(lake, timeout_ms=TIMEOUT_MS)
        for sql in (
            "SELECT * FROM postgres_query('__ducklake_metadata_lake', 'SELECT 1')",
            "CALL postgres_execute('__ducklake_metadata_lake', 'SELECT 1')",
        ):
            error = refused(reads, sql)
            assert error.code == "STORE_STATEMENT_INVALID"


# --- Azure only over HTTPS --------------------------------------------------------------------


@pytest.mark.parametrize(
    "connection_string",
    [
        "DefaultEndpointsProtocol=http;AccountName=acct;AccountKey=a2V5",
        "AccountName=acct;AccountKey=a2V5;BlobEndpoint=http://blob.example.net",
        "AccountName=acct;AccountKey=a2V5;BlobEndpoint=blob.example.net",
    ],
)
def test_an_existing_lake_on_azure_is_reached_only_over_https(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, connection_string: str
) -> None:
    monkeypatch.setenv("LAKE_AZURE", connection_string)
    made = ExistingLakeAttachment(
        kind="existing_lake_attachment",
        name="billing",
        location=SqliteLakeLocation(
            kind="lake_location",
            catalog="sqlite",
            path=str(tmp_path / "catalog.sqlite"),
            data_path="az://lake/data/",
            storage=ObjectStorage(AzureStorage(type="azure", connection_string_env="LAKE_AZURE")),
        ),
        roles_in_use=[],
        protected_files=[],
        protected_locations=[ProtectedLocation(str(tmp_path / "project"))],
        credentials_in_use=[],
    )
    with pytest.raises(StoreError) as caught:
        ExistingLake(made, memory_limit=MEMORY, threads=1).close()
    assert caught.value.code == "STORE_TLS_REQUIRED"
    assert "a2V5" not in caught.value.message


def test_reads_need_a_time_limit(made: tuple[Path, Path]) -> None:
    with ExistingLake(attachment(*made), memory_limit=MEMORY, threads=1) as lake:
        with pytest.raises(StoreError) as caught:
            LakeReads(lake, timeout_ms=0)
        assert caught.value.code == "STORE_LIMITS_REQUIRED"


UHUGEINT_MAX = "340282366920938463463374607431768211455"


@pytest.mark.parametrize(
    "column",
    [
        f"[CAST('{UHUGEINT_MAX}' AS UHUGEINT)]",
        f"{{'a': CAST('{UHUGEINT_MAX}' AS UHUGEINT)}}",
    ],
    ids=["list", "struct"],
)
def test_a_nested_uhugeint_is_refused_never_wrapped(reads: LakeReads, column: str) -> None:
    found = refused(reads, f'SELECT {column} AS "v" FROM "readings" LIMIT 1')
    assert found.code == "STORE_TYPE_UNSUPPORTED"
