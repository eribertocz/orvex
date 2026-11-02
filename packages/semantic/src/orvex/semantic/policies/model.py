"""`security.yaml` read into an immutable `PolicyModel`, bound to a compiled model (P2-08, #304).

`read_policies` refuses, before any query runs:
- a file that is not strict YAML or not a `security-policies` v1 document;
- an unknown dataset, member, column, group or role path;
- a predicate outside the closed grammar;
- a deny-list without a registration, and a masked metric;
- a model element that exposes a protected member (`POLICY_INVALID`);
- two column policies on one member (`DUPLICATE_COLUMN_POLICY`);
- a subquery on a table outside the managed lake (`POLICY_NOT_SUPPORTED_ON_ENGINE`).

It also computes what P2-21 and P3 place in plans:
- the strictest protection each member inherits from what it reads;
- where each row policy propagates, across every relationship;
- which datasets are unusable because a policy they need cannot run yet.

No error ever carries a predicate or a value.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Literal

from jsonschema import Draft202012Validator
from orvex.semantic.canonical import fingerprint
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.policies import predicate as grammar
from orvex.semantic.policies.strict_yaml import StrictYamlError, load

SCHEMA_FILE = "security-policies-v1.json"
MAX_PROBLEMS = 50
MAX_PATHS = 256
DEFAULT_POLICY_VALUES = 1000
Protection = Literal["visible", "masked", "hidden"]
_RANK: dict[str, int] = {"visible": 0, "masked": 1, "hidden": 2}


@cache
def _validator() -> Draft202012Validator:
    schema = json.loads(files("orvex.semantic.policies").joinpath(SCHEMA_FILE).read_bytes())
    return Draft202012Validator(schema)


def _split(reference: str) -> list[str]:
    parts = re.findall(r'"(?:[^"]|"")*"|[^."]+', reference)
    return [sql.normalize(part) for part in parts]


def _items(node: object, key: str) -> list[Any]:
    value = node.get(key, ()) if isinstance(node, Mapping) else ()
    return list(value) if isinstance(value, list | tuple) else []


@dataclass(frozen=True, slots=True)
class Reach:
    """A dataset a row policy reaches: by `path` (relationship names, from the policy's
    dataset), and how: `many_to_one`, `one_to_one` or `bridge_allocation` (#238 rule 1)."""

    dataset: str
    path: tuple[str, ...]
    via: str


@dataclass(frozen=True, slots=True)
class RowRule:
    name: str
    dataset: str
    kind: str
    predicate: grammar.Node | None
    exempt_groups: frozenset[str]
    orphans: str
    roles: frozenset[str]
    registration: Mapping[str, str] | None
    attributes: frozenset[str]
    deny_list: bool


@dataclass(frozen=True, slots=True)
class ColumnRule:
    member: MemberId
    default: Protection
    visible_to_groups: frozenset[str]
    mask: str | None
    conditional: bool  # `visible_when`: masked where the row predicate is false


class _Problems:
    def __init__(self, lines: Mapping[str, int]) -> None:
        self.lines = lines
        self.found: set[tuple[str, str]] = set()

    def add(self, pointer: str, rule: str) -> None:
        self.found.add((pointer, rule))

    def line(self, pointer: str) -> int:
        while pointer not in self.lines and pointer:
            pointer = pointer.rsplit("/", 1)[0]
        return self.lines.get(pointer, 1)

    def raise_if_any(self, file: str) -> None:
        if not self.found:
            return
        ordered = sorted(self.found, key=lambda p: (self.line(p[0]), p[0], p[1]))
        shown = ordered[:MAX_PROBLEMS]
        raise SemanticError(
            "POLICY_INVALID",
            {
                "file": file,
                "problems": [
                    {"pointer": pointer, "line": str(self.line(pointer)), "rule": rule}
                    for pointer, rule in shown
                ],
                "problems_omitted": str(len(ordered) - len(shown)),
            },
        )


def _syntax(file: str, line: int) -> SemanticError:
    return SemanticError(
        "POLICY_INVALID",
        {
            "file": file,
            "problems": [{"pointer": "", "line": str(line), "rule": "syntax"}],
            "problems_omitted": "0",
        },
    )


class _Names:
    """The compiled model's datasets, members, relationships and payloads."""

    def __init__(self, compiled: CompiledModel) -> None:
        parsed = compiled.typed.parsed
        self.members: dict[MemberId, Any] = dict(parsed.members)
        self.all_members = set(parsed.members) | set(parsed.excluded)
        document = compiled.ossie.document
        self.payloads = {
            e.pointer.rsplit("/custom_extensions/", 1)[0]: e.payload
            for e in compiled.ossie.product_extensions
        }
        self.datasets = {sql.normalize(str(d["name"])) for d in _items(document, "datasets")}
        self.bridges = {
            sql.normalize(str(d["name"]))
            for at, d in enumerate(_items(document, "datasets"))
            if "bridge" in self.payloads.get(f"/datasets/{at}", {})
        }
        self.relationships: list[dict[str, Any]] = []
        for at, relationship in enumerate(_items(document, "relationships")):
            payload = self.payloads.get(f"/relationships/{at}", {})
            self.relationships.append(
                {
                    "name": sql.normalize(str(relationship["name"])),
                    "from": sql.normalize(str(relationship["from"])),
                    "to": sql.normalize(str(relationship["to"])),
                    "from_columns": [
                        sql.normalize(str(c)) for c in _items(relationship, "from_columns")
                    ],
                    "to_columns": [
                        sql.normalize(str(c)) for c in _items(relationship, "to_columns")
                    ],
                    "cardinality": payload.get("cardinality", "many_to_one"),
                }
            )
        self.model_payload = self.payloads.get("", {})

    def dataset(self, reference: str) -> str | None:
        parts = _split(reference)
        return parts[0] if len(parts) == 1 and parts[0] in self.datasets else None

    def member(self, reference: str, near: str | None = None) -> MemberId | None:
        parts = _split(reference)
        if len(parts) == 1:
            candidates = [MemberId("field", near, parts[0])] if near else []
            candidates.append(MemberId("metric", None, parts[0]))
        elif len(parts) == 2:
            candidates = [
                MemberId("field", parts[0], parts[1]),
                MemberId("metric", parts[0], parts[1]),
            ]
        else:
            return None
        return next((c for c in candidates if c in self.all_members), None)

    def field(self, dataset: str, name: str) -> bool:
        return MemberId("field", dataset, name) in self.all_members


def _check_predicate(
    text: str,
    dataset: str,
    pointer: str,
    names: _Names,
    managed: Mapping[str, Collection[str]],
    problems: _Problems,
    outside: list[tuple[str, str]],
    owner: tuple[str, str],
) -> grammar.Node | None:
    try:
        node = grammar.parse(text, dataset)
    except grammar.PredicateError, RecursionError:
        problems.add(pointer, "predicate")
        return None
    if not all(names.field(dataset, field) for field in grammar.fields(node)):
        problems.add(pointer, "reference")
    for table, columns in grammar.tables(node):
        found = managed.get(table.lower())
        if found is None:
            outside.append(owner)
        elif not columns <= {c.lower() for c in found}:
            problems.add(pointer, "reference")
    return node


def _table_columns(
    table: str, columns: Sequence[str], managed: Mapping[str, Collection[str]]
) -> bool | None:
    """None outside the managed lake; else whether every column exists."""
    found = managed.get(table.lower())
    if found is None:
        return None
    return {c.lower() for c in columns} <= {c.lower() for c in found}


class PolicyModel:
    """The validated policies of a project, immutable and bound to one compiled model."""

    def __init__(
        self,
        rows: tuple[RowRule, ...],
        columns: Mapping[MemberId, ColumnRule],
        sources: Mapping[MemberId, frozenset[MemberId]],
        reaches: Mapping[str, tuple[Reach, ...]],
        unusable: Mapping[str, str],
        limits: Mapping[str, int],
        policy_fingerprint: str,
    ) -> None:
        self.row_policies = rows
        self.column_policies = MappingProxyType(dict(columns))
        self._sources = MappingProxyType(dict(sources))
        self.propagation = MappingProxyType(dict(reaches))
        self.unusable = MappingProxyType(dict(unusable))
        self.limits = MappingProxyType(dict(limits))
        self.fingerprint = policy_fingerprint

    def _own(self, member: MemberId, groups: Collection[str]) -> Protection:
        rule = self.column_policies.get(member)
        if rule is None:
            return "visible"
        if rule.conditional:
            return "masked"  # listed as masked, never as visible, until P3 executes it
        if rule.default != "visible" and rule.visible_to_groups & set(groups):
            return "visible"
        return rule.default

    def protection(self, member: MemberId, groups: Collection[str] = ()) -> Protection:
        """The effective protection of `member` for a caller in `groups`: the strictest of its
        own and that of every protected member it reads, by any path. A masked source makes a
        metric hidden and a field masked."""
        level = self._own(member, groups)
        for source in self._sources.get(member, frozenset()):
            inherited = self._own(source, groups)
            if inherited == "masked" and member.kind == "metric":
                inherited = "hidden"
            if _RANK[inherited] > _RANK[level]:
                level = inherited
        return level

    def inherited(self, member: MemberId) -> tuple[MemberId, ...]:
        """The protected members `member` reads, for the compile report."""
        return tuple(sorted(s for s in self._sources.get(member, ()) if s in self.column_policies))

    def missing_attributes(self, policy: str, attributes: Collection[str]) -> frozenset[str]:
        """The attributes a row policy needs that a caller's context lacks. A missing attribute
        is absent from the context, never null or empty (`security-context` v1), and makes
        the policy give zero rows with `POLICY_ATTRIBUTE_MISSING`."""
        rule = next((r for r in self.row_policies if r.name == policy), None)
        if rule is None:
            raise KeyError(policy)
        return frozenset(rule.attributes - set(attributes))

    def check_usable(self, datasets: Iterable[str]) -> None:
        """`UNSUPPORTED_OPERATION` when a dataset needs a policy that cannot run yet: never
        served without it."""
        for dataset in sorted(set(datasets)):
            operation = self.unusable.get(dataset)
            if operation is not None:
                raise SemanticError("UNSUPPORTED_OPERATION", {"operation": operation})


def _sources(names: _Names) -> dict[MemberId, frozenset[MemberId]]:
    """Every member each member reads, transitively: its expression and lineage (P2-04b),
    window partitions and orders, and the columns a relationship pairs with its own."""
    direct: dict[MemberId, set[MemberId]] = {m: set() for m in names.all_members}
    for member_id, parsed in names.members.items():
        direct[member_id] |= set(parsed.depends_on) | set(parsed.lineage)
        payload = names.payloads.get(parsed.pointer, {})
        for step in _items(payload, "context"):
            for reference in _items(step, "targets"):
                parts = _split(str(reference))
                if len(parts) == 1:
                    direct[member_id] |= {
                        m for m in names.all_members if m.kind == "field" and m.name == parts[0]
                    }
                else:
                    target = names.member(str(reference))
                    if target is not None:
                        direct[member_id].add(target)
        window = payload.get("window") if isinstance(payload, Mapping) else None
        if isinstance(window, Mapping):
            # A bare name is a dimension the query groups by: every field of that name may be
            # meant, so each one counts as read (conservative).
            for reference in [*_items(window, "partition_by"), *_items(window, "order_by")]:
                parts = _split(str(reference))
                if len(parts) == 1:
                    direct[member_id] |= {
                        m for m in names.all_members if m.kind == "field" and m.name == parts[0]
                    }
                else:
                    found = names.member(str(reference))
                    if found is not None:
                        direct[member_id].add(found)
    for relationship in names.relationships:
        for left, right in zip(
            relationship["from_columns"], relationship["to_columns"], strict=False
        ):
            a = MemberId("field", relationship["from"], left)
            b = MemberId("field", relationship["to"], right)
            if a in direct and b in direct:
                direct[a].add(b)
                direct[b].add(a)
    closed: dict[MemberId, frozenset[MemberId]] = {}
    for start in direct:
        seen: set[MemberId] = set()
        stack = list(direct[start])
        while stack:
            current = stack.pop()
            if current in seen or current == start:
                continue
            seen.add(current)
            stack.extend(direct.get(current, ()))
        closed[start] = frozenset(seen)
    return closed


def _reaches(names: _Names, dataset: str, roles: frozenset[str]) -> tuple[tuple[Reach, ...], bool]:
    """Every dataset a row policy on `dataset` reaches, by every simple path, and whether the
    paths fit under the bound."""
    edges: dict[str, list[tuple[str, str, str]]] = {}
    pairs: dict[frozenset[str], int] = {}
    for r in names.relationships:
        pairs[frozenset((r["from"], r["to"]))] = pairs.get(frozenset((r["from"], r["to"])), 0) + 1
    for r in names.relationships:
        edges.setdefault(r["to"], []).append((r["from"], r["name"], r["cardinality"]))
        if r["cardinality"] == "one_to_one":
            edges.setdefault(r["from"], []).append((r["to"], r["name"], "one_to_one"))
    role_names = {
        r["name"] for r in names.relationships if pairs[frozenset((r["from"], r["to"]))] > 1
    }
    found: list[Reach] = []
    stack: list[tuple[str, tuple[str, ...], frozenset[str]]] = [(dataset, (), frozenset({dataset}))]
    while stack:
        current, path, visited = stack.pop()
        for target, name, cardinality in edges.get(current, ()):
            if path and name == path[-1]:
                continue  # the same relationship walked back (one-to-one): not a new path
            if roles and name in role_names and name not in roles:
                continue
            route = (*path, name)
            via = "bridge_allocation" if target in names.bridges else cardinality
            found.append(Reach(target, route, via))
            if len(found) > MAX_PATHS:
                return (), False
            if target not in visited:  # a cycle is reported once, never looped
                stack.append((target, route, visited | {target}))
    return tuple(sorted(found, key=lambda r: (r.dataset, r.path))), True


def _role_paths(names: _Names, dataset: str) -> set[str]:
    """The role relationships (two or more between one pair of datasets) on a path that a row
    policy on `dataset` propagates along: the only names its `roles` may hold."""
    found, _ = _reaches(names, dataset, frozenset())
    pairs: dict[frozenset[str], int] = {}
    for r in names.relationships:
        pairs[frozenset((r["from"], r["to"]))] = pairs.get(frozenset((r["from"], r["to"])), 0) + 1
    role_names = {
        r["name"] for r in names.relationships if pairs[frozenset((r["from"], r["to"]))] > 1
    }
    return {name for reach in found for name in reach.path if name in role_names}


def _expression_members(text: object, names: _Names) -> list[MemberId | None]:
    """The members an expression of the product's language reads; an expression that does not
    parse reads nothing here (the compiler has already refused it)."""
    if not isinstance(text, str):
        return []
    try:
        tree = sql.parse(text)
    except sql.SqlSyntaxError, sql.SqlUnsupported:
        return []
    return [names.member(".".join(ref.parts)) for ref in sql.references(tree)]


def _ordered(document: Mapping[str, Any]) -> dict[str, Any]:
    """The document with its unordered collections in one order, for the fingerprint."""
    ordered = dict(document)
    for key, sort_key in (
        ("row_policies", lambda p: p["name"]),
        ("column_policies", lambda p: p["member"]),
        ("aggregate_thresholds", lambda p: json.dumps(p, sort_keys=True)),
    ):
        items = [dict(item) for item in document.get(key, [])]
        for item in items:
            for group_key in ("exempt_groups", "visible_to_groups", "roles"):
                if group_key in item:
                    item[group_key] = sorted(item[group_key])
        ordered[key] = sorted(items, key=sort_key)
    return ordered


def read_policies(
    text: str | bytes,
    compiled: CompiledModel,
    *,
    groups: Collection[str],
    managed_tables: Mapping[str, Collection[str]],
    file: str = "security.yaml",
) -> PolicyModel:
    """The project's policies, or a `SemanticError`. `groups` are the project's groups and
    `managed_tables` the managed-lake tables (`schema.table` → columns), both from `server`."""
    try:
        document, lines = load(text)
    except StrictYamlError as error:
        raise _syntax(file, error.line) from None
    problems = _Problems(lines)
    for invalid in _validator().iter_errors(document):
        problems.add("".join(f"/{part}" for part in invalid.absolute_path), "schema")
    problems.raise_if_any(file)
    assert isinstance(document, dict)
    names = _Names(compiled)
    managed = {table.lower(): columns for table, columns in managed_tables.items()}
    known_groups = set(groups)
    outside: list[tuple[str, str]] = []
    rows: list[RowRule] = []
    seen_names: set[str] = set()
    relationship_names = {r["name"] for r in names.relationships}
    for at, policy in enumerate(document.get("row_policies", [])):
        here = f"/row_policies/{at}"
        if policy["name"] in seen_names:
            problems.add(f"{here}/name", "duplicate_name")
        seen_names.add(policy["name"])
        dataset = names.dataset(policy["dataset"])
        if dataset is None:
            problems.add(f"{here}/dataset", "reference")
            continue
        if not set(policy.get("exempt_groups", [])) <= known_groups:
            problems.add(f"{here}/exempt_groups", "reference")
        roles = frozenset(_split(role)[0] for role in policy.get("roles", []))
        if roles and not (roles <= relationship_names and roles <= _role_paths(names, dataset)):
            problems.add(f"{here}/roles", "reference")
        node: grammar.Node | None = None
        attributes: set[str] = set()
        deny = False
        if policy["kind"] == "predicate":
            owner = (str(policy["dataset"]), str(policy["name"]))
            node = _check_predicate(
                policy["predicate"],
                dataset,
                f"{here}/predicate",
                names,
                managed,
                problems,
                outside,
                owner,
            )
            if node is not None:
                attributes = grammar.attributes(node)
                deny = grammar.deny_list(node)
                if deny and "registration" not in policy:
                    problems.add(here, "registration")
        else:
            closure = policy["closure"]
            fits = _table_columns(
                closure["table"], [closure["ancestor"], closure["descendant"]], managed
            )
            if fits is None:
                outside.append((str(policy["dataset"]), str(policy["name"])))
            elif not fits:
                problems.add(f"{here}/closure", "reference")
            if not names.field(dataset, sql.normalize(policy["field"])):
                problems.add(f"{here}/field", "reference")
            attributes = {policy["node"]}
        registration = policy.get("registration")
        if registration is not None:
            fits = _table_columns(registration["table"], [registration["key"]], managed)
            if fits is None:
                outside.append((str(policy["dataset"]), str(policy["name"])))
            elif not fits:
                problems.add(f"{here}/registration", "reference")
        rows.append(
            RowRule(
                name=policy["name"],
                dataset=dataset,
                kind=policy["kind"],
                predicate=node,
                exempt_groups=frozenset(policy.get("exempt_groups", [])),
                orphans=policy.get("orphans", "hidden"),
                roles=roles,
                registration=MappingProxyType(dict(registration)) if registration else None,
                attributes=frozenset(attributes),
                deny_list=deny,
            )
        )
    columns: dict[MemberId, ColumnRule] = {}
    duplicates: list[MemberId] = []
    for at, policy in enumerate(document.get("column_policies", [])):
        here = f"/column_policies/{at}"
        member = names.member(policy["member"])
        if member is None:
            problems.add(f"{here}/member", "reference")
            continue
        if member.kind == "metric" and (policy["default"] == "masked" or "visible_when" in policy):
            problems.add(f"{here}/default", "masked_metric")
        if not set(policy.get("visible_to_groups", [])) <= known_groups:
            problems.add(f"{here}/visible_to_groups", "reference")
        if "visible_when" in policy and member.dataset is not None:
            owner = (member.dataset.lower(), str(policy["member"]))
            _check_predicate(
                policy["visible_when"],
                member.dataset,
                f"{here}/visible_when",
                names,
                managed,
                problems,
                outside,
                owner,
            )
        if member in columns:
            duplicates.append(member)
        columns[member] = ColumnRule(
            member=member,
            default=policy["default"],
            visible_to_groups=frozenset(policy.get("visible_to_groups", [])),
            mask=policy.get("mask"),
            conditional="visible_when" in policy,
        )
    unusable: dict[str, str] = {}
    for at, threshold in enumerate(document.get("aggregate_thresholds", [])):
        here = f"/aggregate_thresholds/{at}"
        if names.member(threshold["entity"]) is None:
            problems.add(f"{here}/entity", "reference")
        if "dataset" in threshold:
            target = names.dataset(threshold["dataset"])
            covered = {target} if target else set()
        else:
            metric = names.member(threshold["metric"])
            covered = (
                {d.dataset for d in names.members[metric].lineage if d.dataset}
                if metric in names.members
                else set()
            )
            if metric is None:
                problems.add(f"{here}/metric", "reference")
        if not covered and "dataset" in threshold:
            problems.add(f"{here}/dataset", "reference")
        for dataset in covered:
            unusable.setdefault(dataset, "min_group_size")
    sources = _sources(names)
    protected = {m for m, rule in columns.items() if rule.default != "visible" or rule.conditional}

    def exposes(members: Iterable[MemberId | None]) -> bool:
        return any(
            m is not None and (m in protected or bool(sources.get(m, frozenset()) & protected))
            for m in members
        )

    # Model elements a query applies by name, as filters or members, never expose a protected
    # member: segments (their dimension, metric and condition) and member or metric parameters.
    for segment in _items(names.model_payload, "segments"):
        read = [names.member(str(segment["dimension"])), names.member(str(segment["metric"]))]
        read += _expression_members(segment.get("condition"), names)
        if exposes(read):
            problems.add("", "inference")
    for parameter in _items(names.model_payload, "parameters"):
        if parameter.get("type") in {"member", "metric"}:
            named = [str(parameter["default"]), *(str(v) for v in _items(parameter, "allowed"))]
            if exposes(names.member(value) for value in named):
                problems.add("", "inference")
    reaches: dict[str, tuple[Reach, ...]] = {}
    for at, rule in enumerate(rows):
        found, bounded = _reaches(names, rule.dataset, rule.roles)
        if not bounded:
            problems.add(f"/row_policies/{at}", "propagation")
        reaches[rule.name] = found
        if rule.kind == "hierarchy":
            for dataset in {rule.dataset, *(r.dataset for r in found)}:
                unusable.setdefault(dataset, "hierarchy_policy")
    for member, column in columns.items():
        if column.conditional and member.dataset is not None:
            unusable.setdefault(member.dataset, "cell_policy")
    problems.raise_if_any(file)
    if duplicates:
        raise SemanticError("DUPLICATE_COLUMN_POLICY", {"member": _label(duplicates[0])})
    if outside:
        dataset, owner_name = sorted(outside)[0]
        raise SemanticError(
            "POLICY_NOT_SUPPORTED_ON_ENGINE",
            {"dataset": dataset, "policy": owner_name, "reason": "subquery_outside_lake"},
        )
    limits = {
        "policy_values": document.get("limits", {}).get("policy_values", DEFAULT_POLICY_VALUES)
    }
    return PolicyModel(
        tuple(rows),
        columns,
        sources,
        reaches,
        unusable,
        limits,
        fingerprint("security_policies", 1, _ordered(document)),
    )


def _label(member: MemberId) -> str:
    return (
        member.name.lower()
        if member.dataset is None
        else f"{member.dataset.lower()}.{member.name.lower()}"
    )
