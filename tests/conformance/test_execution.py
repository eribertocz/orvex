"""P2-12 (#311): the first real answer, end to end through `store`, with every gate first."""

from __future__ import annotations

import inspect
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
import yaml
from orvex.contracts.store.v1 import StatementRequest
from orvex.semantic.adapters import clickhouse
from orvex.semantic.adapters import duckdb as adapter_module
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.errors import PlanError, SemanticError
from orvex.semantic.execution import Answer, answer
from orvex.semantic.ossie.reader import read
from orvex.semantic.planning import SemanticPlan, plan
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.store.errors import StoreError
from orvex.store.json_encoding import arrow_to_json
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from conformance.runners.semantic_corpus import ROOT, run
from tests.support.policies import VENDORS, extension

CASES = ROOT / "conformance/semantic-corpus/bi-platform"
Document = dict[str, Any]


def corpus_case(case: str) -> tuple[CompiledModel, Path, Path]:
    model = CASES / "models" / f"{case}.yaml"
    return ce.compiled_case(model), CASES / "corpus/cases" / case, model


def no_policies(compiled: CompiledModel, **document: Any) -> PolicyModel:  # noqa: ANN401
    return read_policies(
        yaml.safe_dump({"version": 1, **document}), compiled, groups=frozenset(), managed_tables={}
    )


@contextmanager
def lake_for(case: str) -> Iterator[Lake]:
    _, folder, model = corpus_case(case)
    with ce.case_lake(sorted((folder / "input").glob("*.csv")), ce.catalog_of(model)) as lake:
        yield lake


def ask(
    query: Document,
    compiled: CompiledModel,
    lake: Lake,
    policy: PolicyModel | None = None,
    context: dict[str, Any] | None = None,
    **engine: Any,  # noqa: ANN401
) -> Answer:
    return answer(
        json.dumps(query),
        compiled,
        policy or no_policies(compiled),
        context or ce.viewer_context(),
        ce.engine_for(lake, **engine),
    )


REVENUE_BY_REGION = ce.QUERIES["CASE-001"]


# --- CASE-001, end to end ---------------------------------------------------------------------


def test_case_001_answers_exactly_with_its_receipt() -> None:
    compiled, _, _ = corpus_case("CASE-001")
    with lake_for("CASE-001") as lake:
        snapshot = lake.current_snapshot()
        found = ask(REVENUE_BY_REGION, compiled, lake)
    assert ce.rows_of(found) == [["North", "150.00"], ["South", "200.00"]]
    envelope = found.result.envelope()
    assert envelope["receipt"]["reproducibility"] == "exact"
    assert envelope["receipt"]["receipt_id"] == found.receipt.receipt_id
    (data,) = found.receipt.document()["data"]
    assert data["snapshot_id"] == snapshot.id
    assert found.receipt.document()["plan"]["obligations"][0]["obligation"] == (
        "C1.aggregate:revenue"
    )


# --- the corpus runner -------------------------------------------------------------------------


def test_a_forced_mismatch_is_reported_as_fail(tmp_path: Path) -> None:
    from tests.conformance.test_semantic_corpus import copy_repository

    root = copy_repository(tmp_path)
    expected = root / "conformance/semantic-corpus/bi-platform/corpus/cases/CASE-001/expected"
    text = (expected / "result.csv").read_text("utf-8").replace("150.00", "150.01")
    (expected / "result.csv").write_text(text, "utf-8")
    reports, _ = run(root)
    (case,) = [r for r in reports if r.case == "CASE-001"]
    assert case.status == "fail"


# --- values and identifiers --------------------------------------------------------------------

CANARIES = ["'; DROP TABLE x; --", "emoji \U0001f600", "North‮"]
BIG = "9" * 28 + "." + "9" * 10  # 38 digits


@pytest.mark.parametrize("canary", CANARIES)
def test_a_filter_value_is_only_ever_a_parameter(canary: str) -> None:
    compiled, _, _ = corpus_case("CASE-001")
    query = REVENUE_BY_REGION | {
        "filters": [
            {
                "scope": "board",
                "member": "sales.region",
                "predicate": {
                    "op": "in",
                    "values": [
                        {"type": "text", "value": canary},
                        {"type": "text", "value": "South"},
                    ],
                },
            },
            {
                "scope": "board",
                "member": "sales.amount",
                "predicate": {"op": "range", "lte": {"type": "decimal", "value": BIG}},
            },
        ]
    }
    with lake_for("CASE-001") as lake:
        found = ask(query, compiled, lake)
    assert canary not in found.physical.sql
    assert BIG not in found.physical.sql
    assert {p["value"] for p in found.physical.parameters} >= {canary, BIG}
    assert ce.rows_of(found) == [["South", "200.00"]]


def test_a_nul_character_never_reaches_the_sql() -> None:
    compiled, _, _ = corpus_case("CASE-001")
    query = REVENUE_BY_REGION | {
        "filters": [
            {
                "scope": "board",
                "member": "sales.region",
                "predicate": {"op": "in", "values": [{"type": "text", "value": "a\u0000b"}]},
            }
        ]
    }
    with lake_for("CASE-001") as lake:
        try:
            found = ask(query, compiled, lake)
        except SemanticError, StoreError:
            return  # refused before or by the read: never written into the SQL text
    assert "\u0000" not in found.physical.sql


def tiny_model(
    fields: list[tuple[str, str]],
    metric: str,
    extra: list[Document] | None = None,
    columns: list[tuple[str, str]] | None = None,
) -> tuple[CompiledModel, dict[str, Any]]:
    document = {
        "version": "0.2.0.dev0",
        "name": "tiny",
        "datasets": [
            {
                "name": "t",
                "source": "t",
                "primary_key": [fields[0][0]],
                "fields": [{"name": n, "expression": {"sql": n}} for n, _ in fields]
                + (extra or []),
            }
        ],
        "metrics": [{"name": "total", "expression": {"sql": metric}}],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    catalog = {"t": {sql_name(n): SourceColumn(t) for n, t in columns or fields}}
    loaded = read(json.dumps(document).encode(), "semantic/tiny.ossie.json", VENDORS)
    return compile_model(loaded, catalog, VENDORS), catalog


def sql_name(name: str) -> str:
    return name[1:-1].replace('""', '"') if name.startswith('"') else name


@contextmanager
def tiny_lake(catalog: dict[str, Any], rows: list[dict[str, str | None]]) -> Iterator[Lake]:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "t.csv"
        names = list(catalog["t"])
        lines = [",".join(names)] + [",".join(r[n] or "" for n in names) for r in rows]
        path.write_text("\n".join(lines) + "\n", "utf-8")
        with ce.case_lake([path], catalog) as lake:
            yield lake


def test_a_name_in_a_field_is_its_column_never_another_field() -> None:
    """Ossie "Name Resolution": `list_price: price` reads the column `price`, not the field
    `price` (which doubles it)."""
    compiled, catalog = tiny_model(
        [("id", "BIGINT"), ("g", "VARCHAR")],
        "SUM(t.list_price)",
        [
            {"name": "list_price", "expression": {"sql": "price"}},
            {"name": "price", "expression": {"sql": "price * 2"}},
        ],
        columns=[("id", "BIGINT"), ("g", "VARCHAR"), ("price", "DECIMAL(18,2)")],
    )
    rows: list[dict[str, str | None]] = [
        {"id": "1", "g": "a", "price": "1.50"},
        {"id": "2", "g": "a", "price": "2.25"},
    ]
    query = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "total"}],
        "group_by": [{"member": "t.g"}],
    }
    with tiny_lake(catalog, rows) as lake:
        assert ce.rows_of(ask(query, compiled, lake)) == [["a", "3.75"]]


def test_a_boolean_filter_runs() -> None:
    fields = [("id", "BIGINT"), ("g", "VARCHAR"), ("ok", "BOOLEAN"), ("amount", "DECIMAL(18,2)")]
    compiled, catalog = tiny_model(fields, "SUM(t.amount)")
    rows: list[dict[str, str | None]] = [
        {"id": "1", "g": "a", "ok": "true", "amount": "1.00"},
        {"id": "2", "g": "a", "ok": "false", "amount": "2.00"},
    ]
    query = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "total"}],
        "group_by": [{"member": "t.g"}],
        "filters": [
            {
                "scope": "board",
                "member": "t.ok",
                "predicate": {"op": "in", "values": [{"type": "boolean", "value": True}]},
            }
        ],
    }
    with tiny_lake(catalog, rows) as lake:
        assert ce.rows_of(ask(query, compiled, lake)) == [["a", "1.00"]]


def test_odd_identifiers_are_quoted() -> None:
    fields = [
        ("id", "BIGINT"),
        ('"a""b"', "VARCHAR"),
        ('"select"', "VARCHAR"),
        ("amount", "DECIMAL(18,2)"),
    ]
    compiled, catalog = tiny_model(fields, "SUM(t.amount)")
    rows: list[dict[str, str | None]] = [
        {"id": "1", 'a"b': "x", "select": "y", "amount": "1.50"},
        {"id": "2", 'a"b': "x", "select": "y", "amount": "2.25"},
    ]
    query = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "total"}],
        "group_by": [{"member": 't."a""b"'}, {"member": 't."select"'}],
    }
    with tiny_lake(catalog, rows) as lake:
        found = ask(query, compiled, lake)
    assert '"a""b"' in found.physical.sql
    assert ce.rows_of(found) == [["x", "y", "3.75"]]


def test_exact_values_and_nulls_stay_exact() -> None:
    fields = [("id", "BIGINT"), ("g", "VARCHAR"), ("d", "DECIMAL(38,10)"), ("h", "HUGEINT")]
    for metric, expected in (
        ("SUM(t.d)", [["a", "123456789012345678.0123456789"], ["b", None]]),
        ("SUM(t.h)", [["a", "170141183460469231731687303715884105000"], ["b", None]]),
    ):
        compiled, catalog = tiny_model(fields, metric)
        rows: list[dict[str, str | None]] = [
            {
                "id": "1",
                "g": "a",
                "d": "123456789012345678.0123456788",
                "h": "170141183460469231731687303715884104999",
            },
            {"id": "2", "g": "a", "d": "0.0000000001", "h": "1"},
            {"id": "3", "g": "b", "d": None, "h": None},
        ]
        query = {
            "kind": "semantic_query",
            "version": 1,
            "mode": "aggregate",
            "metrics": [{"metric": "total"}],
            "group_by": [{"member": "t.g"}],
        }
        with tiny_lake(catalog, rows) as lake:
            assert ce.rows_of(ask(query, compiled, lake)) == expected, metric


# --- the snapshot --------------------------------------------------------------------------------


def test_a_commit_during_the_read_changes_nothing() -> None:
    compiled, _, _ = corpus_case("CASE-001")
    with lake_for("CASE-001") as lake:
        before = lake.current_snapshot()

        def read_then_commit(request: Any) -> pa.RecordBatchReader:  # noqa: ANN401
            reader = lake.read(request)
            with lake.transaction() as transaction:
                transaction.execute(
                    StatementRequest(
                        kind="statement",
                        sql="INSERT INTO sales VALUES (9, 'North', 1000.00)",
                        parameters=[],
                    )
                )
            return reader

        found = ask(REVENUE_BY_REGION, compiled, lake, read=read_then_commit)
        assert lake.current_snapshot().id != before.id
    assert ce.rows_of(found) == [["North", "150.00"], ["South", "200.00"]]
    assert found.receipt.document()["data"][0]["snapshot_id"] == before.id


def test_a_missing_or_unknown_snapshot_reads_nothing() -> None:
    compiled, _, _ = corpus_case("CASE-001")
    with lake_for("CASE-001") as lake:
        with pytest.raises(PlanError):
            ask(REVENUE_BY_REGION, compiled, lake, snapshot={})
        with pytest.raises(StoreError) as caught:
            ask(
                REVENUE_BY_REGION,
                compiled,
                lake,
                snapshot={"id": 10_000, "committed_at": "2026-11-01T00:00:00+00:00"},
            )
    assert caught.value.code in ("STORE_SNAPSHOT_NOT_FOUND", "STORE_SNAPSHOT_EXPIRED")


@pytest.mark.parametrize("code", ["STORE_SNAPSHOT_EXPIRED", "STORE_TIMEOUT"])
def test_a_read_failing_midway_leaves_no_result(code: str) -> None:
    compiled, _, _ = corpus_case("CASE-001")
    with lake_for("CASE-001") as lake:

        def failing(request: Any) -> pa.RecordBatchReader:  # noqa: ANN401
            schema = lake.read(request).schema

            def batches() -> Iterator[pa.RecordBatch]:
                yield pa.record_batch(
                    [pa.array(["North"]), pa.array([1], pa.decimal128(38, 2))], schema=schema
                )
                raise StoreError(code, "stopped", {})

            return pa.RecordBatchReader.from_batches(schema, batches())

        with pytest.raises(StoreError) as caught:
            ask(REVENUE_BY_REGION, compiled, lake, read=failing)
    assert caught.value.code == code


# --- identities ----------------------------------------------------------------------------------


def test_two_runs_share_the_execution_identity_but_not_necessarily_the_receipt() -> None:
    compiled, _, _ = corpus_case("CASE-001")
    ticks = iter([0.0, 0.010, 5.0, 5.250])
    with lake_for("CASE-001") as lake:
        one = ask(REVENUE_BY_REGION, compiled, lake, clock=lambda: next(ticks))
        two = ask(REVENUE_BY_REGION, compiled, lake, clock=lambda: next(ticks))
        with lake.transaction() as transaction:
            transaction.execute(
                StatementRequest(
                    kind="statement",
                    sql="INSERT INTO sales VALUES (9, 'West', 1.00)",
                    parameters=[],
                )
            )
        three = ask(REVENUE_BY_REGION, compiled, lake)
    identity = [a.receipt.document()["execution_fingerprint"] for a in (one, two, three)]
    assert identity[0] == identity[1] != identity[2]
    assert one.result.canonical(arrow_to_json) == two.result.canonical(arrow_to_json)
    assert one.receipt.receipt_id != two.receipt.receipt_id  # timings differ
    assert one.plan.fingerprint == two.plan.fingerprint


# --- engine independence -------------------------------------------------------------------------


class FakeAdapter:
    """A second adapter: it writes nothing real, only proves the plan carries no dialect."""

    @staticmethod
    def compile_plan(found: SemanticPlan) -> str:
        return " | ".join(
            [f"scan {s.table}" for s in found.sources]
            + [f"sum {a.metric.name}" for a in found.aggregates]
            + [f"by {g.name}" for g in found.groups]
        )


def test_the_plan_holds_no_sql_and_fits_any_adapter() -> None:
    compiled, _, _ = corpus_case("CASE-001")
    with lake_for("CASE-001") as lake:
        found = ask(REVENUE_BY_REGION, compiled, lake)
    plan_document = json.dumps(
        [found.plan.sources, found.plan.groups, found.plan.aggregates], default=str
    )
    assert "SELECT" not in plan_document.upper()
    assert FakeAdapter.compile_plan(found.plan) == "scan sales | sum REVENUE | by region"
    assert adapter_module.compile_plan(found.plan).plan_fingerprint == found.plan.fingerprint


# --- never bypass --------------------------------------------------------------------------------


class Spy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: Any) -> pa.RecordBatchReader:  # noqa: ANN401
        self.calls += 1
        raise AssertionError("a refused question must not read")


def test_a_row_policy_restricts_the_rows_with_the_caller_s_attribute() -> None:
    """P2-21 (#341): CASE-015's tenant sees only its rows; the policy is in the receipt."""
    compiled, _, model = corpus_case("CASE-015")
    policy = read_policies(
        yaml.safe_dump(ce.policies_of(model)), compiled, groups=frozenset(), managed_tables={}
    )
    tenant = ce.viewer_context(attributes={"tenant_id": {"type": "text", "value": "A"}})
    with lake_for("CASE-015") as lake:
        found = ask(ce.QUERIES["CASE-015"], compiled, lake, policy=policy, context=tenant)
        other = ce.viewer_context(attributes={"tenant_id": {"type": "text", "value": "B"}})
        theirs = ask(ce.QUERIES["CASE-015"], compiled, lake, policy=policy, context=other)
        reads: list[object] = []

        def counted(request: Any) -> Any:  # noqa: ANN401
            reads.append(request)
            return lake.read(request)

        missing = ask(ce.QUERIES["CASE-015"], compiled, lake, policy=policy, read=counted)
    assert ce.rows_of(found) == [["North", "100.00"], ["South", "50.00"]]
    assert ce.rows_of(theirs) != ce.rows_of(found)
    assert "$p" in found.physical.sql  # always bound
    assert "'A'" not in found.physical.sql
    used = found.receipt.document()["security"]["policies"]
    assert [(p["kind"], p["applied"]) for p in used] == [("row", "restricted")]
    # No `tenant_id` in the context: zero rows and the generic notice, never all rows.
    assert ce.rows_of(missing) == []
    assert [n["code"] for n in missing.result.envelope()["notices"]] == ["POLICY_ATTRIBUTE_MISSING"]
    assert len(reads) == 1


@pytest.mark.parametrize(
    ("policy", "query", "code"),
    [
        (
            {"column_policies": [{"member": "sales.region", "default": "hidden"}]},
            REVENUE_BY_REGION,
            "MEMBER_NOT_FOUND",
        ),
        (
            {"column_policies": [{"member": "sales.region", "default": "masked", "mask": "*"}]},
            REVENUE_BY_REGION,
            "MEMBER_MASKED",
        ),
        (
            {
                "aggregate_thresholds": [
                    {"dataset": "sales", "entity": "sales.sale_id", "min_group_size": 3}
                ]
            },
            REVENUE_BY_REGION,
            "UNSUPPORTED_OPERATION",
        ),
    ],
)
def test_hidden_masked_and_unusable_are_refused_before_any_read(
    policy: Document, query: Document, code: str
) -> None:
    compiled, _, _ = corpus_case("CASE-001")
    spy = Spy()
    with lake_for("CASE-001") as lake, pytest.raises(SemanticError) as caught:
        ask(query, compiled, lake, policy=no_policies(compiled, **policy), read=spy)
    assert caught.value.code == code
    assert spy.calls == 0


SQL_OR_DATA = {
    "orvex.semantic.execution": {"answer", "prepare"},
    "orvex.semantic.explanation": {"explain"},
    "orvex.semantic.planning": {"plan"},
    # `mapping_queries` (P2-29): a plan's mapping subqueries, for the managed lake.
    "orvex.semantic.adapters.duckdb": {"compile_plan", "mapping_queries"},
    # The ClickHouse dialect (P2-31, #225): only a plan the planner issued.
    "orvex.semantic.adapters.clickhouse": {"compile_plan"},
}


def test_every_public_path_to_sql_or_data_needs_the_policies_and_the_caller() -> None:
    import importlib
    import pkgutil

    import orvex.semantic

    # Every public function of the package that writes SQL or reads data is listed here.
    found: dict[str, set[str]] = {}
    for module in pkgutil.walk_packages(orvex.semantic.__path__, "orvex.semantic."):
        loaded = importlib.import_module(module.name)
        for name, value in vars(loaded).items():
            if name.startswith("_") or not inspect.isfunction(value):
                continue
            if value.__module__ != module.name:
                continue
            source = inspect.getsource(value)
            tokens = ("compile_plan(", ".read(", "_Writer(", "_Planner(", "prepare(")
            if any(t in source for t in tokens):
                found.setdefault(module.name, set()).add(name)
    assert found == SQL_OR_DATA
    # Every path from a question takes the policies and the caller (#364: `prepare`, and
    # `explain`, which never reads).
    from orvex.semantic.execution import prepare
    from orvex.semantic.explanation import explain

    for entry in (answer, prepare, explain):
        assert {"policies", "context"} <= set(inspect.signature(entry).parameters)
    # The other two accept only what `analyze` (policies and caller) and `plan` made.
    compiled, _, _ = corpus_case("CASE-001")
    with lake_for("CASE-001") as lake:
        real = ask(REVENUE_BY_REGION, compiled, lake)
    from dataclasses import replace

    from orvex.semantic.query.binding import Limits, bind_query

    unseen = bind_query(
        compiled,
        json.dumps(REVENUE_BY_REGION),
        instant=ce.INSTANT,
        limits=Limits(detail_page_size=1000),
        hidden=lambda member: False,
    )
    for forged_report in (replace(real.report, bound=unseen), replace(real.report)):
        with pytest.raises(PlanError):
            plan(forged_report)
    for forged_plan in (replace(real.plan, filters=(), groups=()), replace(real.plan)):
        with pytest.raises(PlanError):
            adapter_module.compile_plan(forged_plan)
        with pytest.raises(PlanError):
            adapter_module.mapping_queries(forged_plan)
        with pytest.raises(PlanError):
            clickhouse.compile_plan(forged_plan, {"sales": ["region", "amount"]}, {})


def test_two_spellings_of_one_member_give_one_answer() -> None:
    compiled, _, _ = corpus_case("CASE-001")
    other = REVENUE_BY_REGION | {"group_by": [{"member": '"SALES"."REGION"'}]}
    with lake_for("CASE-001") as lake:
        one, two = ask(REVENUE_BY_REGION, compiled, lake), ask(other, compiled, lake)
    assert one.plan.fingerprint == two.plan.fingerprint
    assert ce.rows_of(one) == ce.rows_of(two)
