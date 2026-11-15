"""The independent oracle of the generated policies (P3-14, #448): what a caller may see, worked
out from the policies' documented meaning (`contracts/security-policies/v1/README.md`) in plain
Python over plain data. It shares nothing with the engine: no compiled model, no predicate
parser, no SQL.

- **Columns.** A member's rule applies unless the caller holds a group it is visible to: a
  hidden member does not exist (`MEMBER_NOT_FOUND` when a question names it); a masked one
  shows its mask in detail rows and refuses grouping, filtering and sorting (`MEMBER_MASKED`).
- **Row policies**, each unless the caller holds one of its `exempt_groups`, combined with AND:
  - an attribute a policy reads that is absent, or of a type that does not fit, gives zero rows
    with `POLICY_ATTRIBUTE_MISSING`;
  - an allow-list with no mapping row for the caller, or a deny-list that has not registered
    them, gives zero rows with `POLICY_MAPPING_MISSING`;
  - otherwise SQL's three-valued logic: `NULL` never matches; `x NOT IN (…)` over a set holding
    `NULL` matches nothing, and over the empty set matches every row, `NULL` included;
  - a policy on `customers` reaches `sales` through the many-to-one relationship: a sale is
    seen only when its customer is; a sale without a customer (an orphan) is seen only under
    `orphans: visible`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tests.properties import oracle
from tests.properties.policy_world import MASK, SHAPES, USER, Caller, Mappings, Policies
from tests.properties.world import World

Row = dict[str, object]
TYPES = {"text": "text", "list": "list", "integer": "integer"}


@dataclass(frozen=True)
class Expected:
    """The answer the caller must get: an error code, or rows (unordered) and notices."""

    error: frozenset[str] = frozenset()  # any of these codes; empty when rows are expected
    rows: tuple[tuple[str | None, ...], ...] = ()
    notices: tuple[str, ...] = ()  # sorted


def column_rules(policies: Policies, caller: Caller) -> dict[str, str]:
    """Each protected member's rule for this caller: `visible`, `masked` or `hidden`."""
    found = {}
    for rule in policies.columns:
        if rule.get("insight") and "insight" in caller.groups:
            found[rule["member"]] = "visible"
        else:
            found[rule["member"]] = rule["default"]
    return found


def refusal(query: Mapping[str, Any], rules: Mapping[str, str]) -> frozenset[str]:
    """The codes a question that names a protected member may be refused with; none when it
    may be answered."""
    named = [e["member"] for e in query.get("fields", ())]
    shaped = [e["member"] for e in query.get("group_by", ())]
    shaped += [e["member"] for e in query.get("filters", ())]
    shaped += [e["by"] for e in query.get("order", ())]
    codes = set()
    if any(rules.get(m) == "hidden" for m in named + shaped):
        codes.add("MEMBER_NOT_FOUND")
    if any(rules.get(m) == "masked" for m in shaped):
        codes.add("MEMBER_MASKED")
    return frozenset(codes)


def _fits(caller: Caller, name: str, kind: str) -> bool:
    given = caller.attributes.get(name)
    return given is not None and given.get("type") == TYPES[kind]


def _rule(rule: Mapping[str, Any], caller: Caller, data: Mappings) -> tuple[str | None, Any]:
    """One applying policy: the notice it gives (and then no row passes), or a row test."""
    name = rule["name"]
    shape = SHAPES[name]
    for attribute, kind in shape["reads"].items():
        if not _fits(caller, attribute, kind):
            return "POLICY_ATTRIBUTE_MISSING", None
    if name == "own_channel":
        wanted = caller.attributes["channel"]["value"]
        return None, lambda row: row["channel"] is not None and row["channel"] == wanted
    if name == "own_regions":
        listed = set(caller.attributes["regions"]["values"])
        return None, lambda row: row["region"] is not None and row["region"] in listed
    if name == "customer_bound":
        bound = int(caller.attributes["max_customer"]["value"])
        return None, lambda row: row["customer_id"] is not None and row["customer_id"] <= bound
    if name == "allowed_channels":
        mine = [channel for user, channel in data.allow if user == USER]
        if not mine:
            return "POLICY_MAPPING_MISSING", None
        allowed = {c for c in mine if c is not None}
        return None, lambda row: row["channel"] is not None and row["channel"] in allowed
    if name == "denied_regions":
        if USER not in data.registered:
            return "POLICY_MAPPING_MISSING", None
        denied = [region for user, region in data.deny if user == USER]
        if not denied:
            return None, lambda row: True  # NOT IN over nothing keeps every row
        if None in denied:
            return None, lambda row: False  # NOT IN over a set with NULL keeps none
        blocked = set(denied)
        return None, lambda row: row["region"] is not None and row["region"] not in blocked
    raise AssertionError(f"the oracle does not know policy {name!r}")


def seen(
    world: World, data: Mappings, policies: Policies, caller: Caller
) -> tuple[list[Row], tuple[str, ...]]:
    """The joined rows the caller may see, and the notices the policies give, sorted."""
    customers = {c["customer_id"]: c for c in _typed(world.customers)}
    sales = _typed(world.sales)
    notices: list[str] = []
    tests: list[tuple[str, str, Any]] = []  # (dataset, orphans, test)
    empty = False
    for rule in policies.rows:
        if rule.get("exempt") and "exempt" in caller.groups:
            continue
        notice, test = _rule(rule, caller, data)
        if notice is not None:
            notices.append(notice)
            empty = True
            continue
        tests.append((SHAPES[rule["name"]]["dataset"], rule.get("orphans", "hidden"), test))
    kept = []
    for sale in sales:
        customer = customers.get(sale["customer_id"])
        if empty:
            continue
        if not all(test(sale) for dataset, _, test in tests if dataset == "sales"):
            continue
        reached = [(orphans, test) for dataset, orphans, test in tests if dataset == "customers"]
        if customer is None and any(orphans != "visible" for orphans, _ in reached):
            continue
        if customer is not None and not all(test(customer) for _, test in reached):
            continue
        kept.append(sale["sale_id"])
    visible = [r for r in oracle.joined(world.customers, world.sales) if r["sales.sale_id"] in kept]
    return visible, tuple(sorted(notices))


def _typed(rows: Sequence[Mapping[str, str | None]]) -> list[Row]:
    return [
        {k: (int(v) if v is not None and k in {"customer_id", "sale_id"} else v)
         for k, v in row.items()}
        for row in rows
    ]  # fmt: skip


def expected(
    world: World, data: Mappings, policies: Policies, caller: Caller, query: Mapping[str, Any]
) -> Expected:
    rules = column_rules(policies, caller)
    codes = refusal(query, rules)
    if codes:
        return Expected(error=codes)
    rows, notices = seen(world, data, policies, caller)
    masks = {member: MASK for member, rule in rules.items() if rule == "masked"}
    found = oracle.expected_over(rows, query, masks)
    return Expected(rows=tuple(tuple(r) for r in found), notices=notices)
