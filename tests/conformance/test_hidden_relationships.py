"""Relationships whose key the caller may not see (#442 AC2, AC3; #449 Q3): a hidden or masked
key makes its relationship absent for that caller, in the catalog and in planning alike, while
row policies keep propagating through it.

On the `utility` fixture, whose `readings.meter_id` is hidden except for `engineering`, and on
copies of its `security.yaml`: one that also masks `readings.consumer_id` (except for
`collections`), one with a row policy on `meters` that propagates to `readings`.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

import pytest
import yaml
from orvex.semantic.catalog.building import catalog_for_context
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import compile_model
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import answer
from orvex.semantic.explanation import explain
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.receipt.viewer import CallerViewer

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from tests.conformance.test_execution import ask, tiny_lake

FOUND = sf.fixture("utility", ce.VENDORS[0])
UTILITY = yaml.safe_load((FOUND.folder / "security.yaml").read_text("utf-8"))
MASKED_KEY = {"member": "readings.consumer_id", "default": "masked", "mask": "***",
              "visible_to_groups": ["collections"]}  # fmt: skip
ALPHA = {"name": "alpha_meters", "dataset": "meters", "kind": "predicate",
         "predicate": "model = 'Alpha'"}  # fmt: skip
# Each relationship, and a question that needs it: grouped by a field across it.
NEEDS = {"consumer": "consumers.zone_id", "meter": "meters.model"}


def policies(document: dict[str, Any], compiled: Any = FOUND.compiled) -> PolicyModel:  # noqa: ANN401
    return read_policies(yaml.safe_dump(document), compiled, groups=sf.GROUPS,
                         managed_tables=FOUND.managed)  # fmt: skip


def caller(groups: tuple[str, ...] = (), user: str = "u-zone1") -> dict[str, Any]:
    return {
        "kind": "security_context", "version": 1, "user": user, "project": "utility",
        "role": "author", "groups": sorted(groups), "attributes": {},
        "permission_fingerprint": "sha256:" + "0" * 64,
    }  # fmt: skip


def grouped(member: str, metric: str = "reading_count") -> dict[str, Any]:
    return {"kind": "semantic_query", "version": 1, "mode": "aggregate",
            "metrics": [{"metric": metric}], "group_by": [{"member": member}],
            "order": [{"by": member, "direction": "asc"}]}  # fmt: skip


def outcome(
    document: dict[str, Any],
    model: PolicyModel,
    context: dict[str, Any],
    compiled: Any = None,  # noqa: ANN401
    lake: Any = None,  # noqa: ANN401
) -> tuple[str | None, Any]:
    """The error code and details, or `None` and the rows."""
    if lake is None:
        with sf.managed_lake(FOUND) as opened:
            return outcome(document, model, context, compiled, opened)
    try:
        found = answer(json.dumps(document), compiled or FOUND.compiled, model, context,
                       ce.engine_for(lake))  # fmt: skip
    except SemanticError as error:
        return error.code, dict(error.details)
    return None, ce.rows_of(found)


def without(relationship: str) -> Any:  # noqa: ANN401
    """The fixture's model, compiled without `relationship`."""
    text = (
        (FOUND.folder / "model.ossie.json").read_text("utf-8").replace("{{vendor}}", ce.VENDORS[0])
    )
    model = json.loads(text)
    model["relationships"] = [r for r in model["relationships"] if r["name"] != relationship]
    return compile_model(read(json.dumps(model).encode(), "model.ossie.json", ce.VENDORS),
                         FOUND.catalog, ce.VENDORS)  # fmt: skip


def test_a_hidden_key_answers_as_a_model_that_never_had_the_relationship() -> None:
    document = grouped("meters.model", "total_kwh")
    hidden = outcome(document, policies(UTILITY), caller())
    other = without("meter")
    absent = outcome(document, policies(UTILITY, other), caller(), other)
    assert hidden == absent  # same code, same details
    assert hidden[0] == "ILLEGAL_GRAIN_TRANSITION"
    text = json.dumps(hidden)
    assert not {"Alpha", "Beta", "meter_id", "readings.meter_id"} & set(
        text.replace('"', " ").split()
    )


def test_engineering_gets_the_models_and_other_questions_answer_as_before() -> None:
    code, rows = outcome(grouped("meters.model", "total_kwh"), policies(UTILITY),
                         caller(("engineering", "management")))  # fmt: skip
    assert code is None
    assert rows == [["Alpha", "40.50"], ["Beta", "61.00"]]
    hidden = outcome(grouped("consumers.zone_id"), policies(UTILITY), caller())
    seen = outcome(grouped("consumers.zone_id"), policies(UTILITY), caller(("engineering",)))
    assert hidden == seen
    assert hidden[0] is None


def test_a_row_policy_still_propagates_through_a_relationship_the_caller_cannot_see() -> None:
    model = policies(UTILITY | {"row_policies": [*UTILITY["row_policies"], ALPHA]})
    meters = {m for m, kind in sf_rows("meters") if kind == "Alpha"}
    readings = sf_rows("readings")
    total = sum((float(r[4]) for r in readings if r[2] in meters and r[4] is not None), 0.0)
    code, rows = outcome({"kind": "semantic_query", "version": 1, "mode": "aggregate",
                          "metrics": [{"metric": "total_kwh"}, {"metric": "reading_count"}]},
                         model, caller(("management",)))  # fmt: skip
    assert code is None
    assert rows == [[f"{total:.2f}", str(sum(r[2] in meters for r in readings))]]
    code, rows = outcome({"kind": "semantic_query", "version": 1, "mode": "detail",
                          "fields": [{"member": "readings.reading_id"}],
                          "order": [{"by": "readings.reading_id", "direction": "asc"}],
                          "page": {"size": "100"}}, model, caller(("management",)))  # fmt: skip
    assert code is None
    assert rows == [[r[0]] for r in readings if r[2] in meters]


def sf_rows(name: str) -> list[list[str | None]]:
    (table,) = [t for t in FOUND.rows if t.split(".")[-1] == name]
    return FOUND.rows[table]


CALLERS = [
    g for n in range(len(sf.GROUPS) + 1) for g in itertools.combinations(sorted(sf.GROUPS), n)
]
COPIES = {
    "fixture": UTILITY,
    "masked key": UTILITY | {"column_policies": [*UTILITY["column_policies"], MASKED_KEY]},
}


@pytest.mark.parametrize("copy", sorted(COPIES))
def test_the_catalog_and_the_planner_agree_on_every_relationship_for_every_caller(
    copy: str,
) -> None:
    model = policies(COPIES[copy])
    decided: set[tuple[str, bool]] = set()
    with sf.managed_lake(FOUND) as lake:
        decided = _decided(model, copy, lake)
    assert {(n, s) for n in NEEDS for s in (True, False)} - decided <= (
        {("consumer", False)} if copy == "fixture" else set()
    )  # every relationship is seen and not seen, but the fixture's consumer key is never hidden


def _decided(model: PolicyModel, copy: str, lake: Any) -> set[tuple[str, bool]]:  # noqa: ANN401
    decided: set[tuple[str, bool]] = set()
    for groups in CALLERS:
        context = caller(groups)
        listed = {r["name"] for r in catalog_for_context(FOUND.compiled, model, context)
                  ["relationships"]}  # fmt: skip
        for name, member in NEEDS.items():
            code, _ = outcome(grouped(member), model, context, lake=lake)
            assert code in {None, "ILLEGAL_GRAIN_TRANSITION"}, (groups, name, code)
            assert (name in listed) == (code is None), (copy, groups, name)
            decided.add((name, code is None))
    return decided


ROLE_QUESTIONS = {
    "aggregate": {"kind": "semantic_query", "version": 1, "mode": "aggregate",
                  "metrics": [{"metric": "total_kwh"}],
                  "group_by": [{"member": "meters.model", "role": "meter"}]},
    "detail": {"kind": "semantic_query", "version": 1, "mode": "detail",
               "fields": [{"member": "readings.reading_id"},
                          {"member": "meters.model", "role": "meter"}],
               "order": [{"by": "readings.reading_id", "direction": "asc"}],
               "page": {"size": "10"}},
}  # fmt: skip


@pytest.mark.parametrize("how", ["question", "explain"])
@pytest.mark.parametrize("shape", sorted(ROLE_QUESTIONS))
def test_naming_an_unseen_relationship_as_a_role_is_as_naming_one_never_declared(
    shape: str, how: str
) -> None:
    """A role is a relationship's name: one the caller cannot use is refused as an unknown
    name, before any later check could tell it exists (Reviewer finding, #442)."""
    other = without("meter")

    def refused(model: PolicyModel, compiled: Any) -> tuple[str, dict[str, Any]]:  # noqa: ANN401
        context = caller()
        document = json.dumps(ROLE_QUESTIONS[shape])
        viewer = CallerViewer(compiled, model, context)
        with sf.managed_lake(FOUND) as lake:
            engine = ce.engine_for(lake)
            try:
                if how == "explain":
                    explain(document, compiled, model, context, engine, viewer)
                else:
                    answer(document, compiled, model, context, engine)
            except SemanticError as error:
                return error.code, dict(error.details)
        raise AssertionError("not refused")

    hidden = refused(policies(UTILITY), FOUND.compiled)
    assert hidden == refused(policies(UTILITY, other), other)
    assert hidden == ("MEMBER_NOT_FOUND", {"member": "meter"})
    seen = outcome(ROLE_QUESTIONS[shape], policies(UTILITY), caller(("engineering", "management")))
    assert seen[0] is None  # the role works for a caller who may use it


# --- a semi-additive entity across a relationship the caller cannot use ------------------------


def snapshots(*, relationship: bool) -> tuple[Any, dict[str, Any]]:
    """Daily balances `t` whose entity (`account`) is known only through the relationship
    `account` to `accounts`: `t` declares no `entity_keys` of its own."""
    columns = {
        "account_id": SourceColumn("BIGINT", False),
        "day": SourceColumn("DATE", False),
        "balance": SourceColumn("DECIMAL(18,2)"),
    }
    catalog = {"t": columns, "accounts": {"account_id": SourceColumn("BIGINT", False)}}

    def own(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"vendor_name": ce.VENDORS[0], "data": json.dumps({"version": 1, **payload})}]

    document = {
        "version": "0.2.0.dev0", "name": "snapshots",
        "datasets": [
            {"name": "t", "source": "t", "primary_key": ["account_id", "day"],
             "fields": [{"name": n, "expression": {"sql": n}} for n in columns],
             "custom_extensions": own({"element": "dataset", "time_roles": {"snapshot": "day"}})},
            {"name": "accounts", "source": "accounts", "primary_key": ["account_id"],
             "fields": [{"name": "account_id", "expression": {"sql": "account_id"}}],
             "custom_extensions": own({"element": "dataset", "entity": "account"})},
        ],
        "relationships": [{"name": "account", "from": "t", "to": "accounts",
                           "from_columns": ["account_id"], "to_columns": ["account_id"]}]
        if relationship else [],
        "metrics": [{"name": "balance", "expression": {"sql": "SUM(t.balance)"},
                     "custom_extensions": own({
                         "element": "metric", "additivity": "semi_additive",
                         "non_additive_axes": [
                             {"dimension": "day", "behavior": "period_end"},
                             {"entity": "account", "behavior": "additive"}],
                         "period_end": {"order_by": ["t.day"]}})}],
        "custom_extensions": own({"element": "model", "default_time_zone": "UTC"}),
    }  # fmt: skip
    loaded = read(json.dumps(document).encode(), "snapshots.ossie.json", ce.VENDORS)
    return compile_model(loaded, catalog, ce.VENDORS), catalog


BALANCES = [("1", "2026-01-01", "10.00"), ("1", "2026-01-31", "30.00"),
            ("2", "2026-01-15", "5.00")]  # fmt: skip


@pytest.mark.parametrize("level", ["visible", "hidden", "masked"])
def test_a_period_end_entity_through_an_unseen_key_answers_as_a_model_without_it(
    level: str,
) -> None:
    """The entity a semi-additive metric adds across may come from a relationship's key
    (`entity_fields`): for a caller who cannot use that relationship, the question answers
    exactly as on a model without it, never by reading the protected key."""
    rule = [] if level == "visible" else [
        {"member": "t.account_id", "default": level} | ({"mask": "*"} if level == "masked" else {})
    ]  # fmt: skip
    question = {"kind": "semantic_query", "version": 1, "mode": "aggregate",
                "metrics": [{"metric": "balance"}],
                "group_by": [{"member": "t.day", "bucket": {"unit": "month"}}]}  # fmt: skip

    def outcome_on(relationship: bool) -> tuple[str | None, Any]:
        compiled, catalog = snapshots(relationship=relationship)
        model = read_policies(yaml.safe_dump({"version": 1, "column_policies": rule}), compiled,
                              groups=frozenset(), managed_tables={})  # fmt: skip
        data: list[dict[str, str | None]] = [
            {"account_id": a, "day": d, "balance": b} for a, d, b in BALANCES
        ]
        with tiny_lake(catalog, data) as lake:
            try:
                found = ask(question, compiled, lake, policy=model)
            except SemanticError as error:
                return error.code, dict(error.details)
        return None, ce.rows_of(found)

    seen, absent = outcome_on(True), outcome_on(False)
    if level == "visible":  # the control: the relationship gives the entity
        assert seen == (None, [["2026-01-01", "35.00"]])
        assert absent[0] == "UNSUPPORTED_OPERATION"
        return
    assert seen == absent  # same refusal, same details: nothing tells the relationship exists
    assert seen[0] == "UNSUPPORTED_OPERATION"  # period_end_ties: no usable entity, no read
