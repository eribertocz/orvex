"""The Direct Query foundation (P2-29, #370; ADR-0027 §3 to §6), with simulated adapters.

No engine is enabled in this release: these tests supply an adapter that runs the plan on a
second lake holding only the datasets' tables, so a statement that read a managed mapping table
would fail there. Every answer is compared with the same question on the managed lake.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import pyarrow as pa
import pytest
import yaml
from orvex.contracts.store.v1 import Parameter, ReadRequest, StatementRequest
from orvex.semantic.adapters.duckdb import PhysicalQuery, PolicyValues, compile_plan
from orvex.semantic.compiler.compiled import compile_model
from orvex.semantic.engines import (
    CAPABILITIES,
    EngineCatalog,
    EngineEntry,
    Routing,
    route_model,
)
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import Answer, Progress, answer, prepare
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.receipt.viewer import CallerViewer
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.conformance import test_row_policies as rp
from tests.conformance.test_execution import Spy, ask
from tests.support.policies import VENDORS, extension

Document = dict[str, Any]
ROOT = Path(__file__).resolve().parents[2]
RECEIPT = json.loads((ROOT / "contracts/receipt/v1/schema.json").read_text("utf-8"))
PROJECT = "warehouse-project"
# A credential a connection could carry: it must never reach a statement or a receipt.
SECRET = "s3cr3t-canary-7f1d"
ENTRY = EngineEntry(
    "warehouse",
    "ducklake",
    "existing_ducklake",
    "ducklake://warehouse.example/catalog",
    frozenset(CAPABILITIES - {"detail_pages"}),
)
LIVE = EngineEntry("stream", "ducklake", "live", "live://stream.example/db", ENTRY.capabilities)


@dataclass
class Simulated:
    """An enabled engine of `entry`'s kind, over its own lake: what it compiled, read and
    pinned is recorded."""

    lake: Lake
    entry: EngineEntry = ENTRY
    engine: str = "ducklake"
    engine_version: str = "simulated-1"
    pins: int = 0
    values: list[Mapping[int, PolicyValues]] = field(default_factory=list)
    statements: list[tuple[str, tuple[Mapping[str, str | None], ...], Mapping[str, object]]] = (
        field(default_factory=list)
    )

    @property
    def connection(self) -> str:
        return self.entry.connection

    def pin(self) -> Mapping[str, object]:
        self.pins += 1
        snapshot = self.lake.current_snapshot()
        if self.entry.route == "live":
            return {"read_at": "2026-11-07T12:00:00+00:00"}
        return {
            "id": snapshot.id,
            "committed_at": snapshot.committed_at.isoformat(),
            "project": PROJECT,
        }

    def compile(self, plan: Any, values: Mapping[int, PolicyValues]) -> PhysicalQuery:  # noqa: ANN401
        self.values.append(dict(values))
        return compile_plan(plan, values)

    def read(
        self,
        statement: str,
        parameters: Sequence[Mapping[str, str | None]],
        version: Mapping[str, object],
        batch_size: int,
        timeout_ms: int | None,
    ) -> pa.RecordBatchReader:
        self.statements.append((statement, tuple(parameters), version))
        snapshot = int(str(version.get("id", self.lake.current_snapshot().id)))
        request = ReadRequest(
            kind="read",
            sql=statement,
            parameters=[Parameter.model_validate(dict(p)) for p in parameters],
            snapshot=snapshot,
            batch_size=batch_size,
            timeout_ms=timeout_ms,
        )
        return self.lake.read(request)


@contextmanager
def lakes(**mappings: Any) -> Iterator[tuple[Lake, Lake]]:  # noqa: ANN401
    """The managed lake (datasets and mapping tables) and the other engine's (datasets only)."""
    with rp.lake_of(**mappings) as managed, rp.lake_of(mappings={}) as other:
        yield managed, other


def routed(*entries: tuple[str, EngineEntry | None]) -> Routing:
    return Routing(dict(entries) or {"sales": ENTRY, "customers": ENTRY})


def ask_on(
    managed: Lake,
    adapter: Simulated | None,
    policy: PolicyModel,
    context: Mapping[str, Any],
    document: Document | None = None,
    routing: Routing | None = None,
    **engine: Any,  # noqa: ANN401
) -> Answer:
    entry = adapter.entry if adapter is not None else ENTRY
    adapters = {adapter.connection: adapter} if adapter is not None else {}
    return answer(
        json.dumps(document or rp.query()),
        rp.COMPILED,
        policy,
        dict(context),
        ce.engine_for(
            managed,
            routing=routing or routed(("sales", entry), ("customers", entry)),
            adapters=adapters,
            **engine,
        ),
    )


ZONE = {
    "name": "own_zone",
    "dataset": "sales",
    "kind": "predicate",
    "predicate": "zone_id IN (SELECT zone_id FROM security.user_zone WHERE user_id = $user_id)",
}
EXISTS = {
    "name": "has_zone",
    "dataset": "sales",
    "kind": "predicate",
    "predicate": "EXISTS (SELECT 1 FROM security.user_zone WHERE user_id = $user_id)",
}
NOT_EXISTS = EXISTS | {
    "name": "no_zone",
    "predicate": "NOT " + str(EXISTS["predicate"]),
    "registration": rp.DENY["registration"],  # a deny-list holds for registered callers
}
CASES: list[tuple[str, list[Document], dict[str, Any], dict[str, Any]]] = [
    ("no policy", [], {}, {}),
    ("attribute predicate", [rp.TENANT], {"tenant": "A"}, {}),
    ("mapping IN", [ZONE], {"user_id": "u-1"}, {}),
    (
        "mapping IN, two values",
        [ZONE],
        {"user_id": "u-1"},
        {"security.user_zone": [("u-1", 1), ("u-1", 2)]},
    ),
    ("mapping IN, none", [ZONE], {"user_id": "u-1"}, {"security.user_zone": []}),
    ("deny-list NOT IN", [rp.DENY], {"user_id": "u-1"}, {}),
    ("deny-list, unregistered", [rp.DENY], {"user_id": "u-1"}, {"security.registered": []}),
    ("deny-list, nothing denied", [rp.DENY], {"user_id": "u-1"}, {"security.user_zone": []}),
    ("EXISTS, matched", [EXISTS], {"user_id": "u-1"}, {}),
    ("EXISTS, unmatched", [EXISTS], {"user_id": "u-2"}, {}),
    ("NOT EXISTS", [NOT_EXISTS], {"user_id": "u-1"}, {}),
    ("NOT EXISTS, unregistered", [NOT_EXISTS], {"user_id": "u-1"}, {"security.registered": []}),
    ("mapping and attribute", [ZONE, rp.TENANT], {"user_id": "u-1", "tenant": "A"}, {}),
    ("missing attribute", [ZONE], {}, {}),
]


@pytest.mark.parametrize(
    ("label", "rules", "attributes", "changed"), CASES, ids=[c[0] for c in CASES]
)
def test_another_engine_answers_exactly_as_the_managed_lake(
    label: str, rules: list[Document], attributes: dict[str, Any], changed: dict[str, Any]
) -> None:
    mappings = rp.MAPPINGS | changed
    policy = rp.policies(*rules)
    context = rp.caller(**attributes)
    with lakes(mappings=mappings) as (managed, other):
        expected = ce.rows_of(ask(rp.query(), rp.COMPILED, managed, policy=policy, context=context))
        adapter = Simulated(other)
        found = ask_on(managed, adapter, policy, context)
    assert ce.rows_of(found) == expected
    assert adapter.pins == 1  # one version for every read of the question
    for statement, parameters, version in adapter.statements:
        assert '"security"' not in statement  # the other engine never reads a managed table
        assert version == adapter.statements[0][2]
        assert len(parameters) == statement.count("$p")  # every value bound, none written


def test_null_in_a_deny_list_matches_nothing_as_in_sql() -> None:
    """`NOT IN` over a set holding `NULL` keeps no row, on both engines."""
    nulls = {
        "security.user_zone": [("u-1", 1)],
        "security.registered": [("u-1",)],
    }
    deny = rp.DENY | {
        "predicate": "customer_id NOT IN (SELECT zone_id FROM security.user_zone"
        " WHERE user_id = $user_id)"
    }
    with lakes(mappings=nulls) as (managed, other):
        with managed.transaction() as transaction:
            transaction.execute(
                StatementRequest(
                    kind="statement",
                    sql="INSERT INTO security.user_zone VALUES ('u-1', NULL)",
                    parameters=[],
                )
            )
        policy = rp.policies(deny)
        context = rp.caller(user_id="u-1")
        expected = ce.rows_of(ask(rp.query(), rp.COMPILED, managed, policy=policy, context=context))
        found = ask_on(managed, Simulated(other), policy, context)
    assert expected == []
    assert ce.rows_of(found) == expected


def builder_view(found: Answer, policy: PolicyModel) -> dict[str, Any]:
    """The receipt as a builder sees it, valid against `receipt` v1."""
    context = rp.caller() | {"role": "builder"}
    view = found.receipt.view(CallerViewer(rp.COMPILED, policy, context))
    jsonschema.validate(view, RECEIPT)
    return view


def test_the_receipt_names_the_connection_snapshot_and_policy_reads() -> None:
    policy = rp.policies(ZONE)
    with lakes() as (managed, other):
        adapter = Simulated(other)
        found = ask_on(managed, adapter, policy, rp.caller(user_id="u-1"))
        version = adapter.pin()
        snapshot = managed.current_snapshot().id
    view = builder_view(found, policy)
    reads = {(r["for"], r["name"]): r for r in view["data"]}
    sales = reads[("dataset", "sales")]
    assert sales["route"] == "existing_ducklake"
    assert sales["snapshot"] | {"committed_at": None} == {
        "id": version["id"],
        "committed_at": None,
        "project": PROJECT,
        "connection": "warehouse",
    }
    # The policy's mapping values were read on the managed lake, at the question's snapshot;
    # the policy is named, never its mapping table.
    zone = reads[("policy", "own_zone")]
    assert (zone["route"], zone["snapshot"]["id"]) == ("managed", snapshot)
    assert "security.user_zone" not in json.dumps(view)
    assert view["execution"]["engine"] == "ducklake"
    assert view["execution"]["engine_version"] == "simulated-1"
    assert "estimate" not in view["execution"]
    assert view["reproducibility"]["class"] == "exact"


def test_a_live_engine_records_its_read_instant_and_is_explainable_only() -> None:
    policy = rp.policies()
    with lakes() as (managed, other):
        found = ask_on(managed, Simulated(other, entry=LIVE), policy, rp.caller())
    view = builder_view(found, policy)
    (read_,) = [r for r in view["data"] if r["for"] == "dataset"]
    assert read_ == {
        "for": "dataset",
        "name": "sales",
        "route": "live",
        "retained": False,
        "read_at": read_["read_at"],
        "connection": "stream",
    }
    assert view["reproducibility"]["class"] == "explainable"
    assert "live_read" in view["reproducibility"]["reasons"]


def test_no_secret_and_no_mapping_value_reaches_a_receipt() -> None:
    canary = "zz-mapping-canary-91f"  # the caller's key, bound to read its mapping values
    entry = EngineEntry(
        "warehouse",
        "ducklake",
        "existing_ducklake",
        f"ducklake://{SECRET}@warehouse.example/catalog",
        ENTRY.capabilities,
    )
    with lakes() as (managed, other):
        adapter = Simulated(other, entry=entry)
        policy = rp.policies(ZONE)
        found = ask_on(managed, adapter, policy, rp.caller(user_id=canary))
    text = json.dumps(found.receipt.document()) + json.dumps(builder_view(found, policy))
    text += json.dumps(found.result.envelope())
    assert SECRET not in text
    assert canary not in text
    assert all(canary not in statement for statement, _, _ in adapter.statements)
    assert all(SECRET not in statement for statement, _, _ in adapter.statements)


# --- caps ------------------------------------------------------------------------------------


@pytest.mark.parametrize("limits", [{"policy_values": 1}, {"policy_value_bytes": 1}])
def test_more_values_than_the_caps_refuse_without_revealing_sizes(limits: dict[str, int]) -> None:
    two = rp.MAPPINGS | {"security.user_zone": [("u-1", 10), ("u-1", 22)]}
    policy = rp.policies(ZONE, limits=limits)
    with lakes(mappings=two) as (managed, other):
        adapter = Simulated(other)
        with pytest.raises(SemanticError) as caught:
            ask_on(managed, adapter, policy, rp.caller(user_id="u-1"))
    assert caught.value.code == "POLICY_NOT_SUPPORTED_ON_ENGINE"
    assert dict(caught.value.details) == {
        "dataset": "sales",
        "policy": "own_zone",
        "reason": "value_cap",
    }
    assert adapter.statements == []  # the policy is never skipped: nothing is read there
    assert adapter.pins == 0  # nor is the other engine asked anything


def test_values_at_the_caps_are_answered() -> None:
    policy = rp.policies(ZONE, limits={"policy_values": 1, "policy_value_bytes": 1})
    with lakes() as (managed, other):
        expected = ce.rows_of(
            ask(rp.query(), rp.COMPILED, managed, policy=policy, context=rp.caller(user_id="u-1"))
        )
        found = ask_on(managed, Simulated(other), policy, rp.caller(user_id="u-1"))
    assert ce.rows_of(found) == expected == [["North", "1106.00"]]


def test_a_correlated_mapping_subquery_is_refused_before_any_read() -> None:
    correlated = rp.DENY | {
        "predicate": "NOT EXISTS (SELECT 1 FROM security.user_zone WHERE zone_id = sales.zone_id"
        " AND user_id = $user_id)"
    }
    spy = Spy()
    with lakes() as (managed, other):
        adapter = Simulated(other)
        with pytest.raises(SemanticError) as caught:
            ask_on(managed, adapter, rp.policies(correlated), rp.caller(user_id="u-1"), read=spy)
    assert caught.value.code == "POLICY_NOT_SUPPORTED_ON_ENGINE"
    assert caught.value.details["reason"] == "engine_unsupported"
    assert spy.calls == 0
    assert adapter.statements == []
    assert adapter.pins == 0


# --- engines, routes and capabilities ----------------------------------------------------------


def test_an_engine_that_is_not_enabled_is_refused_before_any_read() -> None:
    spy = Spy()
    with rp.lake_of() as managed, pytest.raises(SemanticError) as caught:
        ask_on(managed, None, rp.policies(), rp.caller(), read=spy)
    assert caught.value.code == "UNSUPPORTED_OPERATION"
    assert dict(caught.value.details) == {"operation": "route"}
    assert spy.calls == 0


def test_a_question_over_two_engines_is_refused_before_any_read() -> None:
    other_place = EngineEntry("lake2", "ducklake", "existing_ducklake", "ducklake://other/c")
    spy = Spy()
    document = rp.query(("customers.country",))
    for second in (None, other_place):  # the managed lake, or one dialect elsewhere
        with lakes() as (managed, other):
            adapter = Simulated(other)
            with pytest.raises(SemanticError) as caught:
                ask_on(
                    managed,
                    adapter,
                    rp.policies(),
                    rp.caller(),
                    document,
                    routing=routed(("sales", ENTRY), ("customers", second)),
                    read=spy,
                )
        assert caught.value.code == "CROSS_ENGINE_QUERY"
        assert caught.value.details["datasets"] == ["customers", "sales"]
        public = caught.value.to_message("en", "other").model_dump()["details"]
        assert public == {"datasets": ["customers", "sales"]}  # engines are builder-only
        assert spy.calls == 0
        assert adapter.statements == []


def test_a_capability_the_engine_lacks_is_named_before_any_read() -> None:
    lacking = EngineEntry(
        ENTRY.connection, ENTRY.kind, ENTRY.route, ENTRY.environment, frozenset({"exact_decimal"})
    )
    detail = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "detail",
        "fields": [{"member": "sales.region"}],
        "order": [{"by": "sales.sale_id"}],
        "page": {"size": "10"},
    }
    with lakes() as (managed, other):
        adapter = Simulated(other, entry=lacking)
        with pytest.raises(SemanticError) as caught:
            ask_on(managed, adapter, rp.policies(), rp.caller(), detail)
        plain = ask_on(managed, adapter, rp.policies(), rp.caller())  # it needs nothing more
    assert dict(caught.value.details) == {"operation": "detail_pages"}
    without_decimals = EngineEntry(
        ENTRY.connection, ENTRY.kind, ENTRY.route, ENTRY.environment, frozenset({"window"})
    )
    with lakes() as (managed, other), pytest.raises(SemanticError) as inexact:
        ask_on(managed, Simulated(other, entry=without_decimals), rp.policies(), rp.caller())
    assert dict(inexact.value.details) == {"operation": "exact_decimal"}
    assert ce.rows_of(plain) == [["North", "1106.00"], ["South", "50.00"]]


def test_preparing_for_another_engine_reads_nothing() -> None:
    spy = Spy()
    with lakes() as (managed, other):
        adapter = Simulated(other)
        progress = Progress()
        ready = prepare(
            json.dumps(rp.query()),
            rp.COMPILED,
            rp.policies(ZONE),
            rp.caller(user_id="u-1"),
            ce.engine_for(managed, read=spy, routing=routed(), adapters={"warehouse": adapter}),
            progress,
        )
    assert ready.external is not None
    assert progress.stage == "planned"
    assert spy.calls == 0
    assert adapter.statements == []
    assert adapter.pins == 0


def test_an_unknown_capability_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown capabilities"):
        EngineEntry("w", "ducklake", "live", "e", frozenset({"telepathy"}))
    with pytest.raises(ValueError, match="managed lake"):
        EngineEntry("w", "ducklake", "managed", "e")


# --- the model's connections ------------------------------------------------------------------


def thawed(node: object) -> object:
    if isinstance(node, Mapping):
        return {k: thawed(v) for k, v in node.items()}
    if isinstance(node, tuple | list):
        return [thawed(v) for v in node]
    return node


def model_on(connections: Mapping[str, str]) -> Any:  # noqa: ANN401
    document: Any = thawed(rp.COMPILED.ossie.document)
    for dataset in document["datasets"]:
        if dataset["name"] in connections:
            payload = {"element": "dataset", "connection": connections[dataset["name"]]}
            dataset["custom_extensions"] = [extension(payload)]
    loaded = read(json.dumps(document).encode(), "semantic/tenants.ossie.json", VENDORS)
    return compile_model(loaded, dict(rp.CATALOG), VENDORS)


def test_a_dataset_is_bound_to_its_connection_s_engine() -> None:
    catalog = EngineCatalog({"warehouse": ENTRY})
    routing = route_model(model_on({"sales": "warehouse", "customers": "warehouse"}), catalog)
    assert routing.of("sales") == ENTRY
    assert routing.route("customers") == "existing_ducklake"
    managed = route_model(model_on({}), catalog)
    assert not managed.external
    assert managed.route("sales") == "managed"


@pytest.mark.parametrize(
    "catalog",
    [EngineCatalog({}), EngineCatalog({}, frozenset({"warehouse"}))],
    ids=["unknown", "not direct query"],
)
def test_an_unusable_connection_is_a_problem_of_the_model(catalog: EngineCatalog) -> None:
    with pytest.raises(SemanticError) as caught:
        route_model(model_on({"sales": "warehouse"}), catalog, "semantic/tenants.ossie.json")
    assert caught.value.code == "OSSIE_INVALID"
    problems = caught.value.details["problems"]
    assert isinstance(problems, list)
    assert [p["rule"] for p in problems] == ["reference"]
    assert str(problems[0]["pointer"]).startswith("/datasets/0/custom_extensions")


def test_a_relationship_across_engines_is_refused_when_the_model_is_bound() -> None:
    same_dialect = EngineEntry("lake2", "ducklake", "existing_ducklake", "ducklake://other/c")
    catalog = EngineCatalog({"warehouse": ENTRY, "lake2": same_dialect})
    for customers in ({}, {"customers": "lake2"}):
        with pytest.raises(SemanticError) as caught:
            route_model(model_on({"sales": "warehouse"} | customers), catalog)
        assert caught.value.code == "CROSS_ENGINE_QUERY"
    twin = EngineEntry("twin", "ducklake", "existing_ducklake", ENTRY.environment)
    both = EngineCatalog({"warehouse": ENTRY, "twin": twin})  # one environment, two names
    assert route_model(model_on({"sales": "warehouse", "customers": "twin"}), both).external


def test_a_mapping_type_without_exact_text_is_refused_before_the_other_engine_reads() -> None:

    score = {
        "name": "own_score",
        "dataset": "sales",
        "kind": "predicate",
        "predicate": "amount IN (SELECT score FROM security.user_score WHERE user_id = $user_id)",
    }

    policy = read_policies(
        yaml.safe_dump({"version": 1, "row_policies": [score]}),
        rp.COMPILED,
        groups=frozenset(),
        managed_tables=rp.MANAGED | {"security.user_score": ("user_id", "score")},
    )
    with lakes() as (managed, other):
        with managed.transaction() as transaction:
            for sql in (
                'CREATE TABLE security.user_score ("user_id" VARCHAR, "score" DOUBLE)',
                "INSERT INTO security.user_score VALUES ('u-1', 100.0)",
            ):
                transaction.execute(StatementRequest(kind="statement", sql=sql, parameters=[]))
        adapter = Simulated(other)
        with pytest.raises(SemanticError) as caught:
            ask_on(managed, adapter, policy, rp.caller(user_id="u-1"))
    assert dict(caught.value.details) == {
        "dataset": "sales",
        "policy": "own_score",
        "reason": "engine_unsupported",
    }
    assert adapter.statements == []


def test_a_deny_list_at_the_count_cap_with_its_registration_is_answered() -> None:
    """A registration or an `EXISTS` is one bound flag: it never counts as a value."""
    policy = rp.policies(rp.DENY, limits={"policy_values": 1})
    context = rp.caller(user_id="u-1")
    with lakes() as (managed, other):
        expected = ce.rows_of(ask(rp.query(), rp.COMPILED, managed, policy=policy, context=context))
        found = ask_on(managed, Simulated(other), policy, context)
    assert ce.rows_of(found) == expected == [["South", "50.00"]]


def test_every_managed_table_a_policy_read_is_kept_and_named_once() -> None:
    policy = rp.policies(rp.DENY)
    with lakes() as (managed, other):
        found = ask_on(managed, Simulated(other), policy, rp.caller(user_id="u-1"))
    record = found.receipt.document()["data"]
    tables = sorted(r["table"] for r in record if r["for_policy"])
    assert tables == ["security.registered", "security.user_zone"]
    view = builder_view(found, policy)
    assert [r["name"] for r in view["data"] if r["for"] == "policy"] == ["not_blocked_zone"]


def test_each_dataset_names_its_own_connection_when_two_share_an_environment() -> None:
    twin = EngineEntry("twin", ENTRY.kind, ENTRY.route, ENTRY.environment, ENTRY.capabilities)
    policy = rp.policies()
    with lakes() as (managed, other):
        found = ask_on(
            managed,
            Simulated(other),
            policy,
            rp.caller(),
            rp.query(("customers.country",)),
            routing=routed(("sales", ENTRY), ("customers", twin)),
        )
    view = builder_view(found, policy)
    connections = {r["name"]: r["snapshot"]["connection"] for r in view["data"]}
    assert connections == {"sales": "warehouse", "customers": "twin"}


def test_managed_lake_evidence_never_verifies_another_engine_s_data() -> None:
    """The managed lake's records say nothing of another engine's tables: their evidence stays
    declared, even when a table of the same name in the managed lake was verified."""
    from orvex.semantic.evidence import EvidenceRecord

    calls: list[object] = []

    def records(snapshot: Mapping[str, object]) -> list[EvidenceRecord]:
        calls.append(snapshot)
        return []

    with lakes() as (managed, other):
        found = ask_on(managed, Simulated(other), rp.policies(), rp.caller(), evidence=records)
    assert calls == []
    view = builder_view(found, rp.policies())
    assert all(e["status"] != "verified" for e in view["evidence"])
