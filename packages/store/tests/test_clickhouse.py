"""ClickHouse Direct Query connection and reads (P2-31 PR 1, #225; ADR-0028 §3).

Runs against the server named by `STORE_TEST_CLICKHOUSE` (`host:port:user`, an administrator
of a disposable server) and `STORE_TEST_CLICKHOUSE_PASSWORD`; required in CI, skipped elsewhere
when missing. Each test creates its own profile, user and tables, with synthetic values only.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal

import pyarrow as pa
import pytest
from orvex.contracts.store.v1 import Parameter
from orvex.store import clickhouse
from orvex.store.clickhouse import Budget, ClickHouse, ClickHouseTarget, duckdb_type
from orvex.store.errors import StoreError

BUDGET = Budget(timeout_ms=30_000, memory_bytes=2_000_000_000, result_rows=1_000_000)
SAFE = {
    "readonly": "1",
    **clickhouse.REQUIRED_SETTINGS,
    "max_execution_time": "30",
    "max_memory_usage": "1000000000",
    "max_result_rows": "1000000",
}


@dataclass(frozen=True)
class Server:
    host: str
    port: int
    user: str
    password: str

    def run(self, sql: str) -> str:
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/",
            data=sql.encode(),
            headers={"X-ClickHouse-User": self.user, "X-ClickHouse-Key": self.password},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body: bytes = response.read()
            return body.decode()


@pytest.fixture(scope="module")
def server() -> Server:
    named = os.environ.get("STORE_TEST_CLICKHOUSE")
    if not named:
        if os.environ.get("CI"):
            pytest.fail("STORE_TEST_CLICKHOUSE is required in CI")
        pytest.skip("no ClickHouse server configured for this run")
    host, port, user = named.split(":")
    return Server(host, int(port), user, os.environ.get("STORE_TEST_CLICKHOUSE_PASSWORD", ""))


@pytest.fixture
def database(server: Server) -> Iterator[str]:
    name = f"t_{secrets.token_hex(6)}"
    server.run(f"CREATE DATABASE {name}")
    yield name
    server.run(f"DROP DATABASE IF EXISTS {name}")


type Connect = Callable[..., ClickHouse]


@pytest.fixture
def connect(server: Server, database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Connect]:
    """A user whose profile has `settings` (the safe ones by default), connected."""
    made: list[str] = []

    def make(settings: Mapping[str, str] | None = None, budget: Budget = BUDGET) -> ClickHouse:
        name = f"u_{secrets.token_hex(6)}"
        password = secrets.token_hex(16)
        listed = ", ".join(f"{key} = '{value}'" for key, value in (settings or SAFE).items())
        server.run(f"CREATE SETTINGS PROFILE {name} SETTINGS {listed}")
        server.run(f"CREATE USER {name} IDENTIFIED BY '{password}' SETTINGS PROFILE {name}")
        server.run(f"GRANT SELECT ON {database}.* TO {name}")
        made.append(name)
        monkeypatch.setenv("T_CLICKHOUSE_PASSWORD", password)
        target = ClickHouseTarget(
            server.host, server.port, database, name, "T_CLICKHOUSE_PASSWORD", tls=False
        )
        return ClickHouse.connect(target, budget)

    yield make
    for name in made:
        server.run(f"DROP USER IF EXISTS {name}")
        server.run(f"DROP SETTINGS PROFILE IF EXISTS {name}")


def code_of(action: Callable[[], object]) -> str:
    with pytest.raises(StoreError) as caught:
        action()
    return caught.value.code


def p(name: str, kind: str, value: str | None) -> Parameter:
    return Parameter.model_validate({"name": name, "type": kind, "value": value})


# --- verification at connection ----------------------------------------------------------------


def test_a_safe_profile_connects_and_names_its_version(connect: Connect) -> None:
    engine = connect()
    assert ".".join(engine.version.split(".")[:2]) in clickhouse.TESTED_VERSIONS


@pytest.mark.parametrize("value", ["0", "2"])
def test_a_profile_that_is_not_read_only_or_can_change_settings_is_refused(
    connect: Connect, value: str
) -> None:
    assert code_of(lambda: connect({**SAFE, "readonly": value})) == "SOURCE_NOT_READ_ONLY"


WRONG = {
    "join_use_nulls": "0",
    "group_by_use_nulls": "0",
    "transform_null_in": "1",
    "count_distinct_implementation": "uniq",
    "cast_keep_nullable": "0",
    "aggregate_functions_null_for_empty": "1",
    "cancel_http_readonly_queries_on_client_close": "0",
}


def test_every_required_setting_has_a_wrong_value_case() -> None:
    assert set(WRONG) == set(clickhouse.REQUIRED_SETTINGS)


@pytest.mark.parametrize(("name", "value"), sorted(WRONG.items()))
def test_each_setting_that_decides_meaning_must_have_its_value(
    connect: Connect, name: str, value: str
) -> None:
    with pytest.raises(StoreError) as caught:
        connect({**SAFE, name: value})
    assert caught.value.code == "ENGINE_SETTING_REQUIRED"
    assert caught.value.details["setting"] == name


@pytest.mark.parametrize("name", clickhouse.LIMIT_SETTINGS)
@pytest.mark.parametrize("value", ["0", "above"])
def test_each_limit_must_be_set_within_the_budget(connect: Connect, name: str, value: str) -> None:
    above = {"max_execution_time": "31", "max_memory_usage": "3000000000"}
    above["max_result_rows"] = "2000000"
    settings = {**SAFE, name: above[name] if value == "above" else value}
    with pytest.raises(StoreError) as caught:
        connect(settings)
    assert (caught.value.code, caught.value.details["setting"]) == (
        "ENGINE_SETTING_REQUIRED",
        name,
    )


def test_an_untested_version_is_refused(connect: Connect, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clickhouse, "TESTED_VERSIONS", ("1.1",))
    assert code_of(connect) == "ENGINE_VERSION_UNTESTED"


def test_another_machine_without_tls_is_refused_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("T_CLICKHOUSE_PASSWORD", "x")
    target = ClickHouseTarget("db.example.com", 8123, "d", "u", "T_CLICKHOUSE_PASSWORD", tls=False)
    assert code_of(lambda: ClickHouse.connect(target, BUDGET)) == "STORE_TLS_REQUIRED"


def test_a_missing_password_variable_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("T_CLICKHOUSE_NONE", raising=False)
    target = ClickHouseTarget("localhost", 8123, "d", "u", "T_CLICKHOUSE_NONE", tls=False)
    assert code_of(lambda: ClickHouse.connect(target, BUDGET)) == "STORE_SECRET_MISSING"


def test_the_password_appears_in_no_error(
    server: Server, database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    password = secrets.token_hex(16)
    monkeypatch.setenv("T_CLICKHOUSE_PASSWORD", password)
    target = ClickHouseTarget(
        server.host, server.port, database, f"nobody_{password}", "T_CLICKHOUSE_PASSWORD", False
    )
    with pytest.raises(StoreError) as caught:
        ClickHouse.connect(target, BUDGET)
    assert caught.value.code == "STORE_CONNECTION_FAILED"
    assert password not in str(caught.value) + repr(caught.value.details)


# A setting changed beyond the verified ones is refused, whatever it is: each of these cut,
# filter, cache or skip rows silently (measured on 26.8 by the #400 review).
SILENT = {
    "result_overflow_mode": "break",
    "read_overflow_mode": "break",
    "timeout_overflow_mode": "break",
    "max_rows_to_read": "1000",
    "additional_table_filters": "{\\'t\\': \\'k < 7\\'}",
    "use_query_cache": "1",
    "limit": "5",
    "offset": "1",
    "session_timezone": "America/La_Paz",
    "output_format_json_array_of_rows": "1",
}


@pytest.mark.parametrize(("name", "value"), sorted(SILENT.items()))
def test_any_other_changed_setting_is_refused(connect: Connect, name: str, value: str) -> None:
    with pytest.raises(StoreError) as caught:
        connect({**SAFE, name: value})
    assert caught.value.code == "ENGINE_SETTING_REQUIRED"
    named = caught.value.details["setting"]
    assert name in named.split(",") or (named == "output_format_*" and name.startswith("output_"))


def test_a_harmless_setting_may_be_changed(connect: Connect) -> None:
    assert connect({**SAFE, "max_threads": "2", "log_queries": "1"}).version


# --- types -------------------------------------------------------------------------------------

MAPPED = {
    "Int8": "TINYINT",
    "Int64": "BIGINT",
    "Int128": "HUGEINT",
    "UInt64": "UBIGINT",
    "UInt128": "UHUGEINT",
    "Decimal(38, 10)": "DECIMAL(38,10)",
    "Decimal64(2)": "DECIMAL(18,2)",
    "Float32": "FLOAT",
    "String": "VARCHAR",
    "Nullable(Int32)": "INTEGER",
    "LowCardinality(Nullable(String))": "VARCHAR",
    "Date32": "DATE",
    "DateTime": "TIMESTAMPTZ",
    "DateTime('America/La_Paz')": "TIMESTAMPTZ",
    "DateTime64(6, 'America/La_Paz')": "TIMESTAMPTZ",
    "Bool": "BOOLEAN",
    "UUID": "UUID",
}
REFUSED = [
    "Int256",
    "UInt256",
    "Decimal(39, 2)",
    "Decimal256(2)",
    "DateTime64(9)",
    "FixedString(4)",
    "Enum8('a' = 1)",
    "Array(Int32)",
    "Map(String, Int32)",
    "Tuple(Int32, String)",
    "IPv4",
    "IPv6",
    "Nothing",
    "Nullable(Nothing)",
]


@pytest.mark.parametrize(("engine", "duckdb"), sorted(MAPPED.items()))
def test_each_clickhouse_type_maps_exactly(engine: str, duckdb: str) -> None:
    assert duckdb_type(engine) == duckdb


@pytest.mark.parametrize("engine", REFUSED)
def test_a_type_without_an_exact_form_maps_to_nothing(engine: str) -> None:
    assert duckdb_type(engine) is None


def test_table_columns_are_mapped_and_an_unmappable_one_is_named(
    server: Server, database: str, connect: Connect
) -> None:
    server.run(f"CREATE TABLE {database}.ok (a Int64, b Nullable(Decimal(18, 2))) ENGINE = Memory")
    server.run(f"CREATE TABLE {database}.bad (a Int64, tags Array(String)) ENGINE = Memory")
    engine = connect()
    assert [(c.name, c.type) for c in engine.columns("ok")] == [
        ("a", "BIGINT"),
        ("b", "DECIMAL(18,2)"),
    ]
    with pytest.raises(StoreError) as caught:
        engine.columns("bad")
    assert (caught.value.code, caught.value.details["column"]) == (
        "UNSUPPORTED_SOURCE_TYPE",
        "tags",
    )
    assert code_of(lambda: engine.columns("missing")) == "SOURCE_NOT_FOUND"


EXTREMES = (
    "SELECT toInt64(-9223372036854775808) AS i64, toUInt64(18446744073709551615) AS u64, "
    "toString(toInt128('-99999999999999999999999999999999999999')) AS i128, "
    "toString(toUInt128('340282366920938463463374607431768211455')) AS u128, "
    "toDecimal128('-9999999999999999999999999999.9999999999', 10) AS d38, "
    "toDecimal64('12.34', 2) AS d18, "
    "CAST(NULL, 'Nullable(Int32)') AS n32, CAST(NULL, 'Nullable(Decimal(18, 2))') AS nd, "
    "toLowCardinality(toNullable('x')) AS lc, "
    "toDateTime64('2024-01-01 00:00:00.123456', 6, 'America/La_Paz') AS ts, "
    "toDate32('1900-01-01') AS dt, toString(toUUID('00000000-0000-0000-0000-000000000001')) AS id, "
    "true AS b, toFloat32(1.5) AS f"
)
EXTREME_COLUMNS = [
    ("i64", "BIGINT"),
    ("u64", "UBIGINT"),
    ("i128", "HUGEINT"),
    ("u128", "UHUGEINT"),
    ("d38", "DECIMAL(38,10)"),
    ("d18", "DECIMAL(18,2)"),
    ("n32", "INTEGER"),
    ("nd", "DECIMAL(18,2)"),
    ("lc", "VARCHAR"),
    ("ts", "TIMESTAMPTZ"),
    ("dt", "DATE"),
    ("id", "UUID"),
    ("b", "BOOLEAN"),
    ("f", "FLOAT"),
]


def test_every_type_round_trips_exactly_at_its_extremes(connect: Connect) -> None:
    table = connect().read(EXTREMES, [], EXTREME_COLUMNS).read_all()
    assert [str(f.type) for f in table.schema] == [
        "int64",
        "uint64",
        "decimal128(38, 0)",
        "string",
        "decimal128(38, 10)",
        "decimal128(18, 2)",
        "int32",
        "decimal128(18, 2)",
        "string",
        "timestamp[us, tz=UTC]",
        "date32[day]",
        "string",
        "bool",
        "float",
    ]
    assert [f.metadata[b"duckdb_type"].decode() for f in table.schema] == [
        kind for _, kind in EXTREME_COLUMNS
    ]
    (row,) = table.to_pylist()
    assert row["i64"] == -9223372036854775808
    assert row["u64"] == 18446744073709551615
    assert row["i128"] == Decimal("-99999999999999999999999999999999999999")
    assert row["u128"] == "340282366920938463463374607431768211455"
    assert row["d38"] == Decimal("-9999999999999999999999999999.9999999999")
    assert row["d18"] == Decimal("12.34")
    assert (row["n32"], row["nd"]) == (None, None)
    assert row["lc"] == "x"
    assert row["ts"].isoformat() == "2024-01-01T04:00:00.123456+00:00"  # La Paz is UTC-4
    assert row["id"] == "00000000-0000-0000-0000-000000000001"
    assert (row["b"], row["f"]) == (True, 1.5)


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT toString(toInt128('170141183460469231731687303715884105727')) AS v", "HUGEINT"),
        ("SELECT unhex('C328') AS v", "VARCHAR"),  # invalid UTF-8
        ("SELECT toDateTime('2024-01-01 00:00:00', 'UTC') AS v", "TIMESTAMPTZ"),  # not µs
        ("SELECT toInt32(1) AS v", "BIGINT"),  # another width
        ("SELECT toFloat64(1.5) AS v", "DECIMAL(18,2)"),  # a float never becomes a decimal
        ("SELECT '-1' AS v", "UHUGEINT"),  # text that is not unsigned digits
    ],
    ids=["hugeint-beyond-38-digits", "invalid-utf8", "datetime-seconds", "width", "float", "uhuge"],
)
def test_a_value_or_form_that_is_not_exact_is_refused(
    connect: Connect, sql: str, kind: str
) -> None:
    engine = connect()
    assert code_of(lambda: engine.read(sql, [], [("v", kind)]).read_all()) == (
        "UNSUPPORTED_SOURCE_TYPE"
    )


# --- parameters and statements -----------------------------------------------------------------


def test_values_are_bound_and_never_run(server: Server, database: str, connect: Connect) -> None:
    server.run(f"CREATE TABLE {database}.x (a Int64) ENGINE = Memory")
    server.run(f"INSERT INTO {database}.x VALUES (1)")
    evil = "'); DROP TABLE x; --\t\\N"
    table = (
        connect()
        .read(
            "SELECT {s:String} AS s, {n:Nullable(Int64)} AS n, {d:Decimal(18, 2)} AS d, "
            "{t:DateTime64(6, 'UTC')} AS t, {b:Bool} AS b",
            [
                p("s", "VARCHAR", evil),
                p("n", "BIGINT", None),
                p("d", "DECIMAL(18,2)", "-12.34"),
                p("t", "TIMESTAMPTZ", "2024-01-01T00:00:00-04:00"),
                p("b", "BOOLEAN", "true"),
            ],
            [
                ("s", "VARCHAR"),
                ("n", "BIGINT"),
                ("d", "DECIMAL(18,2)"),
                ("t", "TIMESTAMPTZ"),
                ("b", "BOOLEAN"),
            ],
        )
        .read_all()
    )
    (row,) = table.to_pylist()
    assert row["s"] == evil
    assert row["n"] is None
    assert row["d"] == Decimal("-12.34")
    assert row["t"].isoformat() == "2024-01-01T04:00:00+00:00"
    assert row["b"] is True
    assert server.run(f"SELECT count() FROM {database}.x").strip() == "1"


@pytest.mark.parametrize(
    ("sql", "parameters"),
    [
        ("SELECT {a:Int64} AS v", []),
        ("SELECT 1 AS v", [p("a", "BIGINT", "1")]),
        ("SELECT {a:Int32} AS v", [p("a", "BIGINT", "1")]),
        ("SELECT {a:Int64} AS v", [p("a", "BIGINT", None)]),
        ("SELECT {a:Int64} + {a:Int32} AS v", [p("a", "BIGINT", "1")]),
        ("SELECT {a:Nullable(DateTime64(6, 'UTC'))} AS v", [p("a", "TIMESTAMPTZ", "2024-01-01")]),
        ("SELECT {a:String} AS v", [p("a", "TIMESTAMP", "2024-01-01T00:00:00")]),
    ],
    ids=["missing", "extra", "other-type", "null-not-nullable", "two-types", "no-offset", "local"],
)
def test_a_parameter_that_does_not_fit_its_placeholder_is_refused(
    connect: Connect, sql: str, parameters: list[Parameter]
) -> None:
    engine = connect()
    assert code_of(lambda: engine.read(sql, parameters, [("v", "BIGINT")])) == (
        "STORE_PARAMETER_INVALID"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS v; SELECT 2",
        "SELECT 1 AS v SETTINGS readonly = 0",
        "SELECT 1 AS v FORMAT JSON",
        "SELECT 1 AS v INTO OUTFILE 'x'",
        "INSERT INTO t VALUES (1)",
        "SELECT '--' AS a, 1 SETTINGS max_threads = 1",
        "SELECT 1 # '\nSETTINGS max_threads = 1 # '",
        "SELECT 1 # '\nFORMAT TSV # '",
        "SELECT '--'; SELECT 1",
        "SELECT 1 AS v /* unclosed",
        "SELECT 'unclosed",
        "SELECT 1;",
        "SELECT $$'$$ AS a SETTINGS max_threads = 1, b = $$'$$",
        "SELECT $x$ unclosed",
    ],
)
def test_only_one_plain_select_is_run(connect: Connect, sql: str) -> None:
    engine = connect()
    assert code_of(lambda: engine.read(sql, [], [("v", "BIGINT")])) == "STORE_STATEMENT_INVALID"


def test_a_word_inside_a_string_or_a_comment_is_not_a_clause(connect: Connect) -> None:
    table = connect().read("SELECT 'a; FORMAT SETTINGS' AS v -- SETTINGS\n", [], [("v", "VARCHAR")])
    assert table.read_all().column("v").to_pylist() == ["a; FORMAT SETTINGS"]


def test_an_engine_error_names_its_code(connect: Connect) -> None:
    engine = connect()
    with pytest.raises(StoreError) as caught:
        engine.read("SELECT no_such_function(1) AS v", [], [("v", "BIGINT")]).read_all()
    assert caught.value.code == "STORE_QUERY_FAILED"
    assert caught.value.details["engine_code"] == "46"


# --- time and cancellation ---------------------------------------------------------------------


def _running(server: Server, marker: str) -> int:
    return int(
        server.run(
            f"SELECT count() FROM system.processes WHERE query LIKE '%{marker}%' "
            "AND query NOT LIKE '%system.processes%'"
        ).strip()
    )


def test_a_read_over_its_time_budget_is_cancelled_on_the_server(
    server: Server, connect: Connect
) -> None:
    engine = connect()
    marker = secrets.token_hex(4)
    sql = f"SELECT sum(number) AS v, '{marker}' AS m FROM numbers(1000000000000)"
    started = time.monotonic()
    with pytest.raises(StoreError) as caught:
        engine.read(sql, [], [("v", "UBIGINT"), ("m", "VARCHAR")], timeout_ms=500).read_all()
    assert caught.value.code == "STORE_TIMEOUT"
    assert time.monotonic() - started < 10
    deadline = time.monotonic() + 10
    while _running(server, marker) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _running(server, marker) == 0


def test_the_server_s_own_time_limit_gives_the_same_code(connect: Connect) -> None:
    engine = connect({**SAFE, "max_execution_time": "1"})
    sql = "SELECT sum(number) AS v FROM numbers(1000000000000)"
    # The error comes before the first batch (an HTTP error) or inside the stream.
    assert code_of(lambda: engine.read(sql, [], [("v", "UBIGINT")]).read_all()) == "STORE_TIMEOUT"


def test_two_reads_run_at_once_on_one_connection(connect: Connect) -> None:
    engine = connect()
    results: list[int] = []

    def one(n: int) -> None:
        table = engine.read("SELECT {n:Int64} AS v", [p("n", "BIGINT", str(n))], [("v", "BIGINT")])
        results.append(table.read_all().column("v").to_pylist()[0])

    threads = [threading.Thread(target=one, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [0, 1, 2, 3]


def test_the_read_instant_is_the_server_s_clock_in_utc(connect: Connect) -> None:
    instant = connect().now()
    assert instant.utcoffset() is not None
    assert instant.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_no_row_becomes_a_python_object_in_the_read(connect: Connect) -> None:
    reader = connect().read("SELECT number AS v FROM numbers(10)", [], [("v", "UBIGINT")])
    assert isinstance(reader, pa.RecordBatchReader)
    assert reader.read_all().num_rows == 10


def test_the_engine_is_described_without_a_secret() -> None:
    target = ClickHouseTarget("DB.example.com", 8443, "sales", "u", "SECRET_ENV")
    assert clickhouse.describe(target) == {
        "kind": "clickhouse",
        "host": "db.example.com",
        "port": 8443,
        "database": "sales",
    }


@pytest.mark.parametrize(
    "kind",
    ["UHUGEINT[]", "UHUGEINT[2]", "STRUCT(a UHUGEINT)", "MAP(VARCHAR, UHUGEINT)"],
    ids=["list", "array", "struct", "map"],
)
def test_a_nested_uhugeint_is_refused_before_the_query_is_sent(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """#410: the same check as every other Arrow export, before anything reaches the server."""
    monkeypatch.setenv("STORE_TEST_NESTED_PASSWORD", "unused")
    target = ClickHouseTarget("localhost", 8123, "sales", "reader", "STORE_TEST_NESTED_PASSWORD")
    engine = ClickHouse(target, Budget(timeout_ms=1_000, memory_bytes=1 << 30, result_rows=10))

    def sent(*_: object, **__: object) -> object:
        raise AssertionError("the statement was sent")

    monkeypatch.setattr(engine, "_send", sent)
    with pytest.raises(StoreError) as refused:
        engine.read("SELECT 1 AS ok, [1] AS v", [], [("ok", "BIGINT"), ("v", kind)])
    assert refused.value.code == "STORE_TYPE_UNSUPPORTED"
    assert refused.value.details["column"] == "v"
