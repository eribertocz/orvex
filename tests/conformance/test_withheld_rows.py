"""Fail closed on missing attributes and mappings (#443 AC2, AC2b, AC2c, AC3): zero rows on
every engine, never all rows, with the same notices, why each policy withheld its rows, and
the exact SQL cardinality of an empty set: no row for a detail or grouped question, one row of
empty-set values (`COUNT` 0, `SUM` `NULL`, a ratio's `zero_denominator`) for an ungrouped one.

On a copy of the `direct-query` fixture that adds the row policy `own_zone` (`zone_id =
$zone_id`) and the ratio `minutes_per_outage`, served by the managed lake, the existing DuckLake
(SQLite and PostgreSQL catalogs) and ClickHouse (CI).
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from orvex.contracts.store.v1 import StatementRequest
from orvex.semantic.compiler.compiled import compile_model
from orvex.semantic.execution import answer
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import read_policies

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from tests.support.fixtures import frozen
from tests.support.security_engines import ask, served

BASE = sf.fixture("direct-query", ce.VENDORS[0])
OWN_ZONE = {"name": "own_zone", "dataset": "outages", "kind": "predicate",
            "predicate": "zone_id = $zone_id"}  # fmt: skip
RATIO = {
    "element": "metric", "additivity": "recomputable",
    "components": {"numerator": "SUM(outages.minutes)", "denominator": "COUNT(outages.outage_id)"},
    "zero_denominator": "null", "rounding": {"scale": 2, "mode": "half_away_from_zero"},
}  # fmt: skip
ENGINES = [("managed", ""), ("existing-ducklake", "sqlite"), ("existing-ducklake", "postgres"),
           ("clickhouse", "")]  # fmt: skip
IDS = [f"{e}-{c}" if c else e for e, c in ENGINES]
Z1 = {"type": "text", "value": "Z1"}


@pytest.fixture(scope="module")
def found(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sf.Fixture]:
    folder = Path(tmp_path_factory.mktemp("withheld")) / "direct-query"
    shutil.copytree(BASE.folder, folder)
    policies = yaml.safe_load((folder / "security.yaml").read_text("utf-8"))
    policies["row_policies"].append(OWN_ZONE)
    (folder / "security.yaml").write_text(yaml.safe_dump(policies), "utf-8")
    model = json.loads((folder / "model.ossie.json").read_text("utf-8"))
    model["metrics"].append({
        "name": "minutes_per_outage",
        "expression": {"sql": "SUM(outages.minutes) / COUNT(outages.outage_id)"},
        "custom_extensions": [{"vendor_name": "{{vendor}}",
                               "data": json.dumps({"version": 1, **RATIO})}],
    })  # fmt: skip
    (folder / "model.ossie.json").write_text(json.dumps(model), "utf-8")
    text = (folder / "model.ossie.json").read_text("utf-8").replace("{{vendor}}", ce.VENDORS[0])
    loaded = read(text.encode(), "model.ossie.json", ce.VENDORS)
    compiled = compile_model(loaded, BASE.catalog, ce.VENDORS)
    for shared in frozen(folder):
        yield dataclasses.replace(BASE, folder=shared, compiled=compiled)


def caller(user: str, **attributes: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "kind": "security_context", "version": 1, "user": user, "project": "direct-query",
        "role": "author", "groups": [], "attributes": attributes,
        "permission_fingerprint": "sha256:" + "0" * 64,
    }  # fmt: skip


def question(body: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "semantic-query",
            "body": {"kind": "semantic_query", "version": 1, **body}}  # fmt: skip


METRICS = [{"metric": m} for m in ("outage_count", "zone_count", "outage_minutes",
                                    "minutes_per_outage")]  # fmt: skip
UNGROUPED = question({"mode": "aggregate", "metrics": METRICS})
GROUPED = question({"mode": "aggregate", "metrics": METRICS,
                    "group_by": [{"member": "outages.zone_id"}],
                    "order": [{"by": "outages.zone_id"}]})  # fmt: skip
TOTALS = question({"mode": "aggregate", "metrics": METRICS,
                   "group_by": [{"member": "outages.zone_id"}],
                   "order": [{"by": "outages.zone_id"}], "totals": "grand"})  # fmt: skip
EMPTY = ["0", "0", None, None]  # COUNT, COUNT DISTINCT, SUM, the ratio's zero_denominator
# Why each caller gets no row: (caller, notices, the policies that withheld it).
CALLERS = {
    "attribute absent": (caller("u-zone1"), ["POLICY_ATTRIBUTE_MISSING"]),
    "attribute of another type": (
        caller("u-zone1", zone_id={"type": "integer", "value": "1"}),
        ["POLICY_ATTRIBUTE_MISSING"],
    ),
    "allow-list without a mapping row": (caller("u-open", zone_id=Z1), ["POLICY_MAPPING_MISSING"]),
    "no mapping and no registration": (
        caller("u-none", zone_id=Z1),
        ["POLICY_MAPPING_MISSING", "POLICY_MAPPING_MISSING"],
    ),
}


@pytest.mark.parametrize(("engine", "catalog"), ENGINES, ids=IDS)
def test_every_engine_withholds_every_row_with_the_same_notices(
    found: sf.Fixture, engine: str, catalog: str
) -> None:
    with served(found, engine, catalog) as place:
        control = ask(found, place, UNGROUPED, caller("u-zone1", zone_id=Z1))
        assert control.code is None, control
        assert control.rows == [["1", "1", "15.00", "15.00"]]  # u-zone1 in Z1: outage 1
        for label, (context, notices) in CALLERS.items():
            empty = ask(found, place, UNGROUPED, context)
            assert empty.code is None, (label, empty)
            assert empty.rows == [EMPTY], label  # exactly one row of empty-set values
            assert empty.notices == notices, label
            assert empty.columns == control.columns, label  # the same types and nullability
            grouped = ask(found, place, GROUPED, context)
            assert grouped.rows == [], label  # no group: no row
            assert grouped.notices == notices, label
            totals = ask(found, place, TOTALS, context)
            assert totals.code is None, (label, totals)
            assert totals.rows == [[None, *EMPTY]], label  # the grand total of no group
            assert totals.notices == notices, label


@pytest.mark.parametrize("label", sorted(CALLERS))
def test_a_detail_question_has_no_row(found: sf.Fixture, label: str) -> None:
    context, notices = CALLERS[label]
    detail = question({"mode": "detail", "fields": [{"member": "outages.outage_id"}],
                       "order": [{"by": "outages.outage_id"}], "page": {"size": "10"}})  # fmt: skip
    with served(found, "managed", "") as place:
        received = ask(found, place, detail, context)
    assert received.rows == []
    assert received.notices == notices


def test_the_policies_that_withheld_rows_are_named_for_the_audit_only(found: sf.Fixture) -> None:
    with served(found, "managed", "") as place:
        held = answer(json.dumps(UNGROUPED["body"]), found.compiled, found.policies(sf.GROUPS),
                      caller("u-none", zone_id=Z1), place.engine()).withheld  # fmt: skip
    assert [(w.code, w.dataset, w.policy) for w in held] == [
        ("POLICY_MAPPING_MISSING", "outages", "allowed_zones"),
        ("POLICY_MAPPING_MISSING", "outages", "blocked_zones"),
    ]


@pytest.mark.parametrize(("engine", "catalog"), ENGINES[:3], ids=IDS[:3])
def test_a_mapping_is_read_at_the_questions_own_snapshot(
    found: sf.Fixture, engine: str, catalog: str
) -> None:
    """`u-open` gains a mapping row in a later snapshot: the earlier snapshot still has none."""
    context = caller("u-open", zone_id=Z1)
    with served(found, engine, catalog) as place:
        before = place.engine()
        with place.lake.transaction() as transaction:
            transaction.execute(StatementRequest(
                kind="statement", parameters=[],
                sql="INSERT INTO \"security\".\"user_zone\" VALUES ('u-open', 'Z1')"))  # fmt: skip
        after = place.engine()
        assert after.snapshot["id"] != before.snapshot["id"]

        def held(engine: Any) -> list[str]:  # noqa: ANN401
            found_ = answer(json.dumps(UNGROUPED["body"]), found.compiled,
                            found.policies(sf.GROUPS), context, engine)  # fmt: skip
            return [w.policy for w in found_.withheld]

        assert held(before) == ["allowed_zones"]
        assert held(after) == []
        assert held(replace(after, snapshot=before.snapshot)) == ["allowed_zones"]


ATTRIBUTE = st.one_of(  # never a text naming a zone: absent, unfit, or naming none
    st.none(),
    st.builds(lambda v: {"type": "integer", "value": str(v)}, st.integers(-5, 5)),
    st.builds(lambda v: {"type": "boolean", "value": v}, st.booleans()),
    st.builds(lambda v: {"type": "list", "values": v},
              st.lists(st.sampled_from(["Z1", "Z2", "Z3"]), min_size=1, unique=True)),
    st.builds(lambda v: {"type": "text", "value": v}, st.sampled_from(["Z9", "z1", " Z1"])),
)  # fmt: skip


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(user=st.sampled_from(["u-zone1", "u-open", "u-none", "u-deny"]),
       zone=ATTRIBUTE)  # fmt: skip
def test_never_all_rows_whatever_the_attribute(found: sf.Fixture, user: str, zone: Any) -> None:  # noqa: ANN401
    """AC3: no context without a fitting `zone_id` that names a zone sees any row."""
    context = caller(user) if zone is None else caller(user, zone_id=zone)
    with served(found, "managed", "") as place:
        received = ask(found, place, UNGROUPED, context)
    assert received.code is None, received
    assert received.rows == [EMPTY]


def test_a_registered_policy_still_names_its_empty_allow_list(found: sf.Fixture) -> None:
    """A policy with a `registration` and a positive mapping conjunct: a registered caller with
    no mapping row is withheld by the conjunct, and named for it."""
    both = {"version": 1, "row_policies": [{
        "name": "zones", "dataset": "outages", "kind": "predicate",
        "predicate": "zone_id IN (SELECT zone_id FROM security.user_zone WHERE user_id = $user_id)"
                     " AND zone_id NOT IN (SELECT zone_id FROM security.blocked_zone"
                     " WHERE user_id = $user_id)",
        "registration": {"table": "security.registered", "key": "user_id"}}]}  # fmt: skip
    policies = read_policies(yaml.safe_dump(both), found.compiled, groups=sf.GROUPS,
                             managed_tables=found.managed)  # fmt: skip
    with served(found, "managed", "") as place:

        def held(user: str) -> tuple[list[list[Any]], list[str]]:
            answered = answer(json.dumps(UNGROUPED["body"]), found.compiled, policies,
                              caller(user), place.engine())  # fmt: skip
            return ce.rows_of(answered), [w.policy for w in answered.withheld]

        assert held("u-open") == ([EMPTY], ["zones"])  # registered, no mapping row
        assert held("u-zone1") == ([["1", "1", "15.00", "15.00"]], [])  # registered and mapped
        assert held("u-none") == ([EMPTY], ["zones"])  # not registered
