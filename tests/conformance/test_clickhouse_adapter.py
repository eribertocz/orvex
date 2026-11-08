"""The ClickHouse dialect adapter, core (P2-31 PR 2, #225; ADR-0027 §8, ADR-0028 §3).

Two kinds of tests:

- **Statement text**, with no server: each dialect difference is encoded explicitly, every
  value is a typed placeholder, and every corpus statement passes the allow-list check.
- **Parity**, against the server named by `SEMANTIC_TEST_CLICKHOUSE` (`host:port:user`, an
  administrator of a disposable server) and `SEMANTIC_TEST_CLICKHOUSE_PASSWORD`; required in
  CI, skipped elsewhere when missing. Each module run creates its own database, read-only
  profile and user, with synthetic values only. The engine is reached over HTTP here, never
  through `orvex.store`: answers are compared with the managed lake cell by cell and type by
  type, so removing any explicit encoding makes a parity case fail.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.ipc
import pytest
from orvex.contracts.store.v1 import Parameter, ParameterType, StatementRequest
from orvex.semantic import execution, external
from orvex.semantic.adapters import clickhouse as ch
from orvex.semantic.adapters.clickhouse import exact as ch_exact
from orvex.semantic.adapters.clickhouse import writer as ch_writer
from orvex.semantic.adapters.duckdb import PhysicalQuery, PolicyValues, mapping_queries
from orvex.semantic.adapters.duckdb import compile_plan as duckdb_compile
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.correctness import analyze
from orvex.semantic.engines import CAPABILITIES, EngineEntry, Routing
from orvex.semantic.errors import PlanError, SemanticError
from orvex.semantic.execution import answer
from orvex.semantic.ossie.reader import read
from orvex.semantic.planning import SemanticPlan, plan
from orvex.semantic.policies import PolicyModel
from orvex.semantic.query.binding import Limits as QueryLimits
from orvex.semantic.query.binding import bind_query
from orvex.semantic.result.descriptors import arrow_type
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

from conformance.runners import bi_platform_adapter
from conformance.runners import corpus_execution as ce
from tests.conformance import test_direct_query as dq
from tests.conformance import test_row_policies as rp
from tests.conformance.test_execution import no_policies
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "conformance/semantic-corpus/bi-platform"
MANIFEST = json.loads((CORPUS / "corpus/manifest.json").read_text("utf-8"))
# The corpus cases ClickHouse answers: every case the managed lake answers (the others are
# refused when the model is compiled, before any engine).
ANSWERED = tuple(
    c["id"]
    for c in MANIFEST["cases"]
    if c["id"] not in ("CASE-008", "CASE-016", "CASE-017", "CASE-020")
)
ENTRY = EngineEntry(
    "warehouse", "clickhouse", "live", "clickhouse://localhost/test", ch.CAPABILITIES
)

# --- a model that meets every dialect difference -------------------------------------------

# `Amount` is written with its case in the engine; the model names it `amount`.
DIALECT = {
    "m": {
        "id": SourceColumn("BIGINT", False),
        "Amount": SourceColumn("DECIMAL(18,2)", True),
        "price": SourceColumn("DECIMAL(9,4)", False),
        "rate": SourceColumn("DECIMAL(9,2)", False),
        "big": SourceColumn("BIGINT", False),
        "qty": SourceColumn("INTEGER", True),
        "day": SourceColumn("DATE", False),
        "label": SourceColumn("VARCHAR", True),
        "huge": SourceColumn("HUGEINT", False),
    }
}
ROWS: dict[str, list[list[str | None]]] = {
    "m": [
        # 2^62 three times: the sum leaves BIGINT. 9 digits by 9: ClickHouse's own product
        # stays in a Decimal32 and overflows; DuckDB's is a DECIMAL(18). 36 digits: HUGEINT.
        [
            "1",
            "9999999999999999.99",
            "99999.9999",
            "9999999.99",
            "4611686018427387904",
            "3",
            "1969-12-31",
            "a",
            "999999999999999999999999999999999999",
        ],
        ["2", None, "0.0001", "0.01", "4611686018427387904", None, "1970-01-05", None, "-1"],
        [
            "3",
            "-0.01",
            "1.2345",
            "-3.50",
            "4611686018427387904",
            "7",
            "2024-02-29",
            "b",
            "170141183460469231731687303715884105",
        ],
        ["4", "0.00", "2.0000", "0.00", "-5", "0", "2024-03-03", "B", "0"],
        # An empty label: a value of its own, never NULL, ordered first in byte order.
        ["5", "0.01", "0.5000", "1.00", "1", None, "2025-12-31", "", "1"],
    ]
}
FIELDS = {
    "month": "DATE_TRUNC('MONTH', day)",
    "week": "DATE_TRUNC('WEEK', day)",
    "quarter": "DATE_TRUNC('QUARTER', day)",
    "year": "DATE_TRUNC('YEAR', day)",
    "scaled": "price * 1.15",  # 1.15 as a float is 1.1499...
    "doubled": "qty * 2",
    "huge_next": "huge + 1",  # an Int128 operand, which ClickHouse cannot CAST to a Decimal
    "small": "qty IN (0, 3)",
    "unlisted": "qty NOT IN (0, NULL)",  # never true in SQL: NULL is in the list
    "shouting": "UPPER(label)",
}
METRICS = {
    "total": "SUM(m.amount)",
    "big_total": "SUM(m.big)",
    "product": "SUM(m.price * m.rate)",
    "scaled_total": "SUM(m.scaled)",
    "doubled_total": "SUM(m.doubled)",
    "huge_total": "SUM(m.huge_next)",
    "labelled": "COUNT(m.label)",
    "rows": "COUNT(*)",
    "labels": "COUNT(DISTINCT m.label)",
    "a_total": "SUM(m.amount) FILTER (WHERE m.label = 'a')",
    "a_rows": "COUNT(*) FILTER (WHERE m.label = 'a')",
    # A column with no NULL: ClickHouse's sum of a Nullable one is already NULL over no value.
    "a_big_total": "SUM(m.big) FILTER (WHERE m.label = 'a')",
}


DISTINCT = {"element": "metric", "aggregate_class": "holistic", "additivity": "non_additive"}


HOME = {"element": "metric", "home_dataset": "m"}


def dialect_model() -> CompiledModel:
    columns = DIALECT["m"]
    fields = [{"name": c.lower(), "expression": {"sql": c.lower()}} for c in columns]
    fields += [{"name": n, "expression": {"sql": e}} for n, e in FIELDS.items()]
    document = {
        "version": "0.2.0.dev0",
        "name": "dialect",
        "datasets": [{"name": "m", "source": "m", "primary_key": ["id"], "fields": fields}],
        "metrics": [
            {"name": n, "expression": {"sql": e}}
            | ({"custom_extensions": [extension(DISTINCT)]} if "DISTINCT" in e else {})
            | ({"custom_extensions": [extension(HOME)]} if "(*)" in e else {})
            for n, e in METRICS.items()
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/dialect.ossie.json", VENDORS)
    return compile_model(loaded, DIALECT, VENDORS)


MODEL = dialect_model()


def question(*metrics: str, group_by: Sequence[str] = (), **more: Any) -> Document:  # noqa: ANN401
    found: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": m} for m in metrics],
    }
    if group_by:
        found["group_by"] = [{"member": f"m.{g}"} for g in group_by]
    return found | more


def label_filter(op: str, *values: str, **flags: bool) -> Document:
    return {
        "scope": "board",
        "member": "m.label",
        "predicate": {"op": op, "values": [{"type": "text", "value": v} for v in values]} | flags,
    }


def plan_of(
    document: Document,
    compiled: CompiledModel = MODEL,
    policies: PolicyModel | None = None,
    context: Mapping[str, Any] | None = None,
) -> SemanticPlan:
    bound = bind_query(
        compiled,
        json.dumps(document),
        instant=ce.INSTANT,
        limits=QueryLimits(detail_page_size=1000),
        hidden=lambda _: False,
    )
    policies = policies or no_policies(compiled)
    return plan(analyze(bound, compiled, policies, context or ce.viewer_context()))


def names_of(catalog: Mapping[str, Mapping[str, SourceColumn]]) -> dict[str, list[str]]:
    return {table: list(columns) for table, columns in catalog.items()}


def written(document: Document, **more: Any) -> PhysicalQuery:  # noqa: ANN401
    return ch.compile_plan(plan_of(document, **more), names_of(DIALECT), {})


def placeholders(statement: str) -> set[str]:
    return set(re.findall(r"\{(p[0-9]+):", statement))


# --- statement text --------------------------------------------------------------------------


def test_a_column_is_written_with_its_exact_name_in_the_engine() -> None:
    statement = written(question("total")).sql
    assert '"t0"."Amount"' in statement
    assert '"AMOUNT"' not in statement
    assert '"amount"' not in statement
    with pytest.raises(PlanError):  # a catalog without the column: never a guessed name
        ch.compile_plan(plan_of(question("total")), {"m": ["id"]}, {})
    with pytest.raises(PlanError):  # two names that differ only in case
        ch.compile_plan(plan_of(question("total")), {"m": ["Amount", "AMOUNT"]}, {})


def test_every_value_is_a_typed_placeholder() -> None:
    query = written(question("total", filters=[label_filter("in", "a", "'); DROP TABLE m; --")]))
    assert "$p" not in query.sql
    assert "DROP" not in query.sql
    assert placeholders(query.sql) == {p["name"] for p in query.parameters}
    assert "{p0:String}" in query.sql
    assert all(p["value"] is not None for p in query.parameters)


def test_exact_arithmetic_is_widened_and_never_divided_by_slash() -> None:
    statement = written(question("product", "scaled_total", "doubled_total")).sql
    # Each exact operand widened to 38 digits first: a native product keeps the narrower one.
    assert 'toDecimal256("t0"."price", 4) * toDecimal256("t0"."rate", 2)' in statement
    assert "toDecimal128(" not in statement  # 38 digits wrap on a product
    assert "CAST('1.15' AS Decimal(3, 2))" in statement  # never the float literal 1.15
    assert re.search(r"[^']1\.15", statement) is None
    assert " / " not in statement


def test_a_ratio_is_an_exact_integer_division_rounded_once() -> None:
    model = CORPUS / "models" / "CASE-006.yaml"
    compiled = ce.compiled_case(model)
    found = plan_of(ce.QUERIES["CASE-006"], compiled)
    statement = ch.compile_plan(found, names_of(ce.catalog_of(model)), {}).sql
    assert 'intDiv("#a0", "#b0")' in statement
    assert "//" not in statement
    assert "CAST('0.01' AS Decimal(38, 2))" in statement  # never the float 0.01
    assert "trunc(" not in statement
    assert " / " not in statement


def test_a_sum_is_null_over_no_row_and_accumulates_in_256_bits() -> None:
    statement = written(question("total", "big_total")).sql
    amount = 'toDecimal256("t0"."Amount", 2)'
    assert f"if(count({amount}) = 0, NULL, sum({amount}))" in statement
    big = 'toInt256("t0"."big")'  # 256 bits: no sum of the plan's sizes wraps there
    assert f"if(count({big}) = 0, NULL, sum({big}))" in statement
    assert "SUM(" not in statement
    filtered = written(question("a_total")).sql  # a filtered sum: NULL when no row passes
    assert re.search(
        r'if\(countIf\(toDecimal256\("t0"\."Amount", 2\), (.+)\) = 0, NULL, '
        r'sumIf\(toDecimal256\("t0"\."Amount", 2\), \1\)\)',
        filtered,
    )


def test_the_latest_row_of_a_semi_additive_metric_puts_null_last_explicitly() -> None:
    model = CORPUS / "models" / "CASE-007.yaml"
    compiled = ce.compiled_case(model)
    found = plan_of(ce.QUERIES["CASE-007"], compiled)
    statement = ch.compile_plan(found, names_of(ce.catalog_of(model)), {}).sql
    assert re.search(
        r"QUALIFY row_number\(\) OVER \(PARTITION BY .+ ORDER BY \S+ DESC NULLS LAST\)", statement
    )


def test_a_filtered_aggregate_is_an_if_combinator() -> None:
    statement = written(question("a_total", "a_rows", "labels")).sql
    assert "FILTER" not in statement
    assert "sumIf(" in statement
    assert "countIf((" in statement
    assert 'uniqExact("t0"."label")' in statement


def test_in_and_not_in_keep_sql_three_valued_meaning() -> None:
    expression = written(question("total", group_by=["small", "unlisted"])).sql
    assert '("t0"."qty" = 0 OR "t0"."qty" = 3)' in expression
    assert '(NOT ("t0"."qty" = 0 OR "t0"."qty" = NULL))' in expression
    assert " IN (" not in expression


def test_a_date_bucket_stays_a_date32() -> None:
    statement = written(question("total", group_by=["month", "week", "quarter", "year"])).sql
    assert 'makeDate32(toYear("t0"."day"), toMonth("t0"."day"), 1)' in statement
    assert '("t0"."day" - (toDayOfWeek("t0"."day") - 1))' in statement
    assert "toStartOf" not in statement
    assert "date_trunc" not in statement


def test_each_result_column_is_converted_to_its_exact_store_form_after_ordering() -> None:
    query = written(question("big_total", "rows", group_by=["label"]))
    assert query.sql.startswith(
        'SELECT CAST("#r"."label" AS String) AS "label", '
        f'toString({ch_exact.checked('"#r"."big_total"', "HUGEINT")}) AS "big_total", '
        f'{ch_exact.checked('"#r"."rows"', "BIGINT")} AS "rows" FROM ('
    )
    assert query.sql.endswith(' AS "#r" ORDER BY "#r"."label" ASC NULLS LAST')
    assert query.adapter == "clickhouse"


def test_case_mapping_is_refused_as_it_differs_from_the_reference() -> None:
    with pytest.raises(SemanticError) as caught:
        written(question("rows", group_by=["shouting"]))
    assert caught.value.code == "UNSUPPORTED_OPERATION"


def test_the_adapter_runs_every_capability() -> None:
    assert ch.CAPABILITIES == CAPABILITIES


@pytest.mark.parametrize(
    "statement",
    [
        'SELECT 1 FROM numbers(10) AS "n"',
        'SELECT 1 FROM "m" AS "a" SETTINGS readonly = 0',
        'SELECT 1 FROM "m" AS "a" FORMAT JSON',
        'SELECT 1 FROM "m" AS "a"; SELECT 2',
        'SELECT sleepEachRow(1) FROM "m" AS "a"',
        'SELECT 1 FROM "system"."users" AS "a"',
        'SELECT 1 FROM "m" AS "a", url(\'http://x\') AS "b"',
        'SELECT 1 FROM "m" AS "a" WHERE EXISTS (SELECT 1 FROM "other" AS "b")',
        "SELECT 1 FROM m",
    ],
)
def test_a_statement_outside_the_allow_lists_is_never_sent(statement: str) -> None:
    with pytest.raises(PlanError):
        ch.check_statement(statement, {"m"})


def test_a_name_with_a_quote_or_a_backslash_is_quoted_for_clickhouse() -> None:
    assert ch.quote_name('a"b\\c') == '"a""b\\\\c"'
    ch.check_statement(f'SELECT 1 FROM {ch.quote_name('a"b\\c')} AS "x"', {'a"b\\c'})


@pytest.mark.parametrize("case", [c["id"] for c in MANIFEST["cases"]])
def test_every_corpus_statement_passes_the_allow_lists(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each statement the corpus produces compiles, and `compile_plan` checks it against the
    allow-lists."""
    model = CORPUS / "models" / f"{case}.yaml"
    outcomes: list[str] = []
    real = duckdb_compile

    def both(found: SemanticPlan, values: Mapping[int, PolicyValues] | None = None) -> Any:  # noqa: ANN401
        try:
            ch.compile_plan(found, names_of(ce.catalog_of(model)), values or {})
            outcomes.append("compiled")
        except SemanticError as error:
            outcomes.append(str(error.details["operation"]))
        return real(found, values)

    monkeypatch.setattr(execution, "compile_plan", both)
    entry = next(c for c in MANIFEST["cases"] if c["id"] == case)
    ce.run_case(case, CORPUS / "corpus/cases" / case, model, entry.get("error_code"))
    if case in ANSWERED:
        assert outcomes
        assert set(outcomes) == {"compiled"}
    else:  # refused when the model is compiled, or not run: nothing is planned
        assert outcomes == []


def test_a_mapping_set_is_bound_as_one_array_with_null_membership_explicit() -> None:
    policy = rp.policies(rp.DENY)
    found = plan_of(rp.query(), rp.COMPILED, policy, rp.caller(user_id="u-1"))
    values = {q.key: PolicyValues("BIGINT", ("7", None)) for q in mapping_queries(found)}
    query = ch.compile_plan(found, names_of(rp.CATALOG), values)
    assert "multiIf(empty(CAST(JSONExtract({p" in query.sql
    assert "has(CAST(JSONExtract(" in query.sql
    assert "NULL), NULL, false)" in query.sql
    assert '"security"' not in query.sql
    assert "u-1" not in query.sql
    assert "7" not in query.sql
    assert placeholders(query.sql) == {p["name"] for p in query.parameters}
    assert [p["value"] for p in query.parameters if p["value"] == '["7", null]'] == ['["7", null]']


def test_a_rows_scanned_estimate_counts_every_read_and_needs_every_table() -> None:
    query = written(question("total"))
    rows = [{"database": "d", "table": "m", "parts": 1, "rows": 4, "marks": 1}]
    assert ch.rows_scanned(query, rows, "d") == 4
    assert ch.rows_scanned(query, [], "d") is None  # not MergeTree: no statistics
    assert ch.rows_scanned(query, rows, "other") is None
    assert ch.estimate_statement(query) == f"EXPLAIN ESTIMATE {query.sql}"


def test_an_estimate_above_the_budget_is_refused_and_named_only_without_policies() -> None:
    ch.refuse_over_budget(None, 1, restricted=False)  # no estimate decides nothing
    ch.refuse_over_budget(10, 10, restricted=False)
    with pytest.raises(SemanticError) as caught:
        ch.refuse_over_budget(11, 10, restricted=False)
    assert caught.value.details == {"budget": "rows_scanned", "limit": "10", "estimate": "11"}
    with pytest.raises(SemanticError) as hidden:
        ch.refuse_over_budget(11, 10, restricted=True)
    assert hidden.value.details == {"budget": "rows_scanned", "limit": "10"}


# --- parity on a ClickHouse server -----------------------------------------------------------

PROFILE = {
    "readonly": "1",
    "join_use_nulls": "1",
    "group_by_use_nulls": "1",
    "transform_null_in": "0",
    "count_distinct_implementation": "uniqExact",
    "cast_keep_nullable": "1",
    "aggregate_functions_null_for_empty": "0",
    "cancel_http_readonly_queries_on_client_close": "1",
    "max_execution_time": "30",
    "max_memory_usage": "1000000000",
    "max_result_rows": "1000000",
}


class EngineFailure(AssertionError):
    """An error the server answered, with its ClickHouse code."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        found = re.match(r"Code: (\d+)\. ", text)
        self.code = found.group(1) if found else ""


@dataclass
class Server:
    host: str
    port: int
    user: str
    password: str
    database: str = ""

    def run(
        self,
        statement: str,
        values: Mapping[str, str] | None = None,
        as_reader: tuple[str, str] | None = None,
    ) -> bytes:
        user, password = as_reader or (self.user, self.password)
        query = {"database": self.database} if self.database else {}
        query |= dict(values or {})
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/?{urllib.parse.urlencode(query)}",
            data=statement.encode(),
            headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body: bytes = response.read()
                return body
        except urllib.error.HTTPError as error:
            with error:
                text = error.read().decode()[:2000]
            raise EngineFailure(text) from None


@pytest.fixture(scope="module")
def server() -> Iterator[Server]:
    named = os.environ.get("SEMANTIC_TEST_CLICKHOUSE")
    if not named:
        if os.environ.get("CI"):
            pytest.fail("SEMANTIC_TEST_CLICKHOUSE is required in CI")
        pytest.skip("no ClickHouse server configured for this run")
    host, port, user = named.split(":")
    found = Server(host, int(port), user, os.environ.get("SEMANTIC_TEST_CLICKHOUSE_PASSWORD", ""))
    name = f"s_{secrets.token_hex(6)}"
    password = secrets.token_hex(16)
    found.run(f"CREATE DATABASE {name}")
    listed = ", ".join(f"{key} = '{value}'" for key, value in PROFILE.items())
    found.run(f"CREATE SETTINGS PROFILE {name} SETTINGS {listed}")
    found.run(f"CREATE USER {name} IDENTIFIED BY '{password}' SETTINGS PROFILE {name}")
    found.run(f"GRANT SELECT ON {name}.* TO {name}")
    found.database = name
    found.reader = (name, password)  # type: ignore[attr-defined]
    try:
        yield found
    finally:
        found.database = ""
        found.run(f"DROP USER IF EXISTS {name}")
        found.run(f"DROP SETTINGS PROFILE IF EXISTS {name}")
        found.run(f"DROP DATABASE IF EXISTS {name}")


def load(
    server: Server,
    catalog: Mapping[str, Mapping[str, SourceColumn]],
    rows: Mapping[str, Sequence[Sequence[str | None]]],
    engine: str = "MergeTree ORDER BY tuple()",
) -> None:
    """Each table, replaced, in the mapped ClickHouse types, with the same rows as the lake."""
    for table, columns in catalog.items():
        types = {
            name: ch.engine_type(c.type)
            if not c.nullable
            else f"Nullable({ch.engine_type(c.type)})"
            for name, c in columns.items()
        }
        listed = ", ".join(f"{ch.quote_name(n)} {t}" for n, t in types.items())
        server.run(f"DROP TABLE IF EXISTS {ch.quote_name(table)}")
        server.run(f"CREATE TABLE {ch.quote_name(table)} ({listed}) ENGINE = {engine}")
        if rows.get(table):
            body = "\n".join(
                json.dumps(dict(zip(columns, row, strict=True))) for row in rows[table]
            )
            server.run(
                f"INSERT INTO {ch.quote_name(table)} SETTINGS input_format_null_as_default = 0"
                f" FORMAT JSONEachRow\n{body}"
            )


def bound(parameters: Sequence[Mapping[str, str | None]]) -> dict[str, str]:
    """The HTTP form of each parameter, as the store contract's ClickHouse boundary binds it."""
    found: dict[str, str] = {}
    for parameter in parameters:
        value = parameter["value"]
        if value is None:
            text = "\\N"
        elif parameter["type"] == "TIMESTAMPTZ":
            instant = datetime.fromisoformat(value).astimezone(UTC)
            text = instant.strftime("%Y-%m-%d %H:%M:%S.%f")
        else:
            text = value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")
        found[f"param_{parameter['name']}"] = text
    return found


def exact(table: pa.Table, kinds: Sequence[tuple[str, str]]) -> pa.Table:
    """The result in the store contract's Arrow types: text into `HUGEINT`, a wider decimal
    narrowed with overflow checked, a zone label changed; any other type must already match."""
    columns = []
    for (name, kind), values in zip(kinds, table.columns, strict=True):
        target = arrow_type(kind)
        values = values.combine_chunks()
        if pa.types.is_dictionary(values.type):
            values = values.dictionary_decode()
        if pa.types.is_large_string(values.type):
            values = pc.cast(values, pa.utf8())
        if values.type != target:
            assert (
                (kind == "HUGEINT" and pa.types.is_string(values.type))
                or (pa.types.is_decimal(values.type) and values.type.scale == target.scale)
                or (pa.types.is_timestamp(values.type) and values.type.unit == "us")
            ), (name, kind, values.type)
            values = pc.cast(values, target, safe=True)
        columns.append(values)
    return pa.Table.from_arrays(columns, names=[n for n, _ in kinds])


@dataclass(eq=False)  # one connection: its zone checks are cached by identity (`zonedata`)
class OnClickHouse:
    """An enabled ClickHouse engine over the test server (an `ExternalAdapter`)."""

    server: Server
    columns: Mapping[str, Sequence[str]]
    entry: EngineEntry = ENTRY
    engine: str = "clickhouse"
    engine_version: str = "test"
    compiled: dict[str, PhysicalQuery] = field(default_factory=dict)
    statements: list[tuple[str, tuple[Mapping[str, str | None], ...]]] = field(default_factory=list)

    @property
    def connection(self) -> str:
        return self.entry.connection

    def pin(self) -> Mapping[str, object]:
        return {"read_at": "2026-11-08T12:00:00+00:00"}

    def compile(self, found: Any, values: Mapping[int, PolicyValues]) -> PhysicalQuery:  # noqa: ANN401
        query = ch.compile_plan(found, self.columns, values)
        self.compiled[query.sql] = query
        return query

    def read(
        self,
        statement: str,
        parameters: Sequence[Mapping[str, str | None]],
        version: Mapping[str, object],
        batch_size: int,
        timeout_ms: int | None,
    ) -> pa.RecordBatchReader:
        query = self.compiled.get(statement)
        if query is None and statement != ch.ZONE_CHECK_SQL:
            raise AssertionError("a statement this adapter did not compile")
        self.statements.append((statement, tuple(parameters)))
        assert placeholders(statement) == {p["name"] for p in parameters}
        try:
            body = self.server.run(
                statement + "\nFORMAT ArrowStream",
                bound(parameters),
                as_reader=self.server.reader,  # type: ignore[attr-defined]
            )
        except (
            EngineFailure
        ) as failure:  # as the store's boundary maps it (`orvex.store.clickhouse`)
            raise StoreError(
                "STORE_QUERY_FAILED", str(failure), {"engine_code": failure.code}
            ) from None
        kinds = [("#different", "BIGINT")]  # a zone check (C6)
        if query is not None:
            kinds = [(c.name, c.encoding) for c in query.columns]
            kinds += [(n, "BOOLEAN") for n, _ in query.checks]
            kinds += [(n, "BIGINT") for n, _ in [*query.weights, *query.as_of]]
            kinds += [(query.level, "INTEGER")] if query.level else []
            kinds += list(query.hidden)
        table = exact(pyarrow.ipc.open_stream(body).read_all(), kinds)
        return pa.RecordBatchReader.from_batches(table.schema, table.to_batches())

    def estimate(self, query: PhysicalQuery, restricted: bool) -> Mapping[str, int] | None:
        return None

    def evidence(self) -> tuple[()]:
        return ()


def everywhere(compiled: CompiledModel) -> Routing:
    datasets = compiled.ossie.document.get("datasets", ())
    listed = datasets if isinstance(datasets, tuple) else ()
    names = [str(d["name"]) for d in listed if isinstance(d, Mapping)]
    return Routing(dict.fromkeys(names, ENTRY))


@contextmanager
def managed(
    catalog: Mapping[str, Mapping[str, SourceColumn]],
    rows: Mapping[str, Sequence[Sequence[str | None]]],
) -> Iterator[Lake]:
    """The reference: the same rows in the managed lake."""
    with ce.case_lake([], {}) as lake:
        with lake.transaction() as transaction:
            for table, columns in catalog.items():
                listed = ", ".join(f'"{n}" {c.type}' for n, c in columns.items())
                transaction.execute(
                    StatementRequest(
                        kind="statement", sql=f'CREATE TABLE "{table}" ({listed})', parameters=[]
                    )
                )
                for row in rows.get(table, ()):
                    values = ", ".join(
                        f"CAST($v{i} AS {c.type})" for i, c in enumerate(columns.values())
                    )
                    transaction.execute(
                        StatementRequest(
                            kind="statement",
                            sql=f'INSERT INTO "{table}" VALUES ({values})',
                            parameters=[
                                Parameter(name=f"v{i}", type=ParameterType("VARCHAR"), value=v)
                                for i, v in enumerate(row)
                            ],
                        )
                    )
        yield lake


def envelope_and_rows(found: execution.Answer) -> tuple[Any, Any]:
    return [dict(c) for c in found.result.envelope()["columns"]], ce.rows_of(found)


DIALECT_QUESTIONS = {
    "exact sums, products, literals and counts": question(*METRICS),
    "an empty set's sum is NULL": question(
        "total", "big_total", "a_total", "rows", filters=[label_filter("in", "zzz")]
    ),
    "a filtered sum with no row passing is NULL": question(
        "a_total", "a_big_total", "a_rows", group_by=["label"]
    ),
    "every bucket of dates before 1970": question(
        "total", "rows", group_by=["month", "week", "quarter", "year"]
    ),
    "text in byte order, NULL last": question("rows", "total", group_by=["label"]),
    "NULL first when asked": question(
        "rows", group_by=["label"], order=[{"by": "m.label", "direction": "desc", "nulls": "first"}]
    ),
    "NOT IN keeps NULL rows only when asked": question(
        "rows", filters=[label_filter("not_in", "a")]
    ),
    "NOT IN excluding NULL": question(
        "rows", filters=[label_filter("not_in", "a", exclude_null=True)]
    ),
    "IN with NULL included": question("rows", filters=[label_filter("in", "b", include_null=True)]),
    "an IN list in an expression": question("rows", group_by=["small"]),
    "a NOT IN list holding NULL": question("rows", group_by=["unlisted"]),
}


@pytest.mark.parametrize("document", DIALECT_QUESTIONS.values(), ids=list(DIALECT_QUESTIONS))
def test_each_dialect_difference_answers_as_the_managed_lake(
    server: Server, document: Document
) -> None:
    load(server, DIALECT, ROWS)
    adapter = OnClickHouse(server, names_of(DIALECT))
    with managed(DIALECT, ROWS) as lake:
        policy = no_policies(MODEL)
        context = ce.viewer_context()
        reference = answer(json.dumps(document), MODEL, policy, context, ce.engine_for(lake))
        found = answer(
            json.dumps(document),
            MODEL,
            policy,
            context,
            ce.engine_for(lake, routing=everywhere(MODEL), adapters={adapter.connection: adapter}),
        )
    assert envelope_and_rows(found) == envelope_and_rows(reference)
    assert adapter.statements  # read on ClickHouse, not on the lake


def test_explain_estimate_drives_the_budget_on_mergetree_and_not_on_memory(
    server: Server,
) -> None:
    query = written(question("total"))
    for engine, expected in (("MergeTree ORDER BY id", len(ROWS["m"])), ("Memory", None)):
        load(server, DIALECT, ROWS, engine)
        body = server.run(
            ch.estimate_statement(query) + "\nFORMAT JSONEachRow",
            bound(query.parameters),
            as_reader=server.reader,  # type: ignore[attr-defined]
        )
        rows = [json.loads(line) for line in body.decode().splitlines() if line]
        assert ch.rows_scanned(query, rows, server.database) == expected
    with pytest.raises(SemanticError) as caught:
        ch.refuse_over_budget(4, 3, restricted=False)
    assert caught.value.code == "QUERY_BUDGET_EXCEEDED"


# Policies: the managed lake resolves each mapping set; ClickHouse runs the restriction.

POLICY_ROWS: dict[str, list[list[str | None]]] = {
    table: [[v or None for v in row] for row in rows] for table, rows in rp.DATA.items()
}


def ask_policy(
    server: Server, rules: list[Document], attributes: Mapping[str, Any], changed: Document
) -> tuple[Any, Any, OnClickHouse]:
    policy = rp.policies(*rules)
    context = rp.caller(**attributes)
    load(server, rp.CATALOG, POLICY_ROWS)
    adapter = OnClickHouse(server, names_of(rp.CATALOG))
    with rp.lake_of(mappings=rp.MAPPINGS | changed) as lake:
        reference = answer(
            json.dumps(rp.query()), rp.COMPILED, policy, context, ce.engine_for(lake)
        )
        found = answer(
            json.dumps(rp.query()),
            rp.COMPILED,
            policy,
            context,
            ce.engine_for(
                lake, routing=everywhere(rp.COMPILED), adapters={adapter.connection: adapter}
            ),
        )
    return envelope_and_rows(found), envelope_and_rows(reference), adapter


@pytest.mark.parametrize(
    ("label", "rules", "attributes", "changed"), dq.CASES, ids=[c[0] for c in dq.CASES]
)
def test_a_row_policy_restricts_on_clickhouse_as_on_the_managed_lake(
    server: Server,
    label: str,
    rules: list[Document],
    attributes: dict[str, Any],
    changed: dict[str, Any],
) -> None:
    found, reference, adapter = ask_policy(server, rules, attributes, changed)
    assert found == reference
    for statement, parameters in adapter.statements:
        assert '"security"' not in statement
        assert "u-1" not in statement
        assert placeholders(statement) == {p["name"] for p in parameters}


def test_a_null_in_a_deny_list_returns_no_rows(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = PolicyValues("BIGINT", ("2", None))
    real = external.resolve

    def with_null(*arguments: Any) -> Any:  # noqa: ANN401 - the lake's set, plus a NULL
        found, queries = real(*arguments)
        return {k: values if not v.type.startswith("BOOLEAN") else v for k, v in found.items()}, (
            queries
        )

    monkeypatch.setattr(execution, "resolve", with_null)  # where `_execute` looks it up
    deny = rp.DENY | {
        "predicate": "customer_id NOT IN (SELECT zone_id FROM security.user_zone"
        " WHERE user_id = $user_id)"
    }
    found, _, adapter = ask_policy(server, [deny], {"user_id": "u-1"}, {})
    assert found[1] == []
    assert adapter.statements


def test_values_over_the_cap_are_refused_without_their_count(server: Server) -> None:
    load(server, rp.CATALOG, POLICY_ROWS)
    adapter = OnClickHouse(server, names_of(rp.CATALOG))
    many = {"security.user_zone": [("u-1", 1), ("u-1", 2), ("u-1", 3)]}
    with rp.lake_of(mappings=rp.MAPPINGS | many) as lake, pytest.raises(SemanticError) as caught:
        answer(
            json.dumps(rp.query()),
            rp.COMPILED,
            rp.policies(dq.ZONE, limits={"policy_values": 2}),
            rp.caller(user_id="u-1"),
            ce.engine_for(
                lake, routing=everywhere(rp.COMPILED), adapters={adapter.connection: adapter}
            ),
        )
    assert caught.value.code == "POLICY_NOT_SUPPORTED_ON_ENGINE"
    assert dict(caught.value.details) == {
        "dataset": "sales",
        "policy": "own_zone",
        "reason": "value_cap",
    }
    assert adapter.statements == []


@pytest.mark.parametrize("case", ANSWERED)
def test_a_corpus_case_answers_on_clickhouse_as_on_the_managed_lake(
    server: Server, case: str
) -> None:
    model = CORPUS / "models" / f"{case}.yaml"
    catalog = ce.catalog_of(model)
    rows = {
        path.stem: [
            [ce._month_start(row[name], catalog[path.stem][name].type) for name in row]
            for row in bi_platform_adapter.read_csv(path)
        ]
        for path in (CORPUS / "corpus/cases" / case / "input").glob("*.csv")
        if path.stem in catalog
    }
    load(server, {t: catalog[t] for t in rows}, rows)
    adapter = OnClickHouse(server, names_of(catalog))
    compared: list[bool] = []

    def both(
        query: str,
        compiled: CompiledModel,
        policies: PolicyModel,
        context: Mapping[str, Any],
        lake: Lake,
    ) -> ce.Answered:
        reference = answer(query, compiled, policies, context, ce.engine_for(lake))
        routed = ce.engine_for(
            lake, routing=everywhere(compiled), adapters={adapter.connection: adapter}
        )
        found = answer(query, compiled, policies, context, routed)
        compared.append(envelope_and_rows(found) == envelope_and_rows(reference))
        return ce.Answered(found.result.envelope(), ce.rows_of(found))

    entry = next(c for c in MANIFEST["cases"] if c["id"] == case)
    outcome = ce.run_case(
        case, CORPUS / "corpus/cases" / case, model, entry.get("error_code"), ask=both
    )
    assert (outcome.status, compared) == ("pass", [True]), outcome.reason
    assert adapter.statements


# Bounds: no result leaves its declared type without an error, as on the managed lake.

HUGE = str(2**127 - 1)
UHUGE = str(2**128 - 1)
BOUNDS = {
    "b": {
        "id": SourceColumn("BIGINT", False),
        "k": SourceColumn("VARCHAR", False),
        "i32": SourceColumn("INTEGER", False),
        "i64": SourceColumn("BIGINT", False),
        "i128": SourceColumn("HUGEINT", False),
        "u128": SourceColumn("UHUGEINT", False),
        "d9": SourceColumn("DECIMAL(9,2)", False),
        "d18": SourceColumn("DECIMAL(18,2)", False),
        "d38": SourceColumn("DECIMAL(38,0)", False),
        "ts": SourceColumn("TIMESTAMP WITH TIME ZONE", True),
    }
}
BOUND_ROWS: dict[str, list[list[str | None]]] = {
    # Each type's largest value three times (three wrap back into range in 128 bits), its
    # smallest once, and small values.
    "b": [
        ["1", "max", str(2**31 - 1), str(2**63 - 1), HUGE, UHUGE, "9999999.99",
         "9999999999999999.99", "9" * 38, "1969-07-20 20:17:40+00"],
        ["2", "max2", str(2**31 - 1), str(2**63 - 1), HUGE, UHUGE, "9999999.99",
         "9999999999999999.99", "9" * 38, "2106-02-07 06:28:16+00"],
        ["5", "max3", str(2**31 - 1), str(2**63 - 1), HUGE, UHUGE, "9999999.99",
         "9999999999999999.99", "9" * 38, "2000-01-01 00:00:00+00"],
        # x100 lands at 44 past 2^128: an Int128 product wraps into range there.
        ["6", "wrap", "1", "1", "1", "1", "0.01", "0.01", str(-(-(2**128) // 100)), None],
        ["3", "min", str(-(2**31)), str(-(2**63)), str(-(2**127)), "0", "-9999999.99",
         "-9999999999999999.99", "-" + "9" * 38, None],
        ["4", "small", "3", "5", "7", "11", "0.01", "0.02", "13", "2024-02-29 23:59:59.999999+00"],
    ]
}  # fmt: skip
BOUND_FIELDS = {
    "i32_twice": "i32 * 2",
    "i64_twice": "i64 * 2",
    "i128_next": "i128 + 1",
    "i128_negated": "-i128",
    "d38_next": "d38 + 1",
    "d9_twice": "d9 * 2",
    "i32_d9": "CAST(i32 AS DECIMAL(9, 2))",
    "i32_d5": "CAST(i32 AS DECIMAL(5, 2))",
    "i64_d18": "CAST(i64 AS DECIMAL(18, 2))",
    "u128_squared": "u128 * u128",  # past 76 digits: refused, never wrapped
}
BOUND_METRICS = {
    **{f"sum_{name}": f"SUM(b.{name})" for name in ("i32", "i64", "i128", "u128", "d9", "d18")},
    **{f"sum_{name}": f"SUM(b.{name})" for name in ("d38", *BOUND_FIELDS)},
    "rows": "COUNT(*)",
    "d38_per_row": "SUM(b.d38) / COUNT(b.id)",  # at scale 2: the numerator times 10^2
}
PER_ROW = {
    "element": "metric",
    "additivity": "recomputable",
    "components": {"numerator": "SUM(b.d38)", "denominator": "COUNT(b.id)"},
    "zero_denominator": "null",
    "rounding": {"scale": 2, "mode": "half_away_from_zero"},
}


def bounds_model() -> CompiledModel:
    columns = BOUNDS["b"]
    fields = [{"name": c, "expression": {"sql": c}} for c in columns]
    fields += [{"name": n, "expression": {"sql": e}} for n, e in BOUND_FIELDS.items()]
    home = {"element": "metric", "home_dataset": "b"}
    document = {
        "version": "0.2.0.dev0",
        "name": "bounds",
        "datasets": [{"name": "b", "source": "b", "primary_key": ["id"], "fields": fields}],
        "metrics": [
            {"name": n, "expression": {"sql": e}}
            | ({"custom_extensions": [extension(home)]} if "(*)" in e else {})
            | ({"custom_extensions": [extension(PER_ROW)]} if " / " in e else {})
            for n, e in BOUND_METRICS.items()
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/bounds.ossie.json", VENDORS)
    return compile_model(loaded, BOUNDS, VENDORS)


BOUNDS_MODEL = bounds_model()


def of_rows(metric: str, *keys: str, **more: Any) -> Document:  # noqa: ANN401
    keep = {"type": "text", "value": ""}
    values = [keep | {"value": k} for k in keys]
    board = {"scope": "board", "member": "b.k", "predicate": {"op": "in", "values": values}}
    return question(metric, filters=[board], **more)


def both_bounds(server: Server, document: Document) -> tuple[Any, Any]:
    """(ClickHouse's outcome, the managed lake's): the envelope and rows, or the error."""
    load(server, BOUNDS, BOUND_ROWS)
    adapter = OnClickHouse(server, names_of(BOUNDS))
    outcomes: list[Any] = []
    with managed(BOUNDS, BOUND_ROWS) as lake:
        for engine in (
            ce.engine_for(
                lake, routing=everywhere(BOUNDS_MODEL), adapters={adapter.connection: adapter}
            ),
            ce.engine_for(lake),
        ):
            try:
                found = answer(
                    json.dumps(document),
                    BOUNDS_MODEL,
                    no_policies(BOUNDS_MODEL),
                    ce.viewer_context(),
                    engine,
                )
                outcomes.append(envelope_and_rows(found))
            except (StoreError, PlanError) as error:
                outcomes.append(error)
    assert adapter.statements  # asked on ClickHouse
    return outcomes[0], outcomes[1]


SINCE_1970 = {"op": "range", "gte": {"type": "timestamp", "value": "1970-01-01T00:00:00Z"}}


def bound_question(*metrics: str, group_by: Sequence[str] = ()) -> Document:
    found = question(*metrics)
    if group_by:
        found["group_by"] = [{"member": f"b.{g}"} for g in group_by]
    return found


OVERFLOWS = {  # the reference raises an overflow for each
    "SUM of HUGEINT": of_rows("sum_i128", "max", "max2", "max3"),
    "SUM of DECIMAL(38,0)": of_rows("sum_d38", "max", "max2", "max3"),
    "INTEGER product": of_rows("sum_i32_twice", "max"),
    "BIGINT product": of_rows("sum_i64_twice", "max"),
    "HUGEINT increment": of_rows("sum_i128_next", "max"),
    "HUGEINT negated": of_rows("sum_i128_negated", "min"),
    "DECIMAL(38,0) increment": of_rows("sum_d38_next", "max"),
    "INTEGER as DECIMAL(9,2)": of_rows("sum_i32_d9", "max"),
    "INTEGER as DECIMAL(5,2)": of_rows("sum_i32_d5", "small", "max"),
    "BIGINT as DECIMAL(18,2)": of_rows("sum_i64_d18", "min"),
    "a ratio scaled past HUGEINT": of_rows("d38_per_row", "wrap"),
}
FITS = {  # each at its type's edge, identical to the reference
    "SUM of INTEGER and BIGINT maxima": bound_question("sum_i32", "sum_i64"),
    "SUM of DECIMAL(9,2) and DECIMAL(18,2)": bound_question("sum_d9", "sum_d18", "sum_d9_twice"),
    # A HUGEINT result travels in 38 digits (`contracts/store/v1`), so its edge is that.
    "DECIMAL(38,0) at its edges": of_rows("sum_d38", "max", "small", "min")
    | {"group_by": [{"member": "b.id"}]},
    "products in the reference's own type": bound_question("sum_d9_twice", "sum_i32"),
    "INTEGER as DECIMAL(5,2) when it fits": of_rows("sum_i32_d5", "small"),
    "a TIMESTAMP WITH TIME ZONE key": bound_question("rows", group_by=["ts"]),
    "a TIMESTAMP WITH TIME ZONE bound": bound_question("rows")
    | {"filters": [{"scope": "board", "member": "b.ts", "predicate": SINCE_1970}]},
}


@pytest.mark.parametrize("document", OVERFLOWS.values(), ids=list(OVERFLOWS))
def test_a_value_past_its_declared_type_is_an_error_as_on_the_managed_lake(
    server: Server, document: Document
) -> None:
    found, reference = both_bounds(server, document)
    assert isinstance(reference, StoreError), reference
    assert re.search("Overflow|Could not cast", str(reference.details)), reference.details
    assert isinstance(found, StoreError), found  # never a wrapped or widened value
    assert found.code == reference.code == "STORE_QUERY_FAILED"
    assert dict(found.details)["engine_code"] == "395", found  # throwIf, `exact.checked`
    assert ch.OUT_OF_RANGE in str(found)


@pytest.mark.parametrize("document", FITS.values(), ids=list(FITS))
def test_a_value_at_its_declared_type_s_edge_answers_as_the_managed_lake(
    server: Server, document: Document
) -> None:
    found, reference = both_bounds(server, document)
    assert not isinstance(reference, Exception), reference
    assert found == reference


def test_a_sum_of_uhugeint_is_exact_or_an_error_never_wrapped(server: Server) -> None:
    """`SUM(UHUGEINT)` is a `HUGEINT` (the type checker's rule). The reference answers it as a
    `DOUBLE`, which it refuses (a defect of DuckDB recorded apart), so ClickHouse is held to
    the declared type alone: exact within it, an error past it."""
    small, _ = both_bounds(server, of_rows("sum_u128", "small", "min"))
    assert small[1] == [["11"]]
    wide, reference = both_bounds(server, of_rows("sum_u128", "max"))
    assert isinstance(reference, PlanError)  # DuckDB's DOUBLE
    assert isinstance(wide, StoreError)
    assert dict(wide.details)["engine_code"] == "395"


def test_each_spelling_of_a_store_type_is_one_clickhouse_type() -> None:
    for spelling in ("TIMESTAMPTZ", "timestamp with  time zone", "TIMESTAMP WITH TIME ZONE"):
        assert ch.engine_type(spelling) == "DateTime64(6, 'UTC')"
    assert ch.engine_type("decimal(18, 2)") == "Decimal(18, 2)"
    with pytest.raises(SemanticError):
        ch.engine_type("TIMESTAMP")  # no zone: no exact ClickHouse form here


def test_a_checked_value_stops_past_its_range_and_its_precision() -> None:
    integer = ch_exact.checked("x", "INTEGER")
    assert "toDecimal256('-2147483648', 0)" in integer
    assert "toDecimal256('2147483647', 0)" in integer
    assert integer.endswith(" AS Int32)")
    decimal = ch_exact.checked("x", "DECIMAL(5,2)")  # P too: no ClickHouse cast tests it
    assert "abs(toDecimal256(x, 2)) >= toDecimal256('1000', 0)" in decimal
    outside = "coalesce(abs(toDecimal256(x, 2)) >= toDecimal256('1000', 0), false)"
    assert f"throwIf({outside}, '{ch.OUT_OF_RANGE}')" in decimal
    assert "accurateCast" not in decimal  # it keeps a value past P and refuses NULL
    assert ch_exact.checked("x", "VARCHAR") == "CAST(x AS String)"


def test_a_product_that_could_pass_76_digits_is_refused() -> None:
    with pytest.raises(SemanticError) as caught:
        ch.compile_plan(plan_of(question("sum_u128_squared"), BOUNDS_MODEL), names_of(BOUNDS), {})
    assert caught.value.code == "UNSUPPORTED_OPERATION"


def test_a_power_of_ten_past_hugeint_is_checked_and_never_wrapped() -> None:
    writer = ch_writer._ClickHouse(plan_of(question("rows")), names_of(DIALECT), {})
    assert writer.power(38) == f"CAST('1{'0' * 38}' AS Int128)"
    assert writer.power(39) == ch_exact.checked(f"toDecimal256('1{'0' * 39}', 0)", "HUGEINT")
    assert writer.scaled("v", 2) == ch_exact.checked(
        f"(toInt256(v) * toInt256({writer.power(2)}))", "HUGEINT"
    )
