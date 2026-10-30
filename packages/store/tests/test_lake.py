"""P1-01 and P1-02: store contract v1 on DuckLake, local and PostgreSQL catalogs."""

from __future__ import annotations

import decimal
import json
import threading
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
from orvex.contracts.store.v1 import LocalLakeLocation, Parameter, ReadRequest, StatementRequest
from orvex.store.errors import StoreError
from orvex.store.json_encoding import arrow_to_json
from orvex.store.lake import Lake, Limits, open_lake

# The shared contract tests run on every catalog, SQLite included (#120).
EVERY_CATALOG = True

LIMITS = Limits(
    memory_limit="1GB", threads=2, max_concurrent=8, queue_size=64, default_timeout_ms=600_000
)


def statement(sql: str, *parameters: tuple[str, str, str | None]) -> StatementRequest:
    return StatementRequest(
        kind="statement",
        sql=sql,
        parameters=[Parameter(name=n, type=t, value=v) for n, t, v in parameters],  # type: ignore[arg-type]
    )


def read(
    lake: Lake,
    sql: str,
    snapshot: int | None = None,
    *parameters: tuple[str, str, str | None],
    batch_size: int = 65536,
) -> pa.Table:
    pinned = lake.current_snapshot().id if snapshot is None else snapshot
    request = ReadRequest(
        kind="read",
        sql=sql,
        parameters=[Parameter(name=n, type=t, value=v) for n, t, v in parameters],  # type: ignore[arg-type]
        snapshot=pinned,
        batch_size=batch_size,
    )
    return lake.read(request).read_all()


def write(lake: Lake, *statements: StatementRequest) -> int | None:
    with lake.transaction() as transaction:
        for item in statements:
            transaction.execute(item)
    assert transaction.result is not None
    return transaction.result.snapshot.id if transaction.result.snapshot else None


def test_commit_creates_exactly_one_snapshot(lake: Lake) -> None:
    before = lake.current_snapshot().id
    created = write(
        lake,
        statement("CREATE TABLE a AS SELECT 1 AS x"),
        statement("CREATE TABLE b AS SELECT 2 AS y"),
    )
    assert created is not None
    assert created == lake.current_snapshot().id
    assert created == before + 1


def test_transaction_without_changes_creates_no_snapshot(lake: Lake) -> None:
    write(lake, statement("CREATE TABLE a AS SELECT 1 AS x"))
    before = lake.current_snapshot().id
    assert write(lake, statement("DELETE FROM a WHERE x = 99")) is None
    assert write(lake) is None
    assert lake.current_snapshot().id == before


def test_exception_rolls_back(lake: Lake) -> None:
    write(lake, statement("CREATE TABLE a AS SELECT 1 AS x"))
    before = lake.current_snapshot().id

    def failing() -> None:
        with lake.transaction() as transaction:
            transaction.execute(statement("INSERT INTO a VALUES (2)"))
            raise RuntimeError("stop")

    with pytest.raises(RuntimeError):
        failing()
    assert lake.current_snapshot().id == before
    assert read(lake, "SELECT x FROM a ORDER BY x").column("x").to_pylist() == [1]


def test_read_is_pinned_to_its_snapshot(lake: Lake) -> None:
    first = write(
        lake,
        statement("CREATE TABLE a AS SELECT 1 AS id, 10 AS v"),
        statement("CREATE TABLE b AS SELECT 1 AS id, 'one' AS name"),
    )
    write(
        lake,
        statement("INSERT INTO a VALUES (2, 20)"),
        statement("UPDATE b SET name = 'uno'"),
        statement("INSERT INTO b VALUES (2, 'two')"),
    )
    sql = "WITH t AS (SELECT * FROM a) SELECT t.id, t.v, b.name FROM t JOIN b USING (id) ORDER BY 1"
    old = read(lake, sql, first)
    assert old.to_pylist() == [{"id": 1, "v": 10, "name": "one"}]
    new = read(lake, sql)
    assert new.to_pylist() == [{"id": 1, "v": 10, "name": "uno"}, {"id": 2, "v": 20, "name": "two"}]


def test_parameters_are_bound_not_concatenated(lake: Lake) -> None:
    hostile = "x'); DROP TABLE t; --"
    write(lake, statement("CREATE TABLE t (s VARCHAR)"))
    write(lake, statement("INSERT INTO t VALUES ($s)", ("s", "VARCHAR", hostile)))
    table = read(lake, "SELECT s FROM t WHERE s = $s", None, ("s", "VARCHAR", hostile))
    assert table.column("s").to_pylist() == [hostile]


def test_exact_types_reach_arrow(lake: Lake) -> None:
    write(
        lake,
        statement(
            "CREATE TABLE e AS SELECT "
            "12345678901234.5678::DECIMAL(18,4) AS d, "
            "170141183460469231731687303715884105727::HUGEINT AS h, "
            "18446744073709551615::UBIGINT AS u, "
            "TIMESTAMPTZ '2026-10-27 08:30:00-04' AS t "
            "UNION ALL SELECT NULL, NULL, NULL, NULL"
        ),
    )
    table = read(lake, "SELECT d, h, u, t FROM e ORDER BY d NULLS LAST")
    schema = table.schema
    assert schema.field("d").type == pa.decimal128(18, 4)
    assert schema.field("h").type == pa.decimal128(38, 0)
    assert schema.field("u").type == pa.uint64()
    assert schema.field("t").type == pa.timestamp("us", tz="UTC")
    first, second = table.to_pylist()
    assert first["d"] == decimal.Decimal("12345678901234.5678")
    assert first["h"] == decimal.Decimal(2**127 - 1)
    assert first["u"] == 2**64 - 1
    assert first["t"].isoformat() == "2026-10-27T12:30:00+00:00"
    assert second == {"d": None, "h": None, "u": None, "t": None}


def test_read_runs_the_statement_once(lake: Lake) -> None:
    lake._connection.sql("CREATE SEQUENCE memory.main.counter")
    table = read(
        lake, "SELECT nextval('memory.main.counter') AS v FROM range(10000)", batch_size=1000
    )
    assert sorted(table.column("v").to_pylist()) == list(range(1, 10001))
    current = lake._connection.execute("SELECT currval('memory.main.counter')").fetchone()
    assert current == (10000,)


def test_large_read_streams_batch_by_batch(tmp_path: Path) -> None:
    """About 4 GB of results under a 512 MB limit: only streaming can pass."""
    location = LocalLakeLocation(kind="lake_location", catalog="local", path=str(tmp_path))
    small = open_lake(
        location,
        Limits(
            memory_limit="512MB",
            threads=2,
            max_concurrent=8,
            queue_size=64,
            default_timeout_ms=600_000,
        ),
    )
    try:
        request = ReadRequest(
            kind="read",
            sql="SELECT range AS i, range * 2 AS j, repeat('x', 24) AS s FROM range(100000000)",
            parameters=[],
            snapshot=small.current_snapshot().id,
            batch_size=1048576,
        )
        rows = 0
        for batch in small.read(request):
            rows += batch.num_rows
            del batch
        assert rows == 100_000_000
    finally:
        small.close()


def code_of(action: object) -> str:
    assert callable(action)
    with pytest.raises(StoreError) as raised:
        action()
    return raised.value.code


def test_unknown_snapshot(lake: Lake) -> None:
    assert code_of(lambda: read(lake, "SELECT 1", 999)) == "STORE_SNAPSHOT_NOT_FOUND"


@pytest.mark.parametrize("sql", ["SELECT 1; SELECT 2", "DELETE FROM a", "CREATE TABLE z (i INT)"])
def test_read_needs_one_select(lake: Lake, sql: str) -> None:
    assert code_of(lambda: read(lake, sql)) == "STORE_STATEMENT_INVALID"


def test_transaction_rejects_control_statements(lake: Lake) -> None:
    def attempt() -> None:
        with lake.transaction() as transaction:
            transaction.execute(statement("ATTACH 'x.duckdb' AS x"))

    assert code_of(attempt) == "STORE_STATEMENT_INVALID"


def test_read_of_another_catalog_is_rejected(lake: Lake) -> None:
    assert code_of(lambda: read(lake, "SELECT * FROM memory.main.t")) == "STORE_STATEMENT_INVALID"


def test_duckdb_error_is_reported(lake: Lake) -> None:
    def attempt() -> None:
        read(lake, "SELECT no_such_column FROM range(1)")

    with pytest.raises(StoreError) as raised:
        attempt()
    assert raised.value.code == "STORE_QUERY_FAILED"
    assert "no_such_column" in raised.value.details["duckdb"]


@pytest.mark.parametrize(
    ("sql", "parameters"),
    [
        ("SELECT $a AS x", ()),
        ("SELECT 1 AS x", (("a", "INTEGER", "1"),)),
        ("SELECT $a AS x", (("a", "INTEGER", "1.5"),)),
        ("SELECT $a AS x", (("a", "TINYINT", "300"),)),
        ("SELECT $a AS x", (("a", "DATE", "2026-13-01"),)),
        ("SELECT $a AS x", (("a", "TIMESTAMPTZ", "2026-10-27 08:00:00"),)),
        ("SELECT $a AS x", (("a", "BOOLEAN", "yes"),)),
    ],
)
def test_invalid_parameters(
    lake: Lake, sql: str, parameters: tuple[tuple[str, str, str], ...]
) -> None:
    assert code_of(lambda: read(lake, sql, None, *parameters)) == "STORE_PARAMETER_INVALID"


@pytest.mark.parametrize(
    "limits",
    [
        Limits(None, 2, 1, 0, 1000),
        Limits("1GB", None, 1, 0, 1000),
        Limits("1GB", 2, None, 0, 1000),
        Limits("1GB", 2, 0, 0, 1000),
        Limits("1GB", 2, 1, None, 1000),
        Limits("1GB", 2, 1, 0, None),
        Limits("1GB", 2, 1, 0, 3_600_001),
    ],
)
def test_limits_are_required(tmp_path: Path, limits: Limits) -> None:
    location = LocalLakeLocation(kind="lake_location", catalog="local", path=str(tmp_path))
    assert code_of(lambda: open_lake(location, limits)) == "STORE_LIMITS_REQUIRED"


def test_missing_extension_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orvex.store.lake.EXTENSIONS", ("no_such_extension_xyz",))
    location = LocalLakeLocation(kind="lake_location", catalog="local", path=str(tmp_path))
    with pytest.raises(StoreError) as raised:
        open_lake(location, LIMITS)
    assert raised.value.code == "STORE_EXTENSION_UNAVAILABLE"
    assert raised.value.details["extension"] == "no_such_extension_xyz"


def test_errors_convert_to_the_contract_message() -> None:
    message = StoreError("STORE_QUERY_FAILED", "x", {"duckdb": "boom"}).to_message()
    assert message.code.root == "STORE_QUERY_FAILED"
    assert message.details[0].name == "duckdb"
    with pytest.raises(ValueError, match="NOT_A_CODE"):
        StoreError("NOT_A_CODE", "x", {})


def test_cte_named_like_a_lake_table_cannot_escape_the_snapshot(lake: Lake) -> None:
    first = write(lake, statement("CREATE TABLE a AS SELECT 1 AS x"))
    write(lake, statement("INSERT INTO a VALUES (2)"))
    sql = (
        "SELECT x FROM a WHERE x IN (WITH a AS (SELECT 1 AS x UNION ALL SELECT 2) SELECT x FROM a)"
    )
    assert read(lake, sql, first).column("x").to_pylist() == [1]
    assert sorted(read(lake, sql).column("x").to_pylist()) == [1, 2]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM query_table('a')",
        "SELECT * FROM query('SELECT * FROM a')",
        "SELECT * FROM read_parquet('data/*.parquet')",
    ],
)
def test_table_functions_that_could_bypass_the_snapshot_are_rejected(lake: Lake, sql: str) -> None:
    write(lake, statement("CREATE TABLE a AS SELECT 1 AS x"))
    assert code_of(lambda: read(lake, sql)) == "STORE_STATEMENT_INVALID"


def test_generator_table_functions_are_allowed(lake: Lake) -> None:
    sql = "SELECT count(*) AS n FROM range(3) CROSS JOIN generate_series(1, 2)"
    assert read(lake, sql).column("n").to_pylist() == [6]


def test_views_are_refused_and_rolled_back(lake: Lake) -> None:
    write(lake, statement("CREATE TABLE a AS SELECT 1 AS x"))
    before = lake.current_snapshot().id

    def attempt() -> None:
        write(lake, statement("CREATE VIEW v AS SELECT * FROM a"))

    assert code_of(attempt) == "STORE_STATEMENT_INVALID"
    assert lake.current_snapshot().id == before


def test_catalog_name_is_case_insensitive(lake: Lake) -> None:
    write(lake, statement("CREATE TABLE a AS SELECT 1 AS x"))
    assert read(lake, "SELECT x FROM LAKE.main.a").column("x").to_pylist() == [1]


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        ("DECIMAL(18,4)", "1.5", "DECIMAL(18,4)"),
        ("TINYINT", "7", "TINYINT"),
        ("UBIGINT", "1", "UBIGINT"),
    ],
)
def test_parameters_take_their_declared_type(
    lake: Lake, kind: str, value: str, expected: str
) -> None:
    table = read(lake, "SELECT typeof($a) AS t, $a AS v", None, ("a", kind, value))
    assert table.column("t").to_pylist() == [expected]


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("DECIMAL(18,4)", "NaN"),
        ("DECIMAL(18,4)", "Infinity"),
        ("DECIMAL(18,4)", "1E+50"),
        ("DECIMAL(4,2)", "123.456"),
        ("DECIMAL(4,2)", "123.4"),
        ("INTEGER", "1_000"),
        ("INTEGER", " 1"),
        ("DATE", "20261027"),
        ("DOUBLE", "inf"),
        ("DOUBLE", "1e400"),
    ],
)
def test_lenient_or_inexact_parameter_text_is_rejected(lake: Lake, kind: str, value: str) -> None:
    assert code_of(lambda: read(lake, "SELECT $a AS v", None, ("a", kind, value))) == (
        "STORE_PARAMETER_INVALID"
    )


def test_concurrent_transactions_report_their_own_snapshot(lake: Lake) -> None:
    write(lake, statement("CREATE TABLE a (writer INTEGER)"))
    results: dict[int, int | None] = {}

    def writer(number: int) -> None:
        results[number] = write(lake, statement(f"INSERT INTO a VALUES ({number})"))

    threads = [threading.Thread(target=writer, args=(number,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    snapshots = list(results.values())
    assert None not in snapshots
    assert len(set(snapshots)) == 8
    for number, snapshot in results.items():
        assert snapshot is not None
        rows = read(
            lake, "SELECT writer FROM a WHERE writer = $w", snapshot, ("w", "INTEGER", str(number))
        )
        assert rows.num_rows == 1
        before = read(
            lake,
            "SELECT writer FROM a WHERE writer = $w",
            snapshot - 1,
            ("w", "INTEGER", str(number)),
        )
        assert before.num_rows == 0


def test_error_while_streaming_is_a_store_error(lake: Lake) -> None:
    request = ReadRequest(
        kind="read",
        sql="SELECT CASE WHEN range = 5000000 THEN error('boom') ELSE range END AS i "
        "FROM range(10000000)",
        parameters=[],
        snapshot=lake.current_snapshot().id,
        batch_size=65536,
    )
    reader = lake.read(request)
    with pytest.raises(StoreError) as raised:
        for _ in reader:
            pass
    assert raised.value.code == "STORE_QUERY_FAILED"


def test_invalid_limits_are_rejected(tmp_path: Path) -> None:
    location = LocalLakeLocation(kind="lake_location", catalog="local", path=str(tmp_path))
    assert (
        code_of(lambda: open_lake(location, Limits("lots", 2, 1, 0, 1000)))
        == "STORE_LIMITS_REQUIRED"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE MACRO memory.main.s() AS (SELECT count(*) FROM lake.main.a)",
        "CREATE MACRO memory.main.t() AS TABLE SELECT * FROM lake.main.a",
    ],
)
def test_macros_are_refused_and_rolled_back(lake: Lake, sql: str) -> None:
    write(lake, statement("CREATE TABLE a AS SELECT 1 AS x"))
    before = lake.current_snapshot().id
    assert code_of(lambda: write(lake, statement(sql))) == "STORE_STATEMENT_INVALID"
    assert lake.current_snapshot().id == before


def test_nested_transaction_in_one_thread_is_refused(lake: Lake) -> None:
    def nested() -> None:
        with lake.transaction(), lake.transaction():
            pass

    assert code_of(nested) == "STORE_STATEMENT_INVALID"
    write(lake, statement("CREATE TABLE after_nested AS SELECT 1 AS x"))


def test_file_paths_in_from_are_rejected(lake: Lake, tmp_path: Path) -> None:
    write(lake, statement("CREATE TABLE a AS SELECT 1 AS x"))
    csv = tmp_path / "plain.csv"
    csv.write_text("x\n1\n", encoding="utf-8")
    for sql in (
        f"SELECT * FROM '{(tmp_path / 'data').as_posix()}/**/*.parquet'",
        f"SELECT * FROM '{csv.as_posix()}'",
    ):
        assert code_of(lambda sql=sql: read(lake, sql)) == "STORE_STATEMENT_INVALID"


def test_unknown_names_and_python_objects_are_rejected(lake: Lake) -> None:
    arrow_table = pa.table({"x": [1]})
    assert arrow_table.num_rows == 1
    assert code_of(lambda: read(lake, "SELECT * FROM arrow_table")) == "STORE_STATEMENT_INVALID"


def test_dropped_table_is_still_readable_at_its_snapshot(lake: Lake) -> None:
    alive = write(lake, statement("CREATE TABLE gone AS SELECT 7 AS x"))
    write(lake, statement("DROP TABLE gone"))
    assert read(lake, "SELECT x FROM gone", alive).column("x").to_pylist() == [7]
    assert code_of(lambda: read(lake, "SELECT x FROM gone")) == "STORE_STATEMENT_INVALID"


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TYPE memory.main.e AS ENUM (SELECT CAST(x AS VARCHAR) FROM lake.main.a)",
        "CREATE TABLE memory.main.copy AS SELECT * FROM lake.main.a",
        "CREATE TEMP TABLE copy AS SELECT * FROM lake.main.a",
        "CREATE SECRET s (TYPE s3, KEY_ID 'k', SECRET 'v')",
        "CREATE SEQUENCE q",
    ],
)
def test_objects_outside_lake_tables_are_refused(lake: Lake, sql: str) -> None:
    write(lake, statement("CREATE TABLE a AS SELECT 1 AS x"))
    before = lake.current_snapshot().id
    assert code_of(lambda: write(lake, statement(sql))) == "STORE_STATEMENT_INVALID"
    assert lake.current_snapshot().id == before


@pytest.mark.parametrize(
    "sql",
    [
        "DROP SCHEMA __ducklake_metadata_lake.other CASCADE",
        "DELETE FROM __ducklake_metadata_lake.ducklake_snapshot",
        "CREATE TABLE x AS SELECT * FROM postgres_query('__ducklake_metadata_lake', 'SELECT 1')",
        "INSERT INTO a SELECT 1 FROM query('SELECT 1')",
        "INSERT INTO a SELECT 1 FROM query/**/('SELECT * FROM __duck' || 'lake_metadata_lake.t')",
        "INSERT INTO a SELECT 1 FROM query_table -- comment\n('__duck' || 'lake_metadata_lake.t')",
        "INSERT INTO a SELECT 1 FROM json_execute_serialized_sql(json_serialize_sql('SELECT 1'))",
        "CREATE TABLE e AS SELECT content FROM read_text('/proc/self/environ')",
        "CREATE TABLE e AS SELECT * FROM read_csv('/etc/hostname')",
    ],
)
def test_writes_cannot_touch_the_catalog_or_run_outside_sql(lake: Lake, sql: str) -> None:
    write(lake, statement("CREATE TABLE a (x INTEGER)"))
    before = lake.current_snapshot().id
    assert code_of(lambda: write(lake, statement(sql))) == "STORE_STATEMENT_INVALID"
    assert lake.current_snapshot().id == before


def test_reads_cannot_touch_the_catalog(lake: Lake) -> None:
    sql = "SELECT * FROM __ducklake_metadata_lake.ducklake_snapshot"
    assert code_of(lambda: read(lake, sql)) == "STORE_STATEMENT_INVALID"


def test_files_outside_the_lake_cannot_be_read_by_writes(
    lake: Lake, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A file named in FROM is refused by the statement check, and behind it by the sandbox."""
    outside = tmp_path_factory.mktemp("outside") / "x.csv"
    outside.write_text("x\n1\n", encoding="utf-8")
    before = lake.current_snapshot().id
    sql = f"CREATE TABLE e AS SELECT * FROM '{outside.as_posix()}'"
    assert code_of(lambda: write(lake, statement(sql))) == "STORE_STATEMENT_INVALID"
    assert lake.current_snapshot().id == before
    with pytest.raises(duckdb.Error):  # the raw connection: only the sandbox decides here
        lake._connection.execute(f"SELECT * FROM '{outside.as_posix()}'").fetchall()


def test_lake_results_encode_exactly_to_json(lake: Lake) -> None:
    write(
        lake,
        statement(
            "CREATE TABLE j AS SELECT 340282366920938463463374607431768211455::UHUGEINT AS u, "
            "170141183460469231731687303715884105727::HUGEINT AS h, 12.50::DECIMAL(12,2) AS d"
        ),
    )
    request = ReadRequest(
        kind="read",
        sql="SELECT u, h, d FROM j",
        parameters=[],
        snapshot=lake.current_snapshot().id,
        batch_size=16,
    )
    document = json.loads("".join(arrow_to_json(lake.read(request))))
    assert document["columns"] == [
        {"name": "u", "type": "UHUGEINT"},
        {"name": "h", "type": "HUGEINT"},
        {"name": "d", "type": "DECIMAL(12,2)"},
    ]
    assert document["rows"] == [
        [
            "340282366920938463463374607431768211455",
            "170141183460469231731687303715884105727",
            "12.50",
        ]
    ]


@pytest.mark.parametrize("kind", ["DATE", "TIMESTAMP", "TIMESTAMPTZ"])
@pytest.mark.parametrize("value", ["infinity", "-infinity"])
def test_infinite_dates_and_times_are_parameters(lake: Lake, kind: str, value: str) -> None:
    table = read(lake, "SELECT CAST($a AS VARCHAR) AS v, isinf($a) AS i", None, ("a", kind, value))
    assert table.column("v").to_pylist() == [value]
    assert table.column("i").to_pylist() == [True]


def test_describe_binds_parameters_without_running(lake: Lake) -> None:
    write(lake, statement("CREATE TABLE a AS SELECT DATE '2024-01-01' AS d"))
    since = Parameter(name="since", type="DATE", value=None)  # type: ignore[arg-type]
    sql = "SELECT d, d + 1 AS next FROM a WHERE d > $since"
    assert lake.describe(sql, [since]) == [("d", "DATE"), ("next", "DATE")]
    assert code_of(lambda: lake.describe(sql)) == "STORE_PARAMETER_INVALID"


def test_summarize_and_dollar_quoted_files_are_refused_through_the_lake(
    lake: Lake, tmp_path: Path
) -> None:
    # #153: a read of SUMMARIZE would be unpinned, and a dollar-quoted file name was read as a file.
    # #158: the file named is one of the lake's own data files (enough rows to be written to a
    # file rather than kept in the catalog).
    write(lake, statement("CREATE TABLE m AS SELECT range AS a FROM range(200000)"))
    before = lake.current_snapshot().id
    target = next(tmp_path.rglob("ducklake-*.parquet"))
    assert code_of(lambda: read(lake, "SELECT * FROM (SUMMARIZE m)")) == "STORE_STATEMENT_INVALID"
    for sql in (
        f"CREATE TABLE x AS SUMMARIZE $${target.as_posix()}$$",
        f"CREATE TABLE x AS SUMMARIZE '{target.as_posix()}'",
        f"CREATE TABLE x AS SELECT * FROM $tag${target.as_posix()}$tag$",
    ):
        assert code_of(lambda sql=sql: write(lake, statement(sql))) == "STORE_STATEMENT_INVALID"
    assert lake.current_snapshot().id == before  # nothing was created


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN CREATE TABLE y AS SUMMARIZE m",
        "EXPLAIN SUMMARIZE m",
        "EXPLAIN ANALYZE SELECT 1",
    ],
)
def test_explain_is_refused_at_every_entry_point(lake: Lake, sql: str) -> None:
    # #158: refused by statement type wherever SQL enters the store.
    write(lake, statement("CREATE TABLE m AS SELECT range AS a FROM range(3)"))
    assert code_of(lambda: read(lake, sql)) == "STORE_STATEMENT_INVALID"
    assert code_of(lambda: write(lake, statement(sql))) == "STORE_STATEMENT_INVALID"
    assert code_of(lambda: lake.describe(sql)) == "STORE_STATEMENT_INVALID"

    def value() -> None:
        with lake.transaction() as transaction:
            transaction.value(statement(sql))

    assert code_of(value) == "STORE_STATEMENT_INVALID"


@pytest.mark.parametrize("sql", ["SELECT * FROM m desc", "SELEC 1", "SELECT ("])
def test_sql_that_does_not_parse_fails_with_the_parser_message(lake: Lake, sql: str) -> None:
    # #158: as before #154, whatever the statement checks would say.
    write(lake, statement("CREATE TABLE m AS SELECT range AS a FROM range(3)"))
    for action in (lambda: read(lake, sql), lambda: write(lake, statement(sql))):
        with pytest.raises(StoreError) as raised:
            action()
        assert raised.value.code == "STORE_QUERY_FAILED"
        assert "syntax error" in str(raised.value.details.get("duckdb", ""))


def test_histogram_ducklake_macros_and_table_options_are_refused_through_the_lake(
    lake: Lake, tmp_path: Path
) -> None:
    # #136 Reviewer passes 10 and 11, through a real lake: histogram's table form reads a file
    # named by an expression; DuckLake's table macros and table options change the lake's
    # options (deletion vectors, written unencrypted) or read deleted rows.
    write(lake, statement("CREATE TABLE m AS SELECT range AS a FROM range(3)"))
    before = lake.current_snapshot().id
    files = sorted(tmp_path.rglob("*.parquet"))
    named = files[0].as_posix() if files else (tmp_path / "x.parquet").as_posix()
    half = len(named) // 2
    refused = [
        f"CREATE TABLE stolen AS SELECT * FROM histogram('{named[:half]}' || "
        f"'{named[half:]}', a, technique := 'sample')",
        "CREATE TABLE h AS SELECT histogram(a) AS h FROM m",
        "CREATE TABLE x AS SELECT * FROM lake.set_option('write_deletion_vectors', true)",
        "CREATE TABLE x AS SELECT * FROM lake.table_deletions('m', 0, 1000)",
        "CREATE TABLE x AS SELECT * FROM lake.snapshots()",
        "CREATE TABLE y (a INTEGER) WITH (write_deletion_vectors = true)",
        "ALTER TABLE m SET (write_deletion_vectors = true)",
    ]
    for sql in refused:
        assert code_of(lambda sql=sql: write(lake, statement(sql))) == "STORE_STATEMENT_INVALID", (
            sql
        )
    assert lake.current_snapshot().id == before  # nothing was written


def test_a_read_with_summarize_is_refused_by_the_tree_too(
    lake: Lake, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #153, second layer of the read path: with the token rule switched off, the serialized
    # tree's SHOW_REF still refuses the read (it would be unpinned).
    import orvex.store.lake as store_lake

    write(lake, statement("CREATE TABLE m AS SELECT range AS a FROM range(3)"))
    monkeypatch.setattr(store_lake, "_refuse_show_statements", lambda *arguments: None)
    sql = "SELECT * FROM m UNION ALL BY NAME SUMMARIZE m"
    assert code_of(lambda: read(lake, sql)) == "STORE_STATEMENT_INVALID"


def test_a_read_that_binds_sql_text_is_refused_by_the_tree_too(
    lake: Lake, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #136 Reviewer pass 9: with the statement check's function list emptied, the read's tree
    # check still refuses json_serialize_plan, which would bind its text on the lake.
    import orvex.store.lake as store_lake

    write(lake, statement("CREATE TABLE m AS SELECT range AS a FROM range(3)"))
    assert code_of(lambda: read(lake, "SELECT json_serialize_plan('SELECT 1') AS p")) == (
        "STORE_STATEMENT_INVALID"
    )
    guarded = store_lake._guarded

    def without_the_function_rule(sql: str, table_functions: frozenset[str]) -> None:
        with monkeypatch.context() as patched:
            patched.setattr(store_lake, "_EXTERNAL_SQL_FUNCTIONS", frozenset())
            guarded(sql, table_functions)

    monkeypatch.setattr(store_lake, "_guarded", without_the_function_rule)
    sql = "SELECT a, json_serialize_plan('SELECT 1') AS p FROM m"
    assert code_of(lambda: read(lake, sql)) == "STORE_STATEMENT_INVALID"
