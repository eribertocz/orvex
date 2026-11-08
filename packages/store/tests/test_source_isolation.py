"""#372: the source isolation boundary. A database source is read only in a worker process of
its own, where the lake does not exist. The lake holds a canary table, with a random name and a
random value, that no source may reach; each attack below is SQL a source brings with it."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
from orvex.contracts.store.v1 import (
    FileSourceAttachment,
    LocalLakeLocation,
    ReadRequest,
    SqliteSourceAttachment,
    StatementRequest,
)
from orvex.store import source_worker
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, open_lake

LIMITS = Limits(
    memory_limit="1GB", threads=2, max_concurrent=8, queue_size=64, default_timeout_ms=60_000
)
CANARY_VALUE = f"canary-{uuid.uuid4().hex}"
CANARY_TABLE = f"sueldos_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def lake_folder(tmp_path: Path) -> Path:
    return tmp_path / "lake"


def duckdb_source(path: Path, name: str = "ventas") -> FileSourceAttachment:
    return FileSourceAttachment(kind="source_attachment", name=name, type="duckdb", path=str(path))


def sqlite_source(path: Path, name: str = "ventas") -> SqliteSourceAttachment:
    return SqliteSourceAttachment(
        kind="source_attachment", name=name, type="sqlite", path=str(path)
    )


def opened(lake_folder: Path, *sources: object, limits: Limits = LIMITS) -> Lake:
    location = LocalLakeLocation(kind="lake_location", catalog="local", path=str(lake_folder))
    lake = open_lake(location, limits, list(sources))  # type: ignore[arg-type]
    with lake.transaction() as transaction:
        transaction.execute(
            StatementRequest(
                kind="statement",
                sql=f"CREATE OR REPLACE TABLE {CANARY_TABLE} AS SELECT '{CANARY_VALUE}' AS salary",
                parameters=[],
            )
        )
    return lake


def file_with(path: Path, *statements: str) -> Path:
    """A source file built as in #259: on the attacker's machine, against a fake `lake` (and a
    fake catalog database) with the victim's names, then detached, so its views bind there."""
    with duckdb.connect(str(path)) as writer:
        writer.sql("ATTACH ':memory:' AS lake")
        writer.sql(f"CREATE TABLE lake.main.{CANARY_TABLE} AS SELECT 'fake' AS salary")
        writer.sql("CREATE TABLE lake.main.base AS SELECT 0 AS id")
        writer.sql("ATTACH ':memory:' AS __ducklake_metadata_lake")
        writer.sql("CREATE TABLE __ducklake_metadata_lake.ducklake_table AS SELECT 0 AS x")
        writer.sql(f"CREATE TABLE main.{CANARY_TABLE} AS SELECT 'own' AS salary")
        writer.sql("CREATE TABLE base AS SELECT 1 AS id")
        for statement in statements:
            writer.sql(statement)
        writer.sql("DETACH lake")
        writer.sql("DETACH __ducklake_metadata_lake")
    return path


def land(lake: Lake, sql: str, table: str = "landed", source: str = "ventas") -> None:
    with lake.transaction() as transaction:
        transaction.land(source, sql, [], "main", table)


def rows(lake: Lake, sql: str) -> list[dict[str, object]]:
    request = ReadRequest(
        kind="read", sql=sql, parameters=[], snapshot=lake.current_snapshot().id, batch_size=1024
    )
    found: list[dict[str, object]] = lake.read(request).read_all().to_pylist()
    return found


def canary_in(text: str, lake_folder: Path) -> bool:
    """A trace of the lake: the canary's value, the lake's folder, or its session's spill folder.
    (The canary's name is the attacker's own text: a view that names it shows it.)"""
    return any(secret in text for secret in (CANARY_VALUE, str(lake_folder), "lake-spill-"))


def assert_unseen(lake: Lake, lake_folder: Path, sql: str) -> None:
    """Landing `sql` either fails without naming the canary, or lands rows that hold no trace of
    the lake; and the lake is unchanged but for that table."""
    failure: StoreError | None = None
    try:
        land(lake, sql)
    except StoreError as error:
        failure = error
    if failure is not None:
        assert not canary_in(f"{failure} {failure.details}", lake_folder), failure.details
        return
    landed = json.dumps(rows(lake, "SELECT * FROM landed"), default=str)
    assert not canary_in(landed, lake_folder), landed
    assert "fake" not in landed  # nothing the attacker's stand-in held: it is not there either


# --- the lake is invisible from the source ---------------------------------------------------

REFERENCES = [
    f"SELECT * FROM lake.main.{CANARY_TABLE}",
    f'SELECT * FROM "lake"."main"."{CANARY_TABLE}"',
    f"SELECT * FROM {CANARY_TABLE}",
    f"SELECT * FROM main.{CANARY_TABLE}",
    "SELECT * FROM __ducklake_metadata_lake.ducklake_table",
    "SELECT * FROM lake.main.base",
]
METADATA = [
    "SELECT * FROM duckdb_tables()",
    "SELECT * FROM duckdb_columns()",
    "SELECT * FROM duckdb_views()",
    "SELECT * FROM duckdb_databases()",
    "SELECT name, value FROM duckdb_settings()",
    "SELECT * FROM duckdb_secrets()",
    "SELECT current_setting('temp_directory') AS t",
    "SELECT current_setting('home_directory') AS h",
    f"SELECT * FROM query('SELECT * FROM lake.main.{CANARY_TABLE}')",
    f"SELECT * FROM query_table('lake.main.{CANARY_TABLE}')",
]


@pytest.mark.parametrize("attack", REFERENCES + METADATA)
def test_a_duckdb_view_never_reaches_the_lake(
    lake_folder: Path, tmp_path: Path, attack: str
) -> None:
    source = file_with(tmp_path / "ventas.duckdb", f"CREATE VIEW resumen AS {attack}")
    lake = opened(lake_folder, duckdb_source(source))
    try:
        assert_unseen(lake, lake_folder, "SELECT * FROM ventas.resumen")
        assert rows(lake, f"SELECT salary FROM {CANARY_TABLE}") == [{"salary": CANARY_VALUE}]
    finally:
        lake.close()


@pytest.mark.parametrize("attack", REFERENCES)
def test_a_duckdb_macro_never_reaches_the_lake(
    lake_folder: Path, tmp_path: Path, attack: str
) -> None:
    source = file_with(
        tmp_path / "ventas.duckdb",
        f"CREATE MACRO leak() AS TABLE {attack}",
        "CREATE VIEW nested AS SELECT * FROM (SELECT * FROM leak())",
    )
    lake = opened(lake_folder, duckdb_source(source))
    try:
        assert_unseen(lake, lake_folder, "SELECT * FROM ventas.nested")
    finally:
        lake.close()


def test_a_generated_column_reads_only_the_worker(lake_folder: Path, tmp_path: Path) -> None:
    source = file_with(
        tmp_path / "ventas.duckdb",
        "CREATE TABLE leaky (id INTEGER, info VARCHAR GENERATED ALWAYS AS "
        "(current_setting('temp_directory') || current_setting('home_directory')) VIRTUAL)",
        "INSERT INTO leaky (id) VALUES (1)",
    )
    lake = opened(lake_folder, duckdb_source(source))
    try:
        assert_unseen(lake, lake_folder, "SELECT id, info FROM ventas.leaky")
    finally:
        lake.close()


def test_files_of_the_lake_are_unreadable_from_the_source(
    lake_folder: Path, tmp_path: Path
) -> None:
    lake = opened(lake_folder)  # first, so its files exist
    lake.close()
    catalog = next(lake_folder.rglob("*.duckdb"), lake_folder / "catalog.duckdb")
    near = tmp_path / "beside.duckdb"
    near.symlink_to(catalog)
    attacks = [
        f"SELECT * FROM read_text('{catalog}')",
        f"SELECT * FROM read_blob('{catalog}')",
        f"SELECT * FROM glob('{lake_folder}/**')",
        f"SELECT * FROM read_text('{tmp_path}/../{tmp_path.name}/lake/{catalog.name}')",
        f"SELECT * FROM read_text('{near}')",
    ]
    for index, attack in enumerate(attacks):
        source = file_with(tmp_path / f"ventas{index}.duckdb", f"CREATE VIEW leak AS {attack}")
        lake = opened(lake_folder, duckdb_source(source))
        try:
            with pytest.raises(StoreError) as refused:
                land(lake, "SELECT * FROM ventas.leak")
            # Refused by the worker's sandbox (the path in the message is the attacker's own).
            assert refused.value.code == "STORE_QUERY_FAILED"
            assert CANARY_VALUE not in str(refused.value.details)
        finally:
            lake.close()


def test_a_sqlite_view_never_reaches_the_lake(lake_folder: Path, tmp_path: Path) -> None:
    path = tmp_path / "ventas.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE base (id INTEGER)")
    connection.execute("INSERT INTO base VALUES (1)")
    connection.execute(f"CREATE VIEW leak AS SELECT * FROM {CANARY_TABLE}")
    connection.execute("CREATE VIEW nested AS SELECT * FROM leak")
    connection.commit()
    connection.close()
    lake = opened(lake_folder, sqlite_source(path))
    try:
        for view in ("leak", "nested"):
            assert_unseen(lake, lake_folder, f"SELECT * FROM ventas.{view}")
        land(lake, "SELECT * FROM ventas.base")
        assert rows(lake, "SELECT id FROM landed") == [{"id": 1}]
    finally:
        lake.close()


def test_the_model_sql_itself_cannot_reach_the_lake(lake_folder: Path, tmp_path: Path) -> None:
    source = file_with(tmp_path / "ventas.duckdb")
    lake = opened(lake_folder, duckdb_source(source))
    try:
        for attack in REFERENCES + METADATA:
            assert_unseen(lake, lake_folder, attack)
    finally:
        lake.close()


def test_the_worker_is_locked_before_the_source_runs(lake_folder: Path, tmp_path: Path) -> None:
    """The settings a source sees from inside its worker: no file or network access, the
    configuration locked, UTC and lossless Arrow (#372 §1 order)."""
    source = file_with(
        tmp_path / "ventas.duckdb",
        "CREATE VIEW settings AS SELECT "
        "current_setting('enable_external_access') AS external, "
        "current_setting('lock_configuration') AS locked, "
        "current_setting('arrow_lossless_conversion') AS lossless, "
        "current_setting('TimeZone') AS zone, "
        "current_setting('autoload_known_extensions') AS autoload",
    )
    lake = opened(lake_folder, duckdb_source(source))
    try:
        land(lake, "SELECT * FROM ventas.settings")
        assert rows(lake, "SELECT * FROM landed") == [
            {"external": False, "locked": True, "lossless": True, "zone": "UTC", "autoload": False}
        ]
    finally:
        lake.close()


# --- exact types across the stream -----------------------------------------------------------

TYPES = (
    "SELECT 170141183460469231731687303715884105727::HUGEINT AS h_max, "
    "'-170141183460469231731687303715884105728'::HUGEINT AS h_min, "
    "340282366920938463463374607431768211455::UHUGEINT AS uh_max, 0::UHUGEINT AS uh_min, "
    "18446744073709551615::UBIGINT AS ub, 9007199254740993::BIGINT AS big, "
    "12345678901234567890.1234567890::DECIMAL(38,10) AS d38, "
    "TIMESTAMPTZ '2026-03-08 02:30:00.123456+00' AS tz, "
    "'8f8c1b5e-6a6b-4b3e-9a43-0b1f2b3c4d5e'::UUID AS u, "
    "INTERVAL '1 month 2 days 3.000004 seconds' AS iv, [1, NULL, 3]::INTEGER[] AS l, "
    "{'a': 1, 'b': 'x'} AS s, MAP {'k': 2} AS m, NULL::DECIMAL(18,2) AS n, "
    "'x'::ENUM('x', 'y') AS e"
)


def test_every_type_lands_exactly(lake_folder: Path, tmp_path: Path) -> None:
    source = file_with(tmp_path / "ventas.duckdb", f"CREATE VIEW typed AS {TYPES}")
    lake = opened(lake_folder, duckdb_source(source))
    try:
        land(lake, "SELECT * FROM ventas.typed")
        request = ReadRequest(
            kind="read",
            sql="SELECT CAST(COLUMNS(*) AS VARCHAR) FROM landed",
            parameters=[],
            snapshot=lake.current_snapshot().id,
            batch_size=16,
        )
        landed = lake.read(request).read_all().to_pylist()
        described = lake.describe("SELECT * FROM landed")
    finally:
        lake.close()
    with duckdb.connect() as reference:
        reference.sql("SET TimeZone = 'UTC'")
        relation = reference.sql(TYPES)
        expected_types = [
            "VARCHAR" if str(kind).startswith("ENUM(") else str(kind) for kind in relation.types
        ]
        expected = reference.sql(f"SELECT CAST(COLUMNS(*) AS VARCHAR) FROM ({TYPES})").fetchall()
    assert [kind for _, kind in described] == expected_types
    assert [tuple(row.values()) for row in landed] == expected


def test_a_type_that_does_not_arrive_exactly_is_refused(
    lake_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked at both ends: a stream whose types are not the source's own (here, the default,
    lossy export: UHUGEINT as DECIMAL(38,0)) lands nothing."""
    source = file_with(
        tmp_path / "ventas.duckdb",
        "CREATE VIEW big AS SELECT 340282366920938463463374607431768211455::UHUGEINT AS uh",
    )
    original = source_worker.start

    def lossy(*args: object, **kwargs: object) -> source_worker.SourceStream:
        stream = original(*args, **kwargs).open()  # type: ignore[arg-type]
        stream.types = [("uh", "UHUGEINT")]
        table = pa.table({"uh": pa.array([-1], pa.decimal128(38, 0))})
        stream._reader = table.to_reader()  # what DuckDB's default export sends
        return stream

    monkeypatch.setattr(source_worker, "start", lossy)
    lake = opened(lake_folder, duckdb_source(source))
    try:
        with pytest.raises(StoreError) as refused:
            land(lake, "SELECT * FROM ventas.big")
        assert refused.value.code == "UNSUPPORTED_SOURCE_TYPE"
        assert refused.value.details == {"column": "ventas.uh"}
        assert ("main", "landed") not in lake.tables()
    finally:
        lake.close()


# --- robustness: one run fails, the lake does not --------------------------------------------


def left_behind(lake: Lake) -> list[str]:
    """What the lake's workers left in their folder (the lake's own spill folder): nothing, in
    every outcome (#372 §1)."""
    return sorted(path.name for path in Path(lake._workers).iterdir())


@pytest.mark.parametrize("damage", ["random", "truncated"])
def test_a_corrupt_duckdb_file_fails_that_read_only(
    lake_folder: Path, tmp_path: Path, damage: str
) -> None:
    path = file_with(
        tmp_path / "ventas.duckdb", "CREATE TABLE more AS SELECT range FROM range(5000)"
    )
    data = path.read_bytes()
    path.write_bytes(os.urandom(len(data)) if damage == "random" else data[: len(data) // 3])
    lake = opened(lake_folder, duckdb_source(path))
    try:
        with pytest.raises(StoreError) as refused:
            land(lake, "SELECT * FROM ventas.base")
        assert refused.value.code in {
            "STORE_SOURCE_INVALID",
            "STORE_SOURCE_FAILED",
            "STORE_QUERY_FAILED",
        }
        assert rows(lake, f"SELECT salary FROM {CANARY_TABLE}") == [{"salary": CANARY_VALUE}]
        assert left_behind(lake) == []
    finally:
        lake.close()


def test_a_corrupt_sqlite_file_is_refused(lake_folder: Path, tmp_path: Path) -> None:
    path = tmp_path / "ventas.sqlite"
    path.write_bytes(b"SQLite format 3\x00" + os.urandom(4096))
    with pytest.raises(StoreError) as refused:
        opened(lake_folder, sqlite_source(path))
    assert refused.value.code == "STORE_SOURCE_INVALID"


def test_a_view_that_exhausts_memory_fails_that_read_only(
    lake_folder: Path, tmp_path: Path
) -> None:
    source = file_with(
        tmp_path / "ventas.duckdb",
        "CREATE VIEW greedy AS SELECT list(range) AS l FROM range(400000000)",
    )
    small = Limits(
        memory_limit="128MB", threads=1, max_concurrent=8, queue_size=64, default_timeout_ms=600_000
    )
    lake = opened(lake_folder, duckdb_source(source), limits=small)
    try:
        with pytest.raises(StoreError) as refused:
            land(lake, "SELECT * FROM ventas.greedy")
        assert refused.value.code in {"STORE_QUERY_FAILED", "STORE_SOURCE_FAILED"}
        land(lake, "SELECT * FROM ventas.base", "after")
        assert rows(lake, "SELECT id FROM after") == [{"id": 1}]
    finally:
        lake.close()


def test_a_view_that_runs_forever_is_stopped_at_the_limit(
    lake_folder: Path, tmp_path: Path
) -> None:
    source = file_with(
        tmp_path / "ventas.duckdb",
        "CREATE VIEW endless AS SELECT count(*) AS n FROM range(100000000000000)",
    )
    lake = opened(lake_folder, duckdb_source(source))
    started = time.monotonic()
    try:
        with lake.transaction() as transaction, pytest.raises(StoreError) as refused:
            transaction.land("ventas", "SELECT * FROM ventas.endless", [], "main", "x", 2_000)
    except StoreError as rolled_back:
        refused_code = rolled_back.code
    else:
        refused_code = refused.value.code
    finally:
        leftovers = left_behind(lake)
        lake.close()
    assert leftovers == []
    assert refused_code == "STORE_TIMEOUT"
    assert time.monotonic() - started < 30


def cancelled_land(lake: Lake) -> None:
    with lake.transaction() as transaction:
        threading.Timer(1.0, transaction.cancel).start()
        transaction.land("ventas", "SELECT * FROM ventas.endless", [], "main", "x")


def test_a_cancelled_land_ends_its_worker(lake_folder: Path, tmp_path: Path) -> None:
    source = file_with(
        tmp_path / "ventas.duckdb",
        "CREATE VIEW endless AS SELECT count(*) AS n FROM range(100000000000000)",
    )
    lake = opened(lake_folder, duckdb_source(source))
    workers: list[source_worker.SourceStream] = []
    original = source_worker.start

    def recorded(*args: object, **kwargs: object) -> source_worker.SourceStream:
        stream = original(*args, **kwargs)  # type: ignore[arg-type]
        workers.append(stream)
        return stream

    try:
        source_worker.start = recorded
        with pytest.raises(StoreError) as refused:
            cancelled_land(lake)
        assert left_behind(lake) == []
    finally:
        source_worker.start = original
        lake.close()
    assert refused.value.code == "STORE_CANCELLED"
    assert workers
    assert workers[0]._process.poll() is not None  # the worker is gone


def test_a_worker_killed_mid_stream_lands_nothing(
    lake_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = file_with(
        tmp_path / "ventas.duckdb", "CREATE TABLE many AS SELECT range AS id FROM range(2000000)"
    )
    original = source_worker.SourceStream.batches

    def dying(self: source_worker.SourceStream) -> Iterator[pa.RecordBatch]:
        for index, batch in enumerate(original(self)):
            if index == 1:
                self._process.kill()
            yield batch

    monkeypatch.setattr(source_worker.SourceStream, "batches", dying)
    lake = opened(lake_folder, duckdb_source(source))
    try:
        with pytest.raises(StoreError) as refused:
            land(lake, "SELECT * FROM ventas.many")
        assert refused.value.code == "STORE_SOURCE_FAILED"
        assert ("main", "landed") not in lake.tables()
        assert left_behind(lake) == []
    finally:
        lake.close()


def test_a_malformed_stream_lands_nothing(
    lake_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = file_with(tmp_path / "ventas.duckdb")
    real = subprocess.Popen

    def garbage(args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        if "-m" in args and source_worker.__name__ in args:
            return real(  # type: ignore[call-overload,no-any-return]
                ["/bin/sh", "-c", "cat > /dev/null; printf 'not an arrow stream'"], **kwargs
            )
        return real(args, **kwargs)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(subprocess, "Popen", garbage)
    lake = opened(lake_folder, duckdb_source(source))
    try:
        with pytest.raises(StoreError) as refused:
            land(lake, "SELECT * FROM ventas.base")
        assert refused.value.code == "STORE_SOURCE_FAILED"
        assert ("main", "landed") not in lake.tables()
        assert left_behind(lake) == []
    finally:
        lake.close()


def test_streaming_stays_bounded_by_the_batch(lake_folder: Path, tmp_path: Path) -> None:
    """A source larger than the lake's memory limit lands: the stream is never held whole."""
    source = file_with(
        tmp_path / "ventas.duckdb",
        "CREATE TABLE wide AS SELECT range AS id, repeat('x', 200) AS pad FROM range(3000000)",
    )
    small = Limits(
        memory_limit="256MB", threads=2, max_concurrent=8, queue_size=64, default_timeout_ms=600_000
    )
    lake = opened(lake_folder, duckdb_source(source), limits=small)
    try:
        land(lake, "SELECT * FROM ventas.wide")
        assert rows(lake, "SELECT count(*) AS n FROM landed") == [{"n": 3_000_000}]
    finally:
        lake.close()


def test_describing_and_landing_leave_nothing_behind(lake_folder: Path, tmp_path: Path) -> None:
    """The parent removes each worker's folder, also when it ends a worker itself (describing
    stops a worker once it has the schema): no copy of a source stays on disk."""
    path = tmp_path / "ventas.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE base (id INTEGER, secret TEXT)")
    connection.executemany("INSERT INTO base VALUES (?, 'private')", [(i,) for i in range(20000)])
    connection.commit()
    connection.close()
    lake = opened(lake_folder, sqlite_source(path))
    try:
        for _ in range(3):
            assert lake.describe_source("ventas", "SELECT * FROM ventas.base") == [
                ("id", "BIGINT"),
                ("secret", "VARCHAR"),
            ]
        land(lake, "SELECT * FROM ventas.base")
        assert left_behind(lake) == []
    finally:
        lake.close()


def test_the_worker_gets_its_own_share_of_the_limits(
    lake_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a database source, the lake's limits are split (#372 §1): the worker gets half of
    the memory and of the threads, and the lake keeps the rest."""
    source = file_with(tmp_path / "ventas.duckdb")
    given: list[tuple[str, int]] = []
    original = source_worker.start

    def recorded(*args: object, **kwargs: object) -> source_worker.SourceStream:
        given.append((str(kwargs["memory_limit"]), int(str(kwargs["threads"]))))
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(source_worker, "start", recorded)
    lake = opened(lake_folder, duckdb_source(source))
    try:
        land(lake, "SELECT * FROM ventas.base")
        # Read on the lake's own connection: SQL may not read settings (#256).
        cursor = lake._cursor()
        (setting,) = cursor.execute("SELECT current_setting('memory_limit')").fetchone() or ("",)
        cursor.close()
        lake_memory = [{"m": setting}]
    finally:
        lake.close()
    total = source_worker.memory_bytes(str(LIMITS.memory_limit))
    ((memory, threads),) = given
    # DuckDB reports a limit rounded to 0.1 MiB: compared within that.
    assert abs(source_worker.memory_bytes(memory) - total // 2) < 2**20
    assert threads == 1
    assert lake_memory  # the lake runs on the other half (DuckDB reports it rounded)
    assert source_worker.memory_bytes(str(lake_memory[0]["m"]).replace(" ", "")) <= total


def test_the_configuration_is_applied_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#372 §1: limits and spill, UTC and lossless Arrow, the source's extension, the attach,
    then the sandbox and the lock, before any statement of the source."""
    path = file_with(tmp_path / "ventas.duckdb")
    seen: list[str] = []
    connect = duckdb.connect

    class Recording:
        def __init__(self, inner: duckdb.DuckDBPyConnection) -> None:
            self.inner = inner

        def execute(self, sql: str, *rest: object) -> object:
            seen.append(sql)
            return self.inner.execute(sql, *rest)

        def __getattr__(self, name: str) -> object:
            return getattr(self.inner, name)

    def recording(*args: object, **kwargs: object) -> object:
        return Recording(connect(*args, **kwargs))  # type: ignore[arg-type]

    monkeypatch.setattr(duckdb, "connect", recording)
    folder = tmp_path / "worker"
    folder.mkdir()
    connection, _, _ = source_worker._open(duckdb_source(path), "", "1GB", 1, folder)
    connection.close()

    def at(prefix: str) -> int:
        return next(i for i, sql in enumerate(seen) if sql.startswith(prefix))

    order = [
        "SET temp_directory",
        "SET TimeZone",
        "SET arrow_lossless_conversion",
        "ATTACH",
        "USE",
        "SET allowed_paths",
        "SET enable_external_access = false",
        "SET lock_configuration = true",
    ]
    positions = [at(step) for step in order]
    assert positions == sorted(positions), list(zip(order, positions, strict=True))
    assert at("SET lock_configuration = true") == len(seen) - 1


def test_a_failure_before_the_guard_still_ends_the_worker(
    lake_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything raised between starting the worker and guarding the read (a Ctrl-C, say) ends
    the worker and frees its slot: the next read starts at once."""
    from orvex.store import lake as lake_module

    source = file_with(tmp_path / "ventas.duckdb")
    lake = opened(lake_folder, duckdb_source(source))
    real = lake_module.Guard

    class Interrupted(Exception):
        pass

    def failing(*args: object, **kwargs: object) -> object:
        raise Interrupted

    try:
        monkeypatch.setattr(lake_module, "Guard", failing)
        with pytest.raises(Interrupted):
            land(lake, "SELECT * FROM ventas.base")
        monkeypatch.setattr(lake_module, "Guard", real)
        assert left_behind(lake) == []
        started = time.monotonic()
        land(lake, "SELECT * FROM ventas.base")
        assert time.monotonic() - started < 30
    finally:
        lake.close()
