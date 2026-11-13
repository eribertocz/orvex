"""Cell conditions (`visible_when`, #238 rule 9), executed (#442 AC6): a detail row shows a
member's value where its condition holds for that row and caller, and its mask elsewhere; the
column is `VARCHAR` text for every row, never typed; the member is masked for every other use.

On a `utility` copy with two conditions:
- `consumers.segment`, shown in the caller's own zones (a mapping table, `$user_id`);
- `readings.kwh`, shown where `zone_id = $zone_id` (a real user attribute).

The callers are in `management`, exempt from the row policies, so every row is read and only
the conditions decide what shows. Each expected answer is computed here from the fixture's rows.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
import yaml
from orvex.semantic.correctness.discharge import DischargeError, verify_physical
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import answer
from orvex.semantic.explanation import explain
from orvex.semantic.policies import read_policies
from orvex.semantic.receipt.viewer import CallerViewer

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from tests.conformance.test_detail_rows import FORM
from tests.conformance.test_execution import Spy

FOUND = sf.fixture("utility", ce.VENDORS[0])
UTILITY = yaml.safe_load((sf.SECURITY / "fixtures/utility/security.yaml").read_text("utf-8"))
SEGMENT_WHEN = "zone_id IN (SELECT zone_id FROM security.user_zone WHERE user_id = $user_id)"
KWH_WHEN = "zone_id = $zone_id"
CONDITIONS = [
    {"member": "consumers.segment", "default": "visible", "mask": "***",
     "visible_when": SEGMENT_WHEN, "visible_to_groups": ["operations"]},
    {"member": "readings.kwh", "default": "visible", "mask": "***", "visible_when": KWH_WHEN},
]  # fmt: skip
TEXT = yaml.safe_dump(UTILITY | {"column_policies": [*UTILITY["column_policies"], *CONDITIONS]})
POLICIES = read_policies(TEXT, FOUND.compiled, groups=sf.GROUPS, managed_tables=FOUND.managed)


def caller(user: str, **attributes: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "kind": "security_context",
        "version": 1,
        "user": user,
        "project": "utility",
        "role": "author",
        "groups": ["management"],
        "attributes": attributes,
        "permission_fingerprint": "sha256:" + "0" * 64,
    }


def zone(value: str) -> dict[str, str]:
    return {"type": "text", "value": value}


def rows_of(name: str) -> list[list[str | None]]:
    (table,) = [t for t in FOUND.rows if t.endswith(f".{name}")]
    return FOUND.rows[table]


def mapped(user: str) -> set[str]:
    return {str(z) for u, z in rows_of("user_zone") if u == user and z is not None}


def detail(*fields: str, size: str = "100") -> dict[str, Any]:
    return {
        "kind": "semantic_query", "version": 1, "mode": "detail",
        "fields": [{"member": f} for f in fields],
        "order": [{"by": fields[0], "direction": "asc"}],
        "page": {"size": size},
    }  # fmt: skip


def pages(document: dict[str, Any], context: dict[str, Any]) -> tuple[list[list[Any]], list[Any]]:
    """Every page of a detail query, in order, and every page's envelope."""
    seen: list[list[Any]] = []
    envelopes: list[Any] = []
    with sf.managed_lake(FOUND) as lake:
        snapshot = ce.engine_for(lake).snapshot
        more = {
            "cursors": FORM,
            "snapshot": snapshot,
            "pinned": lambda v: snapshot if v == str(snapshot["id"]) else None,
        }
        while True:
            found = answer(json.dumps(document), FOUND.compiled, POLICIES, context,
                           ce.engine_for(lake, **more))  # fmt: skip
            seen += ce.rows_of(found)
            envelopes.append(found)
            following = found.result.envelope()["page"].get("next")
            if following is None:
                return seen, envelopes
            document = {**document, "page": {**document["page"], "after": following}}


def column(found: Any, name: str) -> dict[str, Any]:  # noqa: ANN401
    (only,) = [c for c in found.result.envelope()["columns"] if c["name"] == name]
    return dict(only)


TEXT_COLUMN = {
    "encoding": "VARCHAR", "nullable": True, "role": "field", "logical": {"family": "String"},
    "exactness": "exact",
}  # fmt: skip


def test_a_mapping_condition_shows_the_value_in_the_callers_zones_only_on_every_page() -> None:
    seen, envelopes = pages(detail("consumers.consumer_id", "consumers.segment", size="1"),
                            caller("u-zone1"))  # fmt: skip
    zones = mapped("u-zone1")
    expected = [[c[0], c[5] if c[1] in zones else "***"] for c in sorted(rows_of("consumers"))]
    assert seen == expected  # C1, C2 shown; C3, C4 masked
    for found in envelopes:  # the same descriptor on every page, whatever its row shows
        described = column(found, "segment")
        assert {k: described[k] for k in TEXT_COLUMN} == TEXT_COLUMN
        assert not {"masked", "mask", "unit", "currency", "semantic_type"} & set(described)


def test_a_user_attribute_condition_shows_its_rows_as_text_and_null_stays_null() -> None:
    seen, envelopes = pages(detail("readings.reading_id", "readings.kwh"),
                            caller("u-zone1", zone_id=zone("Z2")))  # fmt: skip
    expected = [[r[0], r[4] if r[3] == "Z2" else "***"] for r in rows_of("readings")]
    assert seen == expected  # 3 -> "30.00", 5 -> None (its value is NULL), others "***"
    assert ["5", None] in seen
    assert ["3", "30.00"] in seen
    described = column(envelopes[0], "kwh")
    assert {k: described[k] for k in TEXT_COLUMN} == TEXT_COLUMN  # never DECIMAL


@pytest.mark.parametrize(
    ("label", "context"),
    [
        ("no zone_id attribute", caller("u-zone1")),
        ("zone_id of another type", caller("u-zone1", zone_id={"type": "integer", "value": "2"})),
    ],
)
def test_a_missing_or_unfit_attribute_masks_every_row(label: str, context: dict[str, Any]) -> None:
    seen, _ = pages(detail("readings.reading_id", "readings.kwh"), context)
    assert [r[1] for r in seen] == ["***"] * len(rows_of("readings")), label


def test_a_caller_with_no_mapping_sees_only_masks() -> None:
    seen, _ = pages(detail("consumers.consumer_id", "consumers.segment"), caller("u-none"))
    assert [r[1] for r in seen] == ["***"] * len(rows_of("consumers"))


def test_a_caller_in_a_visible_group_gets_the_member_as_compiled_on_every_row() -> None:
    context = caller("u-zone1") | {"groups": ["management", "operations"]}
    with sf.managed_lake(FOUND) as lake:
        found = answer(json.dumps(detail("consumers.consumer_id", "consumers.segment")),
                       FOUND.compiled, POLICIES, context, ce.engine_for(lake))  # fmt: skip
    assert [r[1] for r in ce.rows_of(found)] == [c[5] for c in sorted(rows_of("consumers"))]
    assert column(found, "segment")["nullable"] is False  # the compiled column, no condition


REFUSED = {
    "group": {"kind": "semantic_query", "version": 1, "mode": "aggregate",
              "metrics": [{"metric": "consumer_count"}],
              "group_by": [{"member": "consumers.segment"}]},
    "filter": {**detail("consumers.consumer_id"), "filters": [{
        "scope": "visual", "member": "consumers.segment",
        "predicate": {"op": "in", "values": [{"type": "text", "value": "residential"}],
                      "include_null": False}}]},
    "sort": {**detail("consumers.consumer_id", "consumers.segment"),
             "order": [{"by": "consumers.segment", "direction": "asc"}]},
    "derived metric": {"kind": "semantic_query", "version": 1, "mode": "aggregate",
                       "metrics": [{"metric": "total_kwh"}]},
}  # fmt: skip


@pytest.mark.parametrize("use", sorted(REFUSED))
def test_every_other_use_is_refused_before_any_read(use: str) -> None:
    spy = Spy()
    with sf.managed_lake(FOUND) as lake, pytest.raises(SemanticError) as caught:
        answer(json.dumps(REFUSED[use]), FOUND.compiled, POLICIES,
               caller("u-zone1", zone_id=zone("Z1")), ce.engine_for(lake, read=spy))  # fmt: skip
    assert spy.calls == 0
    expected = "MEMBER_NOT_FOUND" if use == "derived metric" else "MEMBER_MASKED"
    assert caught.value.code == expected, caught.value  # a derived metric is hidden (Q1)


def test_no_receipt_or_explain_names_a_condition_or_its_outcome() -> None:
    context = caller("u-zone1", zone_id=zone("Z1"))
    document = detail("readings.reading_id", "readings.kwh", "consumers.segment")
    _, envelopes = pages(document, context)
    with sf.managed_lake(FOUND) as lake:
        explained = explain(
            json.dumps(document),
            FOUND.compiled,
            POLICIES,
            context,
            ce.engine_for(lake),
            CallerViewer(FOUND.compiled, POLICIES, context),
        )
    texts = [json.dumps(explained, default=str)]
    for found in envelopes:
        texts.append(
            json.dumps(
                found.receipt.view(CallerViewer(FOUND.compiled, POLICIES, context)), default=str
            )
        )
        texts.append(json.dumps(found.result.envelope(), default=str))
    for text in texts:
        for secret in ("visible_when", "$zone_id", "user_zone", "Z2", SEGMENT_WHEN, KWH_WHEN):
            assert secret not in text, secret
    assert all("cells" not in e.result.envelope() for e in envelopes)  # no per-row state


def test_a_statement_reading_a_masked_field_without_its_policy_condition_never_runs() -> None:
    """C9 checks the conditional read against the policy model, not the placement: the same
    plan, checked against policies without the condition, is refused."""
    context = caller("u-zone1", zone_id=zone("Z2"))
    with sf.managed_lake(FOUND) as lake:
        found = answer(json.dumps(detail("readings.reading_id", "readings.kwh")), FOUND.compiled,
                       POLICIES, context, ce.engine_for(lake))  # fmt: skip
    verify_physical(found.plan, found.physical)
    masked = yaml.safe_dump(UTILITY | {"column_policies": [*UTILITY["column_policies"], {
        "member": "readings.kwh", "default": "masked", "mask": "***"}]})  # fmt: skip
    plain = read_policies(masked, FOUND.compiled, groups=sf.GROUPS, managed_tables=FOUND.managed)
    report = replace(found.plan.report, policies=plain)
    with pytest.raises(DischargeError, match="a masked field is read"):
        verify_physical(replace(found.plan, report=report), found.physical)


def test_a_conditional_read_not_in_its_guarded_form_never_runs() -> None:
    context = caller("u-zone1", zone_id=zone("Z2"))
    with sf.managed_lake(FOUND) as lake:
        found = answer(json.dumps(detail("readings.reading_id", "readings.kwh")), FOUND.compiled,
                       POLICIES, context, ce.engine_for(lake))  # fmt: skip
    sql = found.physical.sql
    assert "CASE WHEN COALESCE((" in sql
    unguarded = sql.replace("CASE WHEN COALESCE((", "CASE WHEN TRUE OR ((", 1)
    with pytest.raises(DischargeError, match="a masked field is read"):
        verify_physical(found.plan, replace(found.physical, sql=unguarded))
