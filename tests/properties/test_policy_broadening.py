"""P3-14 (#448): generated row and column policies never broaden what a caller sees.

Every generated world, mapping data, policy model, valid caller and question is answered by the
engine and judged by `policy_oracle`, which shares nothing with it:

- **P1** a detail answer keeps a subset of the rows by identity (the sale's primary key), every
  authorized field equal to its value without policies, and masks checked apart;
- **P2** every answer equals the oracle's, rows and notices;
- **P3** a restrictive change never widens the rows seen, and a permissive one widens them to
  exactly the oracle's new set; the oracle checks each change's class on the data itself;
- **P4** an absent attribute, a `NULL` in the data and an invalid context each have their own
  outcome;
- **P5** a hidden member is not found like a member that does not exist, a masked one refuses
  shaping, and no error, notice or receipt view carries a protected value.

Each check is a function that returns its problems; `test_each_check_catches_its_planted_bug`
shows that every one of them fails on a planted defect.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from orvex.semantic.errors import SemanticError
from orvex.semantic.policies import InvalidContext
from orvex.semantic.receipt.viewer import CallerViewer

from conformance.runners import corpus_execution as ce
from conformance.runners.security_cases import SEPARATOR
from tests.properties import policy_oracle as po
from tests.properties import policy_world as pw
from tests.properties.world import (
    COMPILED,
    PROFILE,
    World,
    aggregate_queries,
    detail_queries,
    sorted_rows,
    worlds,
)

Rows = list[list[str | None]]
QUESTIONS = st.one_of(aggregate_queries(), detail_queries())


def answered(
    world: World, data: pw.Mappings, policies: pw.Policies, caller: pw.Caller, query: pw.Document
) -> tuple[str | None, Rows, list[str], list[str], list[str]]:
    """The engine's answer: its error code (or None), rows over every page, columns, notices,
    and every text the caller receives (result, notices, receipt view, error)."""
    model = pw.compiled_policies(policies)
    context = caller.context()
    with pw.lake_with(world, data) as lake:
        try:
            if query["mode"] == "detail":
                rows, columns, notices = pw.every_page(query, lake, model, context)
                texts = [json.dumps(rows), json.dumps(notices)]
            else:
                found = pw.ask(query, lake, model, context)
                envelope = found.result.envelope()
                rows = ce.rows_of(found)
                columns = []
                notices = [str(n["code"]) for n in envelope.get("notices", [])]
                view = found.receipt.view(CallerViewer(COMPILED, model, context))
                texts = [json.dumps(envelope, default=str), json.dumps(view, default=str)]
        except SemanticError as error:
            text = json.dumps({"code": error.code, "details": dict(error.details)})
            return error.code, [], [], [], [text, error.to_json("es", "other")]
    return None, rows, columns, notices, texts


# --- the checks ------------------------------------------------------------------------------


def subset_problems(
    query: pw.Document, rules: Mapping[str, str], policed: Rows, full: Rows
) -> list[str]:
    """P1: rows by identity, authorized fields equal, masks apart."""
    members = [entry["member"] for entry in query["fields"]]
    key = members.index("sales.sale_id")
    without = {row[key]: row for row in full}
    found = []
    for row in policed:
        if row[key] not in without:
            found.append(f"row {row[key]} is not seen without policies")
            continue
        for at, member in enumerate(members):
            rule = rules.get(member, "visible")
            if rule == "masked" and row[at] != pw.MASK:
                found.append(f"{member} of row {row[key]} is not its mask")
            if rule == "visible" and row[at] != without[row[key]][at]:
                found.append(f"{member} of row {row[key]} differs from its value")
    return found


def oracle_problems(
    code: str | None, rows: Rows, notices: list[str], expected: po.Expected
) -> list[str]:
    """P2: the engine's answer is the oracle's."""
    if expected.error:
        return (
            [] if code in expected.error else [f"answered {code}, expected {set(expected.error)}"]
        )
    if code is not None:
        return [f"refused with {code}"]
    found = []
    if sorted_rows(rows) != sorted_rows([list(r) for r in expected.rows]):
        found.append(f"rows differ ({len(rows)} for {len(expected.rows)})")
    if sorted(notices) != list(expected.notices):
        found.append(f"notices {sorted(notices)}, expected {list(expected.notices)}")
    return found


def leak_problems(texts: list[str], protected: set[str]) -> list[str]:
    """P5: no whole token of a protected value anywhere the caller receives."""
    tokens = {t for text in texts for t in SEPARATOR.split(text) if t}
    return [f"{value} reaches the caller" for value in sorted(protected & tokens)]


def protected_values(world: World, policies: pw.Policies, caller: pw.Caller) -> set[str]:
    """The values of the members hidden or masked for this caller, apart from those the caller's
    own attributes hold (theirs to know)."""
    own = {
        str(v)
        for attribute in caller.attributes.values()
        for v in (attribute.get("values") or [attribute.get("value")])
    }
    rules = po.column_rules(policies, caller)
    found = set()
    for member, rule in rules.items():
        if rule in {"hidden", "masked"}:
            column = member.split(".")[1]
            found |= {str(c[column]) for c in world.customers if c[column] is not None}
    return found - own


# --- P1, P2, P5 ------------------------------------------------------------------------------


ORPHAN = World(
    [],
    [{"sale_id": "1", "customer_id": None, "amount": None, "qty": "0.00",
      "sold_on": "2026-01-01", "sold_at": "2026-04-05 02:59:00+00", "channel": "web"}],
)  # fmt: skip
BY_CHANNEL = {
    "kind": "semantic_query", "version": 1, "mode": "aggregate",
    "metrics": [{"metric": "revenue"}], "group_by": [{"member": "sales.channel"}],
}  # fmt: skip


@PROFILE
# Found nightly (#448): an unregistered caller saw a sale without a customer under a deny-list
# with `orphans: visible`; a missing registration now refuses every row the policy reaches.
@example(
    ORPHAN,
    pw.Mappings(),
    pw.Policies(({"name": "own_channel"}, {"name": "denied_regions", "orphans": "visible"})),
    pw.Caller(attributes={"channel": {"type": "text", "value": "web"}}),
    BY_CHANNEL,
)
@given(worlds(), pw.mappings(), pw.policy_models(), pw.callers(), QUESTIONS)
def test_every_answer_is_the_oracles_and_leaks_nothing_protected(
    world: World,
    data: pw.Mappings,
    policies: pw.Policies,
    caller: pw.Caller,
    query: pw.Document,
) -> None:
    """P2 and P5 on every question."""
    code, rows, _, notices, texts = answered(world, data, policies, caller, query)
    expected = po.expected(world, data, policies, caller, query)
    assert oracle_problems(code, rows, notices, expected) == []
    assert leak_problems(texts, protected_values(world, policies, caller)) == []


@PROFILE
@given(worlds(), pw.mappings(), pw.policy_models(), pw.callers(), detail_queries())
def test_a_detail_answer_is_a_subset_by_identity_with_masks_apart(
    world: World,
    data: pw.Mappings,
    policies: pw.Policies,
    caller: pw.Caller,
    query: pw.Document,
) -> None:
    """P1."""
    rules = po.column_rules(policies, caller)
    code, policed, _, _, _ = answered(world, data, policies, caller, query)
    if code is not None:  # a refusal shows no row (P2 checks its code)
        return
    _, full, _, _, _ = answered(world, data, pw.Policies(), caller, query)
    assert subset_problems(query, rules, policed, full) == []


# --- P3, monotonic by meaning ----------------------------------------------------------------


def changes(
    policies: pw.Policies, caller: pw.Caller, data: pw.Mappings
) -> list[tuple[str, bool, pw.Policies, pw.Caller, pw.Mappings]]:
    """Every change of one step this world allows: (name, restrictive, the changed world).
    A permissive change is the inverse of a restrictive one."""
    found: list[tuple[str, bool, pw.Policies, pw.Caller, pw.Mappings]] = []
    named = {rule["name"] for rule in policies.rows}
    for name in sorted(set(pw.SHAPES) - named):
        added = replace(policies, rows=(*policies.rows, {"name": name}))
        found.append((f"add policy {name}", True, added, caller, data))
    for rule in policies.rows:
        kept = tuple(r for r in policies.rows if r is not rule)
        found.append(
            (f"remove policy {rule['name']}", False, replace(policies, rows=kept), caller, data)
        )
    for group in pw.GROUPS:
        if group in caller.groups:
            fewer = replace(caller, groups=tuple(g for g in caller.groups if g != group))
            found.append((f"remove group {group}", True, policies, fewer, data))
        else:
            more = replace(caller, groups=(*caller.groups, group))
            found.append((f"add group {group}", False, policies, more, data))
    for attribute in sorted(caller.attributes):
        found.append(
            (f"remove attribute {attribute}", True, policies, caller.without(attribute), data)
        )
    for entry in data.allow:
        fewer_allow = replace(data, allow=tuple(e for e in data.allow if e != entry))
        found.append((f"remove allow {entry}", True, policies, caller, fewer_allow))
    for entry in ((pw.USER, "web"), (pw.USER, "shop"), (pw.USER, None)):
        if entry not in data.allow:
            more_allow = replace(data, allow=(*data.allow, entry))
            found.append((f"add allow {entry}", False, policies, caller, more_allow))
    for entry in ((pw.USER, "North"), (pw.USER, "South"), (pw.USER, None)):
        if entry not in data.deny:
            more_deny = replace(data, deny=(*data.deny, entry))
            found.append((f"add deny {entry}", True, policies, caller, more_deny))
    for entry in data.deny:
        fewer_deny = replace(data, deny=tuple(e for e in data.deny if e != entry))
        found.append((f"remove deny {entry}", False, policies, caller, fewer_deny))
    if pw.USER in data.registered:
        gone = replace(data, registered=tuple(u for u in data.registered if u != pw.USER))
        found.append(("unregister the caller", True, policies, caller, gone))
    else:
        joined = replace(data, registered=(*data.registered, pw.USER))
        found.append(("register the caller", False, policies, caller, joined))
    return found


def identities(code: str | None, rows: Rows) -> set[str | None]:
    """The sale ids of a detail answer; a refusal shows none."""
    return set() if code is not None else {row[0] for row in rows}


@PROFILE
@given(worlds(), pw.mappings(), pw.policy_models(), pw.callers(), detail_queries(), st.data())
def test_a_restrictive_change_never_widens_and_a_permissive_one_is_exact(
    world: World,
    data: pw.Mappings,
    policies: pw.Policies,
    caller: pw.Caller,
    query: pw.Document,
    draws: st.DataObject,
) -> None:
    """P3. The oracle first confirms the change's class on this data."""
    name, restrictive, policies2, caller2, data2 = draws.draw(
        st.sampled_from(changes(policies, caller, data)), label="change"
    )
    before = po.expected(world, data, policies, caller, query)
    after = po.expected(world, data2, policies2, caller2, query)
    seen_before = set() if before.error else {r[0] for r in before.rows}
    seen_after = set() if after.error else {r[0] for r in after.rows}
    if restrictive:
        assert seen_after <= seen_before, f"the oracle widens on {name}: misclassified"
    code, rows, _, _, _ = answered(world, data, policies, caller, query)
    code2, rows2, _, notices2, _ = answered(world, data2, policies2, caller2, query)
    if restrictive:
        assert identities(code2, rows2) <= identities(code, rows), name
    else:
        assert oracle_problems(code2, rows2, notices2, after) == [], name


def test_a_change_is_classified_by_the_policies_meaning() -> None:
    """P3's classes: for an allow-list, removing an entry restricts; for a deny-list, adding
    one restricts and removing one is permissive (Owner correction 1)."""
    policies = pw.Policies(({"name": "allowed_channels"}, {"name": "denied_regions"}))
    data = pw.Mappings(allow=((pw.USER, "web"),), deny=((pw.USER, "North"),), registered=(pw.USER,))
    classes = {name: restrictive for name, restrictive, *_ in changes(policies, pw.Caller(), data)}
    assert classes["remove allow ('u1', 'web')"] is True
    assert classes["add allow ('u1', 'shop')"] is False
    assert classes["add deny ('u1', 'South')"] is True
    assert classes["remove deny ('u1', 'North')"] is False
    assert classes["unregister the caller"] is True


# --- P4, three kinds of absence --------------------------------------------------------------


def test_an_absent_attribute_gives_zero_rows_with_its_notice_and_names_nothing() -> None:
    """P4a."""
    world = World(
        [{"customer_id": "1", "region": "North", "segment": "a"}],
        [{"sale_id": "1", "customer_id": "1", "amount": "1.00", "qty": "1.00",
          "sold_on": "2026-01-01", "sold_at": "2026-01-01 00:00:00+00", "channel": "web"}],
    )  # fmt: skip
    policies = pw.Policies(({"name": "own_channel"}, {"name": "allowed_channels"}))
    query = {
        "kind": "semantic_query", "version": 1, "mode": "aggregate",
        "metrics": [{"metric": "revenue"}],
    }  # fmt: skip
    code, rows, _, notices, texts = answered(world, pw.Mappings(), policies, pw.Caller(), query)
    assert code is None
    assert rows == [[None]]  # the grand total over no row
    assert sorted(notices) == ["POLICY_ATTRIBUTE_MISSING", "POLICY_MAPPING_MISSING"]
    for named in ("own_channel", "allowed_channels", "channel_allow", "security.channel_allow"):
        assert leak_problems(texts, {named}) == [], named


def test_a_null_in_the_data_never_matches_and_a_null_deny_entry_blocks_every_row() -> None:
    """P4b, on the oracle's three-valued logic, checked against the engine."""
    world = World(
        [{"customer_id": "1", "region": None, "segment": "a"},
         {"customer_id": "2", "region": "North", "segment": "b"}],
        [{"sale_id": str(i), "customer_id": c, "amount": "1.00", "qty": "1.00",
          "sold_on": "2026-01-01", "sold_at": "2026-01-01 00:00:00+00", "channel": ch}
         for i, (c, ch) in enumerate((("1", None), ("2", "web"), ("1", "web")), 1)],
    )  # fmt: skip
    detail = {
        "kind": "semantic_query", "version": 1, "mode": "detail",
        "fields": [{"member": "sales.sale_id"}],
        "order": [{"by": "sales.sale_id", "direction": "asc"}], "page": {"size": "10"},
    }  # fmt: skip
    web = pw.Caller(attributes={"channel": {"type": "text", "value": "web"}})
    deny = pw.Policies(({"name": "denied_regions"},))
    cases = [
        (pw.Policies(({"name": "own_channel"},)), web, pw.Mappings(), [["2"], ["3"]]),
        (pw.Policies(({"name": "allowed_channels"},)), web,
         pw.Mappings(allow=((pw.USER, None),)), []),
        (deny, web, pw.Mappings(deny=((pw.USER, None),), registered=(pw.USER,)), []),
        (deny, web, pw.Mappings(deny=((pw.USER, "South"),), registered=(pw.USER,)), [["2"]]),
        (deny, web, pw.Mappings(registered=(pw.USER,)), [["1"], ["2"], ["3"]]),
    ]  # fmt: skip
    for policies, caller, data, rows in cases:
        code, found, _, notices, _ = answered(world, data, policies, caller, detail)
        expected = po.expected(world, data, policies, caller, detail)
        assert code is None, (policies, data)
        assert notices == [], (policies, data)
        assert found == rows == [list(r) for r in expected.rows], (policies, data)


@PROFILE
@given(pw.invalid_contexts())
def test_an_invalid_context_is_refused_before_any_answer(context: pw.Document) -> None:
    """P4c: a context that breaks `security-context` v1 is never answered."""
    world = World([], [])
    model = pw.compiled_policies(pw.Policies(({"name": "own_channel"},)))
    query = {"kind": "semantic_query", "version": 1, "mode": "aggregate",
             "metrics": [{"metric": "revenue"}]}  # fmt: skip
    with pw.lake_with(world, pw.Mappings()) as lake, pytest.raises(InvalidContext):
        pw.ask(query, lake, model, context)


# --- P5, hidden as absent ----------------------------------------------------------------------


def test_a_hidden_member_is_refused_as_one_that_does_not_exist() -> None:
    """P5: the same code and the same details' shape: nothing confirms it exists."""
    world = World([], [])
    policies = pw.Policies(columns=({"member": "customers.segment", "default": "hidden"},))

    def refused(member: str) -> SemanticError:
        query = {"kind": "semantic_query", "version": 1, "mode": "aggregate",
                 "metrics": [{"metric": "revenue"}], "group_by": [{"member": member}]}  # fmt: skip
        with pw.lake_with(world, pw.Mappings()) as lake, pytest.raises(SemanticError) as caught:
            pw.ask(query, lake, pw.compiled_policies(policies), pw.Caller().context())
        return caught.value

    hidden, absent = refused("customers.segment"), refused("customers.nickname")
    assert hidden.code == absent.code == "MEMBER_NOT_FOUND"
    assert set(hidden.details) == set(absent.details)


# --- every check can fail --------------------------------------------------------------------


def test_each_check_catches_its_planted_bug() -> None:
    detail: dict[str, Any] = {
        "fields": [{"member": "sales.sale_id"}, {"member": "customers.region"}]
    }
    rules = {"customers.region": "masked"}
    full: Rows = [["1", "North"], ["2", "South"]]
    assert subset_problems(detail, rules, [["1", "***"]], full) == []
    assert subset_problems(detail, rules, [["3", "***"]], full)  # a row seen only with the bug
    assert subset_problems(detail, rules, [["1", "North"]], full)  # a mask skipped
    assert subset_problems(detail, {}, [["1", "South"]], full)  # a field changed
    expected = po.Expected(rows=(("1",),), notices=("POLICY_MAPPING_MISSING",))
    assert oracle_problems(None, [["1"]], ["POLICY_MAPPING_MISSING"], expected) == []
    assert oracle_problems(None, [["1"], ["2"]], ["POLICY_MAPPING_MISSING"], expected)  # dropped
    assert oracle_problems(None, [["1"]], [], expected)  # a notice lost
    assert oracle_problems("MEMBER_NOT_FOUND", [], [], expected)  # an answer refused
    assert oracle_problems(None, [], [], po.Expected(error=frozenset({"MEMBER_MASKED"})))
    assert leak_problems(['{"rows": [["North"]]}'], {"North"})  # a protected value shown
    assert leak_problems(['{"rows": [["Northern"]]}'], {"North"}) == []  # whole tokens only
