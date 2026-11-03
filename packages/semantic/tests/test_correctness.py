"""P2-11 (#310): the correctness engine framework and C1, the grain algebra."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from orvex.semantic import SemanticError
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.correctness import IMPLEMENTED, RULES, SHAPES, CorrectnessReport, analyze
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.query.binding import BoundQuery, Limits, bind_query

from tests.support.corpus import QUERIES, corpus_model
from tests.support.policies import CATALOG, GROUPS, MANAGED_TABLES, MODEL, VENDORS, extension

sys.path.insert(0, str(Path(__file__).parent))

ROOT = Path(__file__).resolve().parents[3]
INSTANT = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
Document = dict[str, Any]


def context(groups: Iterable[str] = (), role: str = "viewer") -> Document:
    return {
        "kind": "security_context",
        "version": 1,
        "user": "u-1",
        "project": "p1",
        "role": role,
        "groups": sorted(set(groups)),
        "attributes": {},
        "permission_fingerprint": "sha256:" + "0" * 64,
    }


def metric(name: str, sql: str, **payload: Any) -> Document:  # noqa: ANN401
    node: Document = {"name": name, "expression": {"sql": sql}}
    if payload:
        node["custom_extensions"] = [extension({"element": "metric", **payload})]
    return node


def bank_model() -> Document:
    """The P2-08 bank model, plus metrics of each additivity class."""
    document = copy.deepcopy(MODEL)
    document["metrics"] += [
        metric("declared", "SUM(payments.amount)", additivity="additive"),
        metric("biggest", "MAX(payments.amount)", additivity="non_additive"),
        metric("smallest", "MIN(payments.amount)"),
        metric("doubled", "SUM(payments.amount) * 2"),
        metric("doubled_additive", "SUM(payments.amount) * 2", additivity="additive"),
        metric("payments_count", "COUNT(payments.payment_id)"),
    ]
    return document


def compiled_from(document: Document) -> CompiledModel:
    loaded = read(json.dumps(document).encode(), "semantic/bank.ossie.json", VENDORS)
    return compile_model(loaded, CATALOG, VENDORS)


BANK = compiled_from(bank_model())


def policies(compiled: CompiledModel = BANK, **document: Any) -> PolicyModel:  # noqa: ANN401
    return read_policies(
        yaml.safe_dump({"version": 1, **document}),
        compiled,
        groups=GROUPS,
        managed_tables=MANAGED_TABLES,
    )


def query(*metrics: str, group_by: Iterable[Any] = (), **more: Any) -> Document:  # noqa: ANN401
    found: Document = {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": m} for m in metrics],
    }
    rows = [g if isinstance(g, dict) else {"member": g} for g in group_by]
    if rows:
        found["group_by"] = rows
    found.update(more)
    return found


def bound(
    document: Document,
    compiled: CompiledModel = BANK,
    hidden: Callable[[MemberId], bool] = lambda member: False,
) -> BoundQuery:
    return bind_query(
        compiled,
        json.dumps(document),
        instant=INSTANT,
        limits=Limits(detail_page_size=1000),
        hidden=hidden,
    )


def run(
    document: Document,
    compiled: CompiledModel = BANK,
    policy: PolicyModel | None = None,
    groups: Iterable[str] = (),
    role: str = "viewer",
) -> CorrectnessReport:
    policy = policy if policy is not None else policies(compiled)
    groups = sorted(set(groups))

    def hidden(member: MemberId) -> bool:
        return policy.protection(member, groups) == "hidden"

    return analyze(bound(document, compiled, hidden), compiled, policy, context(groups, role))


def refusal(*arguments: Any, **options: Any) -> tuple[str, dict[str, object]]:  # noqa: ANN401
    with pytest.raises(SemanticError) as caught:
        run(*arguments, **options)
    return caught.value.code, dict(caught.value.details)


# --- the framework ------------------------------------------------------------------------------


def test_the_report_is_deterministic_and_order_does_not_change_its_facts() -> None:
    zone = {"member": "zones.label", "role": "billing"}
    day = {"member": "payments.paid_on"}
    one = run(query("revenue", "declared", group_by=[zone, day]))
    two = run(query("declared", "revenue", group_by=[day, zone]))
    assert one.fingerprint == run(query("revenue", "declared", group_by=[zone, day])).fingerprint
    assert one.facts == two.facts
    assert one.obligations == two.obligations
    assert one.rules == RULES == (("C1", 1),)
    assert {"C1"} == IMPLEMENTED


SCRIPT = """
import json, sys
sys.path[:0] = [{tests!r}, {root!r}]
import test_correctness as t
print(t.run(t.query("revenue", group_by=["payments.paid_on"])).fingerprint)
"""


def test_the_fingerprint_is_equal_across_processes_and_hash_seeds() -> None:
    here = run(query("revenue", group_by=["payments.paid_on"])).fingerprint
    for seed in ("0", "1", "4242"):
        result = subprocess.run(
            [sys.executable, "-c", SCRIPT.format(tests=str(Path(__file__).parent), root=str(ROOT))],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=ROOT,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        assert result.stdout.strip() == here


def test_the_report_is_immutable() -> None:
    report = run(query("revenue"))
    with pytest.raises(TypeError):
        report.facts["mode"] = "detail"  # type: ignore[index]
    with pytest.raises(AttributeError):
        report.fingerprint = "x"  # type: ignore[misc]


# --- the corpus ---------------------------------------------------------------------------------

TENANT = {
    "name": "own_tenant",
    "dataset": "sales",
    "kind": "predicate",
    "predicate": "tenant_id = $tenant_id",
}

# Each case's rules, and what the engine answers today: None when C1 alone answers it, else the
# refusal. A later issue that implements a rule updates its rows (#310, "Fail closed").
CASES: dict[str, tuple[frozenset[str], tuple[str, str] | None]] = {
    "CASE-001": (frozenset({"C1"}), None),
    "CASE-002": (frozenset({"C1"}), None),
    "CASE-003": (frozenset({"C1"}), None),
    "CASE-004": (frozenset({"C1", "C3", "C5"}), ("UNSUPPORTED_OPERATION", "multi_fact")),
    "CASE-005": (frozenset({"C1"}), None),
    "CASE-006": (frozenset({"C1", "C2"}), ("COMPILER", "recomputable")),
    "CASE-007": (frozenset({"C1", "C2", "C6"}), ("UNSUPPORTED_OPERATION", "semi_additive")),
    "CASE-008": (frozenset({"C1", "C2"}), ("UNSUPPORTED_OPERATION", "holistic")),
    "CASE-009": (frozenset({"C1", "C5"}), ("COMPILER", "multi_fact")),
    "CASE-010": (frozenset({"C1", "C2", "C5"}), ("UNSUPPORTED_OPERATION", "recomputable")),
    "CASE-011": (frozenset({"C1", "C7"}), ("UNSUPPORTED_OPERATION", "bridge_allocation")),
    "CASE-012": (frozenset({"C1", "C6"}), ("UNSUPPORTED_OPERATION", "zoned_bucketing")),
    "CASE-013": (frozenset({"C1", "C6"}), ("UNSUPPORTED_OPERATION", "as_of_join")),
    "CASE-014": (frozenset({"C1"}), None),
    "CASE-015": (frozenset({"C1", "policy placement"}), ("UNSUPPORTED_OPERATION", "row_policy")),
    "CASE-016": (frozenset({"C1", "C4"}), ("AMBIGUOUS_RELATIONSHIP_PATH", "")),
    "CASE-017": (frozenset({"C1"}), None),
    "CASE-018": (frozenset({"C1"}), None),
    "CASE-019": (frozenset({"C1", "C6"}), ("UNSUPPORTED_OPERATION", "zoned_bucketing")),
    "CASE-020": (frozenset({"C1", "C2", "C6"}), ("UNSUPPORTED_OPERATION", "semi_additive")),
    "CASE-021": (frozenset({"C1", "C6"}), ("COMPILER", "ordered")),
}


def test_the_table_covers_every_corpus_case() -> None:
    assert sorted(CASES) == sorted(QUERIES)
    for case, (rules, outcome) in CASES.items():
        assert (outcome is None) == (rules <= IMPLEMENTED), case
        if outcome is not None and outcome[0] in ("UNSUPPORTED_OPERATION", "COMPILER"):
            assert outcome[1] == "row_policy" or SHAPES[outcome[1]].rule in rules, case


@pytest.mark.parametrize("case", sorted(CASES))
def test_each_corpus_case_is_answered_or_refused_as_the_table_says(case: str) -> None:
    outcome = CASES[case][1]
    if outcome is not None and outcome[0] == "COMPILER":
        with pytest.raises(SemanticError):  # the compiler refuses it until its rule lands (#283)
            corpus_model(case)
        return
    compiled = corpus_model(case)
    rows = [TENANT] if case == "CASE-015" else []
    policy = read_policies(
        yaml.safe_dump({"version": 1, "row_policies": rows}),
        compiled,
        groups=frozenset(),
        managed_tables={},
    )
    document = QUERIES[case]
    if outcome is None:
        report = analyze(bound(document, compiled), compiled, policy, context())
        assert [o.rule for o in report.obligations] == ["C1"] * len(report.obligations)
        return
    with pytest.raises(SemanticError) as caught:
        analyze(bound(document, compiled), compiled, policy, context())
    assert caught.value.code == outcome[0]
    if outcome[0] == "UNSUPPORTED_OPERATION":
        assert caught.value.details["operation"] == outcome[1]


def test_case_001_gives_one_fact_set_and_one_obligation() -> None:
    compiled = corpus_model("CASE-001")
    report = analyze(
        bound(QUERIES["CASE-001"], compiled),
        compiled,
        read_policies("version: 1\n", compiled, groups=frozenset(), managed_tables={}),
        context(),
    )
    (obligation,) = report.obligations
    assert obligation.id == "C1.aggregate:revenue"
    assert (obligation.from_grain, obligation.to_grain) == (("sales.sale_id",), ("sales.region",))
    assert report.facts["metrics"][0]["home"] == "sales"
    assert report.facts["datasets"] == ("sales",)


# --- additivity ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["revenue", "declared", "payments_count", "doubled_additive"])
def test_an_additive_metric_to_a_coarser_grain_is_accepted(name: str) -> None:
    report = run(query(name, group_by=[{"member": "zones.label", "role": "billing"}]))
    assert [o.id for o in report.obligations] == [f"C1.aggregate:{name}"]


@pytest.mark.parametrize(
    ("name", "operation"),
    [
        ("identified", "holistic"),
        ("tails", "holistic"),
        ("biggest", "non_additive"),
        ("running", "ordered"),
    ],
)
def test_a_metric_that_is_not_additive_is_refused_with_its_pending_rule(
    name: str, operation: str
) -> None:
    # Its grain transition alone would be legal: payments' own grain, coarser.
    code, details = refusal(query(name))
    assert (code, details) == ("UNSUPPORTED_OPERATION", {"operation": operation})


@pytest.mark.parametrize("name", ["smallest", "doubled"])
def test_a_metric_with_no_declared_or_derivable_additivity_is_refused(name: str) -> None:
    assert refusal(query(name)) == ("MISSING_ADDITIVITY_DECLARATION", {"metric": name})


def test_a_metric_reading_two_datasets_is_multi_fact() -> None:
    assert refusal(query("flagged")) == ("UNSUPPORTED_OPERATION", {"operation": "multi_fact"})
    assert refusal(query("revenue", "phones")) == (
        "UNSUPPORTED_OPERATION",
        {"operation": "multi_fact"},
    )


# --- C1, the grain ------------------------------------------------------------------------------


def test_the_same_grain_is_legal() -> None:
    (obligation,) = run(query("revenue", group_by=["payments.payment_id"])).obligations
    assert obligation.from_grain == obligation.to_grain == ("payments.payment_id",)


def test_a_coarser_grain_through_many_to_one_and_one_to_one_is_legal() -> None:
    report = run(
        query(
            "revenue",
            group_by=[
                {"member": "zones.label", "role": "billing"},
                {"member": "profiles.score", "role": "billing"},
            ],
        )
    )
    paths = {p["member"]: p["relationships"] for p in report.facts["paths"]}
    assert paths == {
        "zones.label": ("billing", "customer_zone"),
        "profiles.score": ("billing", "profile"),
    }


def test_a_coarser_bucket_of_a_date_is_legal() -> None:
    (obligation,) = run(
        query("revenue", group_by=[{"member": "payments.paid_on", "bucket": {"unit": "month"}}])
    ).obligations
    assert obligation.to_grain == ("payments.paid_on@month",)


def test_a_member_of_an_unrelated_dataset_is_an_illegal_transition() -> None:
    assert refusal(query("revenue", group_by=["employees.manager_id"])) == (
        "ILLEGAL_GRAIN_TRANSITION",
        {"metric": "revenue", "grain": ["employees.manager_id"]},
    )


def test_a_finer_member_of_a_many_side_dataset_is_fanout() -> None:
    assert refusal(query("phones", group_by=["payments.paid_on"])) == (
        "UNSUPPORTED_OPERATION",
        {"operation": "fanout"},
    )


def test_a_path_through_a_bridge_is_bridge_allocation() -> None:
    assert refusal(query("revenue", group_by=["campaigns.title"])) == (
        "UNSUPPORTED_OPERATION",
        {"operation": "bridge_allocation"},
    )


def test_two_role_paths_without_a_role_are_ambiguous() -> None:
    code, details = refusal(query("revenue", group_by=["customers.segment"]))
    assert code == "AMBIGUOUS_RELATIONSHIP_PATH"
    assert details == {"from": "payments", "to": "customers", "roles": ["billing", "shipping"]}
    for role in ("billing", "shipping"):
        report = run(query("revenue", group_by=[{"member": "customers.segment", "role": role}]))
        assert report.facts["paths"][0]["relationships"] == (role,)


def test_sorting_by_a_member_that_is_not_grouped_is_an_illegal_transition() -> None:
    document = query("revenue", order=[{"by": "payments.paid_on"}])
    assert refusal(document)[0] == "ILLEGAL_GRAIN_TRANSITION"
    grouped = query("revenue", group_by=["payments.paid_on"], order=[{"by": "payments.paid_on"}])
    assert run(grouped).obligations


def test_a_sort_follows_the_path_of_its_group() -> None:
    document = query(
        "revenue",
        group_by=[{"member": "customers.segment", "role": "shipping"}],
        order=[{"by": "customers.segment"}],
    )
    report = run(document)
    assert {p["slot"]: p["relationships"] for p in report.facts["paths"]} == {
        "group_by": ("shipping",),
        "sort": ("shipping",),
    }


def test_a_shape_no_table_row_names_is_refused() -> None:
    from orvex.semantic.correctness.analysis import _refuse_pending

    with pytest.raises(SemanticError) as caught:
        _refuse_pending({"aggregate", "a_shape_from_the_future"})
    assert dict(caught.value.details) == {"operation": "metric_extension"}


def test_a_filter_follows_the_same_paths() -> None:
    def where(member: str, role: str | None = None) -> Document:
        entry: Document = {"member": member, "scope": "board", "predicate": {"op": "is_not_null"}}
        if role:
            entry["role"] = role
        return query("revenue", filters=[entry])

    assert run(where("zones.label", "billing")).facts["paths"][0]["slot"] == "filter"
    assert refusal(where("employees.manager_id"))[0] == "ILLEGAL_GRAIN_TRANSITION"
    assert refusal(where("customers.segment"))[0] == "AMBIGUOUS_RELATIONSHIP_PATH"


@pytest.mark.parametrize(
    ("change", "operation"),
    [
        (
            {
                "mode": "detail",
                "fields": [{"member": "payments.amount"}],
                "page": {"size": "10"},
                "order": [{"by": "payments.payment_id"}],
            },
            "detail_rows",
        ),
        ({"top_n": {"by": "revenue", "n": "3", "others": True}}, "top_n"),
        ({"coverage": "aligned"}, "coverage"),
        (
            {
                "context_modifiers": [
                    {"op": "remove", "scope": "page", "members": ["payments.paid_on"]}
                ]
            },
            "context_modifier",
        ),
        (
            {
                "contexts": [
                    {
                        "name": "c",
                        "filters": [
                            {
                                "scope": "visual",
                                "member": "payments.paid_on",
                                "predicate": {"op": "is_not_null"},
                            }
                        ],
                    }
                ]
            },
            "filter_context",
        ),
        (
            {
                "metric_filters": [
                    {
                        "scope": "visual",
                        "metric": "revenue",
                        "dimension": "payments.payment_id",
                        "condition": {"op": "gt", "value": {"type": "decimal", "value": "1"}},
                    }
                ]
            },
            "metric_filter",
        ),
    ],
)
def test_shapes_without_a_rule_are_refused(change: Document, operation: str) -> None:
    document = query("revenue", group_by=["payments.paid_on"]) | change
    if change.get("mode") == "detail":
        document.pop("metrics")
        document.pop("group_by")
    assert refusal(document) == ("UNSUPPORTED_OPERATION", {"operation": operation})


# --- policies, before anything ------------------------------------------------------------------

HIDDEN = {
    "member": "customers.national_id",
    "default": "hidden",
    "visible_to_groups": ["collections"],
}
MASKED = {"member": "customers.phone", "default": "masked", "mask": "***"}


def without_national_id() -> CompiledModel:
    """The bank model where `customers.national_id` does not exist."""
    document = bank_model()
    customers = next(d for d in document["datasets"] if d["name"] == "customers")
    customers["fields"] = [f for f in customers["fields"] if f["name"] != "national_id"]
    document["metrics"] = [m for m in document["metrics"] if "national_id" not in json.dumps(m)]
    return compiled_from(document)


@pytest.mark.parametrize("spelling", ["customers.national_id", "CUSTOMERS.NATIONAL_ID"])
def test_a_hidden_member_is_refused_like_one_that_does_not_exist(spelling: str) -> None:
    policy = policies(column_policies=[HIDDEN])
    document = query("revenue", group_by=[{"member": spelling, "role": "billing"}])

    def hidden(member: MemberId) -> bool:
        return policy.protection(member, ()) == "hidden"

    with pytest.raises(SemanticError) as seen:
        bound(document, BANK, hidden)
    with pytest.raises(SemanticError) as missing:
        bound(document, without_national_id())
    assert seen.value.code == missing.value.code == "MEMBER_NOT_FOUND"
    assert seen.value.details == {"member": spelling}
    for audience in ("builder", "other"):
        assert seen.value.to_json(audience=audience) == missing.value.to_json(audience=audience)
    assert run(document, policy=policy, groups=["collections"]).obligations


def test_a_query_bound_for_another_caller_never_gets_an_answer() -> None:
    from orvex.semantic.correctness.analysis import UnfilteredQuery

    policy = policies(column_policies=[HIDDEN])
    document = query("revenue", group_by=[{"member": "CUSTOMERS.NATIONAL_ID", "role": "billing"}])
    with pytest.raises(UnfilteredQuery) as caught:  # bound with nothing hidden, for a viewer
        analyze(bound(document), BANK, policy, context())
    assert not isinstance(caught.value, SemanticError)
    assert "national" not in str(caught.value).lower()


@pytest.mark.parametrize(
    ("change", "operation"),
    [
        ({"group_by": [{"member": "customers.phone", "role": "billing"}]}, "group_by"),
        (
            {
                "filters": [
                    {
                        "member": "customers.phone",
                        "role": "billing",
                        "scope": "board",
                        "predicate": {"op": "is_null"},
                    }
                ]
            },
            "filter",
        ),
        ({"order": [{"by": "customers.phone"}]}, "sort"),
    ],
)
def test_a_masked_member_is_refused_wherever_it_is_used(change: Document, operation: str) -> None:
    document = query("revenue") | change
    assert refusal(document, policy=policies(column_policies=[MASKED])) == (
        "MEMBER_MASKED",
        {"member": "customers.phone", "operation": operation},
    )


def test_a_distinct_count_over_a_masked_member_is_not_found() -> None:
    # P2-08: a metric reading a masked member is hidden, which is stricter than MEMBER_MASKED.
    assert refusal(query("tails"), policy=policies(column_policies=[MASKED])) == (
        "MEMBER_NOT_FOUND",
        {"member": "tails"},
    )


OWN_ZONE = {
    "name": "own_zone_only",
    "dataset": "customers",
    "kind": "predicate",
    "predicate": "zone_id IN (SELECT zone_id FROM security.user_zone WHERE user_id = $user_id)",
}


def test_a_dataset_a_row_policy_reaches_is_refused_until_its_placement() -> None:
    policy = policies(row_policies=[OWN_ZONE])
    for document in (
        query("phones"),  # the policy's own dataset
        query("revenue"),  # reached through `billing` and `shipping`
        query("revenue", group_by=[{"member": "zones.label", "role": "billing"}]),
    ):
        assert refusal(document, policy=policy) == (
            "UNSUPPORTED_OPERATION",
            {"operation": "row_policy"},
        )


def test_an_unusable_dataset_is_refused() -> None:
    threshold = [{"dataset": "payments", "entity": "payments.customer_id", "min_group_size": 5}]
    assert refusal(query("revenue"), policy=policies(aggregate_thresholds=threshold)) == (
        "UNSUPPORTED_OPERATION",
        {"operation": "min_group_size"},
    )


def test_an_administrator_gets_the_report_of_a_viewer_with_the_same_groups() -> None:
    policy = policies(column_policies=[HIDDEN, MASKED])
    document = query("revenue", group_by=[{"member": "customers.national_id", "role": "billing"}])
    for role in ("viewer", "author", "builder"):
        with pytest.raises(SemanticError) as caught:
            run(document, policy=policy, role=role)
        assert caught.value.code == "MEMBER_NOT_FOUND"
    reports = {
        run(document, policy=policy, groups=["collections"], role=role).fingerprint
        for role in ("viewer", "author", "builder")
    }
    assert len(reports) == 1


def test_a_malformed_context_is_refused() -> None:
    from orvex.semantic.policies import InvalidContext

    for broken in (context() | {"role": "root"}, context() | {"groups": ["b", "a"]}, {}):
        with pytest.raises(InvalidContext):
            analyze(bound(query("revenue")), BANK, policies(), broken)


# --- properties ---------------------------------------------------------------------------------

M2O_FROM_PAYMENTS = {"PAYMENTS", "CUSTOMERS", "ZONES", "PROFILES"}
FIELDS = sorted(
    f"{m.dataset.lower()}.{m.name.lower()}"
    for m in BANK.members
    if m.kind == "field" and m.dataset is not None and m.name.lower() != "phone_tail"
)


def outcome(members: list[str]) -> CorrectnessReport | SemanticError:
    rows = [
        {"member": m, "role": "billing"} if not m.startswith("payments.") else {"member": m}
        for m in members
    ]
    try:
        return run(query("revenue", group_by=rows))
    except SemanticError as error:
        return error


@settings(max_examples=60, deadline=None)
@given(st.lists(st.sampled_from(FIELDS), min_size=1, max_size=4, unique=True))
def test_no_legal_transition_crosses_a_one_to_many_step(members: list[str]) -> None:
    found = outcome(members)
    if isinstance(found, CorrectnessReport):
        assert {m.split(".")[0].upper() for m in members} <= M2O_FROM_PAYMENTS


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.sampled_from(FIELDS), min_size=1, max_size=3, unique=True),
    st.sampled_from(["zones.label", "customers.segment", "payments.paid_on"]),
)
def test_adding_a_coarser_dimension_never_makes_an_illegal_query_legal(
    members: list[str], coarser: str
) -> None:
    if isinstance(outcome(members), SemanticError):
        assert isinstance(outcome(sorted({*members, coarser})), SemanticError)


@pytest.mark.parametrize(
    "spelling",
    ["payments.paid_on", "PAYMENTS.PAID_ON", '"PAYMENTS"."PAID_ON"', 'Payments."PAID_ON"'],
)
def test_spellings_of_one_member_give_one_fact(spelling: str) -> None:
    reference = run(query("revenue", group_by=["payments.paid_on"]))
    assert run(query("REVENUE", group_by=[spelling])).facts == reference.facts


# --- obligations in receipts, filtered by permission (#310 §3) -----------------------------------


def report_with_a_protected_member() -> CorrectnessReport:
    policy = policies(column_policies=[HIDDEN])
    document = query("revenue", group_by=[{"member": "customers.national_id", "role": "billing"}])
    return run(document, policy=policy, groups=["collections"])


def test_an_obligation_reaches_only_viewers_who_see_everything_it_names() -> None:
    from tests.support.receipts import EVERYTHING, SampleViewer, receipt

    (obligation,) = report_with_a_protected_member().obligations
    assert "customers.national_id" in obligation.members
    record = receipt(obligations=[obligation.for_receipt()])
    allowed = SampleViewer(
        builder=True,
        members=EVERYTHING.members | {"payments.payment_id"},
        datasets=frozenset({"payments", "customers"}),
    )
    assert record.view(allowed)["plan"]["obligations"] == [
        {"rule": "C1", "obligation": "C1.aggregate:revenue"}
    ]
    for blind in (
        SampleViewer(
            builder=True,
            members=allowed.members - {"customers.national_id"},
            datasets=allowed.datasets,
        ),
        SampleViewer(builder=True, members=allowed.members, datasets=frozenset({"payments"})),
    ):
        view = record.view(blind)
        assert view["plan"]["obligations"] == []
        assert "national_id" not in json.dumps(view["plan"])


def test_an_obligation_field_reaches_no_view_until_the_allow_list_names_it() -> None:
    from orvex.semantic.receipt import Obligation as Recorded

    from tests.support.receipts import EVERYTHING, receipt

    recorded = Recorded(
        "C1", "C1.aggregate:revenue", evidence="run-7", members=("revenue",), datasets=("payments",)
    )
    record = receipt(obligations=[recorded])
    (stored,) = record.document()["plan"]["obligations"]
    assert {"members", "datasets"} <= set(stored)
    (shown,) = record.view(EVERYTHING)["plan"]["obligations"]
    assert set(shown) == {"rule", "obligation", "evidence"}
    undeclared = receipt(obligations=[Recorded("C1", "names nothing it declares")])
    assert undeclared.view(EVERYTHING)["plan"]["obligations"] == []
