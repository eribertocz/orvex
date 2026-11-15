"""Generated policies, callers and mapping data for the non-broadening properties (P3-14, #448).

The world of `world.py` (`sales` many-to-one `customers`), with what a policy can read beside
it: three managed-lake mapping tables, an allow-list, a deny-list and a registration table.
`policy_models()` draws a `security.yaml` valid by construction: up to three row policies from
`SHAPES` (comparison, `IN` list, integer bound, allow-list, deny-list with registration; on
`sales` or on `customers`, which propagates to `sales`), each maybe with `exempt_groups` and,
on `customers`, `orphans`; and up to two column policies on `customers.region` and
`customers.segment`. `callers()` draws valid `security-context` v1 documents only: each
attribute present with its type, present with a type that does not fit, or absent. `NULL`
enters through the data (`world.py`'s pools and `mappings()`), and an invalid context through
`invalid_contexts()` alone.

The oracle that judges the engine is `policy_oracle.py`; it imports nothing from here but plain
data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from hypothesis import strategies as st
from orvex.semantic.compiler.catalog import SourceColumn

Document = dict[str, Any]
USER = "u1"  # the caller; `u2` is somebody else in every mapping
GROUPS = ("exempt", "insight")  # the project's groups
MASK = "***"
MAPPINGS: dict[str, dict[str, SourceColumn]] = {
    "channel_allow": {
        "user_id": SourceColumn("VARCHAR", False),
        "channel": SourceColumn("VARCHAR"),
    },
    "region_deny": {"user_id": SourceColumn("VARCHAR", False), "region": SourceColumn("VARCHAR")},
    "registered": {"user_id": SourceColumn("VARCHAR", False)},
}
MANAGED = {f"security.{name}": list(columns) for name, columns in MAPPINGS.items()}

# Each row policy shape: its dataset, predicate, the attributes it reads with their types, and
# what it is to the oracle (`policy_oracle.passes`).
SHAPES: dict[str, Document] = {
    "own_channel": {
        "dataset": "sales",
        "predicate": "channel = $channel",
        "reads": {"channel": "text"},
    },
    "allowed_channels": {
        "dataset": "sales",
        "predicate": (
            "channel IN (SELECT channel FROM security.channel_allow WHERE user_id = $user_id)"
        ),
        "reads": {},
    },
    "own_regions": {
        "dataset": "customers",
        "predicate": "region IN ($regions)",
        "reads": {"regions": "list"},
    },
    "customer_bound": {
        "dataset": "customers",
        "predicate": "customer_id <= $max_customer",
        "reads": {"max_customer": "integer"},
    },
    "denied_regions": {
        "dataset": "customers",
        "predicate": (
            "region NOT IN (SELECT region FROM security.region_deny WHERE user_id = $user_id)"
        ),
        "registration": {"table": "security.registered", "key": "user_id"},
        "reads": {},
    },
}
COLUMNS = ("customers.region", "customers.segment")


@dataclass(frozen=True)
class Mappings:
    """The managed-lake mapping rows, as text (None is SQL NULL)."""

    allow: tuple[tuple[str, str | None], ...] = ()  # (user_id, channel)
    deny: tuple[tuple[str, str | None], ...] = ()  # (user_id, region)
    registered: tuple[str, ...] = ()


@dataclass(frozen=True)
class Policies:
    """A generated policy model as plain data: the oracle reads this, the engine its YAML."""

    rows: tuple[Document, ...] = ()  # each: name, exempt (bool), orphans ('hidden'|'visible')
    columns: tuple[Document, ...] = ()  # each: member, default, insight (bool)

    def document(self) -> Document:
        found: Document = {"version": 1}
        if self.rows:
            found["row_policies"] = [_row_policy(rule) for rule in self.rows]
        if self.columns:
            found["column_policies"] = [_column_policy(rule) for rule in self.columns]
        return found


def _row_policy(rule: Mapping[str, Any]) -> Document:
    shape = SHAPES[rule["name"]]
    made: Document = {
        "name": rule["name"],
        "dataset": shape["dataset"],
        "kind": "predicate",
        "predicate": shape["predicate"],
    }
    if "registration" in shape:
        made["registration"] = dict(shape["registration"])
    if rule.get("exempt"):
        made["exempt_groups"] = ["exempt"]
    if shape["dataset"] == "customers" and rule.get("orphans") == "visible":
        made["orphans"] = "visible"
    return made


def _column_policy(rule: Mapping[str, Any]) -> Document:
    made: Document = {"member": rule["member"], "default": rule["default"]}
    if rule["default"] == "masked":
        made["mask"] = MASK
    if rule.get("insight"):
        made["visible_to_groups"] = ["insight"]
    return made


@dataclass(frozen=True)
class Caller:
    """A valid `security-context` v1 caller: its groups and attributes."""

    groups: tuple[str, ...] = ()
    attributes: Mapping[str, Document] = field(default_factory=dict)

    def context(self) -> Document:
        return {
            "kind": "security_context",
            "version": 1,
            "user": USER,
            "project": "corpus",
            "role": "viewer",
            "groups": sorted(set(self.groups)),
            "attributes": {k: dict(v) for k, v in sorted(self.attributes.items())},
            "permission_fingerprint": "sha256:" + "0" * 64,
        }

    def without(self, name: str) -> Caller:
        return replace(self, attributes={k: v for k, v in self.attributes.items() if k != name})


# --- strategies ----------------------------------------------------------------------------------


@st.composite
def policy_models(draw: st.DrawFn) -> Policies:
    names = draw(st.lists(st.sampled_from(sorted(SHAPES)), max_size=3, unique=True))
    rows = tuple(
        {
            "name": name,
            "exempt": draw(st.booleans()),
            "orphans": draw(st.sampled_from(("hidden", "visible"))),
        }
        for name in names
    )
    members = draw(st.lists(st.sampled_from(COLUMNS), max_size=2, unique=True))
    columns = tuple(
        {
            "member": member,
            "default": draw(st.sampled_from(("visible", "masked", "hidden"))),
            "insight": draw(st.booleans()),
        }
        for member in members
    )
    return Policies(rows, columns)


# Each attribute a shape reads: values that fit, and one of another type that does not.
FITTING: dict[str, list[Document]] = {
    "channel": [{"type": "text", "value": "web"}, {"type": "text", "value": "shop"}],
    "regions": [
        {"type": "list", "values": ["North"]},
        {"type": "list", "values": ["North", "East"]},
        {"type": "list", "values": ["South", "East", "West"]},
    ],
    "max_customer": [{"type": "integer", "value": "1"}, {"type": "integer", "value": "3"}],
}
NOT_FITTING: dict[str, Document] = {
    "channel": {"type": "list", "values": ["web"]},
    "regions": {"type": "text", "value": "North"},
    "max_customer": {"type": "text", "value": "3"},
}


@st.composite
def callers(draw: st.DrawFn) -> Caller:
    groups = tuple(draw(st.lists(st.sampled_from(GROUPS), max_size=2, unique=True)))
    attributes: dict[str, Document] = {}
    for name, fitting in sorted(FITTING.items()):
        # Mostly fitting, so generated policies filter rows rather than give none (an absent
        # or unfit attribute is P4a, also shown by its directed test).
        how = draw(st.sampled_from(("fits",) * 8 + ("absent", "does_not_fit")))
        if how == "fits":
            attributes[name] = draw(st.sampled_from(fitting))
        elif how == "does_not_fit":
            attributes[name] = NOT_FITTING[name]
    if draw(st.booleans()):  # an attribute no policy reads changes nothing
        attributes["flag"] = {"type": "boolean", "value": draw(st.booleans())}
    return Caller(groups, attributes)


@st.composite
def mappings(draw: st.DrawFn) -> Mappings:
    allow = draw(
        st.lists(
            st.sampled_from(((USER, "web"), (USER, "shop"), (USER, None), ("u2", "web"))),
            max_size=3,
            unique=True,
        )
    )
    deny = draw(
        st.lists(
            st.sampled_from(((USER, "North"), (USER, "South"), (USER, None), ("u2", "East"))),
            max_size=3,
            unique=True,
        )
    )
    registered = draw(st.lists(st.sampled_from((USER, "u2")), max_size=2, unique=True))
    return Mappings(tuple(allow), tuple(deny), tuple(registered))


def invalid_contexts() -> st.SearchStrategy[Document]:
    """Documents that break `security-context` v1: never built for a caller (P4c)."""
    broken: list[Document] = [
        {"channel": {"type": "text", "value": None}},
        {"channel": {"type": "text", "value": ""}},
        {"regions": {"type": "list", "values": []}},
        {"regions": {"type": "list", "values": ["North", "North"]}},
        {"max_customer": {"type": "integer", "value": "three"}},
        {"max_customer": {"type": "integer", "value": "007"}},
        {"channel": {"type": "date", "value": "web"}},
        {"channel": None},
    ]
    base = Caller().context()
    return st.sampled_from(broken).map(lambda attributes: {**base, "attributes": attributes})
