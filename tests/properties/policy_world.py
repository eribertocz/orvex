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

import json
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from hypothesis import strategies as st
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.execution import Answer, answer
from orvex.semantic.policies import PolicyModel, read_policies
from orvex.semantic.result.cursors import SealedForm
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from conformance.runners.security_engines import column_name
from tests.properties.world import CATALOG, COMPILED, World

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


def compiled_policies(policies: Policies) -> PolicyModel:
    return read_policies(
        yaml.safe_dump(policies.document()),
        COMPILED,
        groups=frozenset(GROUPS),
        managed_tables=MANAGED,
    )


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
        how = draw(st.sampled_from(("fits", "fits", "fits", "absent", "does_not_fit")))
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


# --- the lake and the question -------------------------------------------------------------------


@contextmanager
def lake_with(world: World, data: Mappings) -> Iterator[Lake]:
    """The world's tables, and the mapping tables in schema `security`."""
    with tempfile.TemporaryDirectory() as folder:
        inputs = [
            _csv(Path(folder), "customers", CATALOG["customers"], world.customers),
            _csv(Path(folder), "sales", CATALOG["sales"], world.sales),
        ]
        tables: dict[str, list[dict[str, str | None]]] = {
            "channel_allow": [{"user_id": u, "channel": c} for u, c in data.allow],
            "region_deny": [{"user_id": u, "region": r} for u, r in data.deny],
            "registered": [{"user_id": u} for u in data.registered],
        }
        security = Path(folder) / "security"
        security.mkdir()
        mapped = [_csv(security, n, MAPPINGS[n], rows) for n, rows in tables.items()]
        with ce.case_lake(inputs, CATALOG) as lake:
            ce.load_tables(
                lake, mapped, {f"security.{n}": c for n, c in MAPPINGS.items()}, "security"
            )
            yield lake


def _csv(
    folder: Path,
    name: str,
    columns: Mapping[str, SourceColumn],
    rows: list[dict[str, str | None]],
) -> Path:
    path = folder / f"{name}.csv"
    names = list(columns)
    lines = [",".join(names)]
    lines += [",".join("" if r[n] is None else str(r[n]) for n in names) for r in rows]
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path


def ask(document: Document, lake: Lake, policy: PolicyModel, context: Mapping[str, Any]) -> Answer:
    snapshot = lake.current_snapshot()
    pinned = {"id": snapshot.id, "committed_at": snapshot.committed_at.isoformat()}
    engine = ce.engine_for(
        lake,
        cursors=SealedForm(b"p" * 32),
        pinned=lambda version: pinned if version == str(snapshot.id) else None,
    )
    return answer(json.dumps(document), COMPILED, policy, dict(context), engine)


def every_page(
    query: Document, lake: Lake, policy: PolicyModel, context: Mapping[str, Any]
) -> tuple[list[list[str | None]], list[str], list[str]]:
    """A detail question's rows over all its pages, its columns and its first page's notices."""
    rows: list[list[str | None]] = []
    document: Document = query
    columns: list[str] = []
    notices: list[str] = []
    while True:
        found = ask(document, lake, policy, context)
        envelope = found.result.envelope()
        if not columns:
            columns = [column_name(c) for c in envelope["columns"]]
            notices = [str(n["code"]) for n in envelope.get("notices", [])]
        rows += ce.rows_of(found)
        cursor = envelope["page"].get("next")
        if cursor is None:
            return rows, columns, notices
        document = {**query, "page": {**query["page"], "after": cursor}}
