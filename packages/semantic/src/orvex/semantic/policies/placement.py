"""Which row policies restrict which datasets, for one caller (P2-21, #341).

`security-policies` v1 says what a policy means; this module says where it applies:
- a policy restricts its own dataset, and every dataset it reaches by propagation, each along
  its own path (`Reach`), so each role path is restricted unless the policy names its roles;
- `exempt_groups` exempt from that policy only, by the caller's own groups, never a role;
- a policy whose attribute the caller's context lacks, or holds with a type that does not fit
  where the predicate uses it, gives zero rows (`no_rows`), with the notice
  `POLICY_ATTRIBUTE_MISSING`; it is never an error, and never all rows. A value that does not
  fit is never written into the predicate, where a `NOT` could turn it into a match (`fits`);
- a policy that reaches a dataset only through a bridge has no meaning yet: the dataset is not
  read (`row_policy`), never read without it;
- a `visible_when` member the caller sees masked has its condition (`Cell`, #442): a detail row
  shows its value where the condition holds, its mask elsewhere. A condition whose attribute is
  absent, or does not fit, is never written (`fits` again): every row shows the mask.

The attributes come only from the validated security context (`read_caller`): the query has no
member for them, so a client can never supply or change one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.errors import SemanticError
from orvex.semantic.policies import predicate as grammar
from orvex.semantic.policies.caller import Attribute, Caller
from orvex.semantic.policies.model import PolicyModel, RowRule

SCALAR = {"Integer": "integer", "String": "text", "Boolean": "boolean"}  # field type -> attribute
ELEMENT = {"Integer": re.compile(r"-?[0-9]+"), "String": re.compile(r".*", re.S)}


@dataclass(frozen=True, slots=True)
class Restriction:
    """One row policy on one dataset, as one caller sees it."""

    rule: RowRule
    path: tuple[str, ...]  # relationship names from the policy's dataset; () for its own
    no_rows: bool  # an attribute it needs is absent from the caller's context


@dataclass(frozen=True, slots=True)
class Cell:
    """One `visible_when` member, as one caller sees it: its condition on its own dataset, or
    None when an attribute it needs is absent or does not fit, so no row shows the value."""

    dataset: str
    condition: grammar.Node | None


@dataclass(frozen=True, slots=True)
class Placement:
    """Every restriction of every dataset a question reads, and what the receipt records."""

    restrictions: Mapping[str, tuple[Restriction, ...]]  # dataset -> its restrictions
    applied: Mapping[str, str]  # policy -> restricted, exempt or no_rows
    attributes: Mapping[str, Attribute]
    cells: Mapping[MemberId, Cell] = MappingProxyType({})  # the caller's conditional masks

    def of(self, dataset: str) -> tuple[Restriction, ...]:
        return self.restrictions.get(dataset, ())


def datatype(compiled: CompiledModel, dataset: str, field: str) -> str | None:
    """The logical type of a field, the one thing a policy's attribute must fit."""
    typed = compiled.typed.members.get(MemberId("field", dataset, field))
    return None if typed is None else typed.type.logical.datatype


def fits(rule: RowRule, attributes: Mapping[str, Attribute], compiled: CompiledModel) -> bool:
    """Whether every attribute the policy uses is present and of a type that fits its use:
    a scalar of the field's type in a comparison, a list of values of the field's type in
    `IN $list`, and text or an integer against a mapping column or a registration key."""
    registration = rule.registration
    if registration is not None and not _scalar(
        attributes, str(registration.get("attribute", "user_id")), ("text", "integer")
    ):
        return False
    return rule.predicate is not None and fits_predicate(
        rule.predicate, rule.dataset, attributes, compiled
    )


def _scalar(attributes: Mapping[str, Attribute], name: str, wanted: tuple[str, ...]) -> bool:
    found = attributes.get(name)
    return found is not None and found[0] in wanted


def fits_predicate(
    predicate: grammar.Node,
    dataset: str,
    attributes: Mapping[str, Attribute],
    compiled: CompiledModel,
) -> bool:
    """`fits` for one predicate on `dataset`: every attribute present, of a fitting type."""

    def scalar(name: str, wanted: tuple[str, ...]) -> bool:
        return _scalar(attributes, name, wanted)

    def operand(node: object, other: object) -> bool:
        if not isinstance(node, grammar.Attribute):
            return True
        if isinstance(other, grammar.Field):
            kind = SCALAR.get(datatype(compiled, dataset, other.name) or "")
            return kind is not None and scalar(node.name, (kind,))
        if isinstance(other, grammar.Value):
            return scalar(node.name, ({"string": "text"}.get(other.kind, other.kind),))
        if isinstance(other, grammar.Attribute):
            found, then = attributes.get(node.name), attributes.get(other.name)
            return found is not None and then is not None and found[0] == then[0] != "list"
        return False

    def walk(node: object) -> bool:
        if isinstance(node, grammar.Compare):
            return operand(node.left, node.right) and operand(node.right, node.left)
        if isinstance(node, grammar.InAttribute):
            found = attributes.get(node.attribute.name)
            element = ELEMENT.get(datatype(compiled, dataset, node.field.name) or "")
            return (
                found is not None
                and found[0] == "list"
                and element is not None
                and isinstance(found[1], tuple)
                and all(element.fullmatch(v) for v in found[1])
            )
        if isinstance(node, grammar.InSelect | grammar.Exists):
            return all(
                not isinstance(c.value, grammar.Attribute)
                or scalar(c.value.name, ("text", "integer"))
                for c in node.conditions
            )
        if isinstance(node, grammar.Not):
            return walk(node.operand)
        if isinstance(node, grammar.Logical):
            return all(walk(o) for o in node.operands)
        return isinstance(node, grammar.IsNull)

    names = grammar.attributes(predicate)
    return all(name in attributes for name in names) and walk(predicate)


def place(
    policies: PolicyModel, datasets: Iterable[str], caller: Caller, compiled: CompiledModel
) -> Placement:
    """The restrictions of `datasets` for `caller`, or `UNSUPPORTED_OPERATION` (`row_policy`)
    when one is reached only through a bridge."""
    wanted = set(datasets)
    found: dict[str, list[Restriction]] = {}
    applied: dict[str, str] = {}
    for rule in policies.row_policies:
        reaches = [((), rule.dataset)] + [
            (r.path, r.dataset) for r in policies.propagation.get(rule.name, ())
        ]
        hits = [(path, dataset) for path, dataset in reaches if dataset in wanted]
        if not hits:
            continue
        if any(
            r.via == "bridge_allocation"
            for r in policies.propagation.get(rule.name, ())
            if r.dataset in wanted
        ):
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "row_policy"})
        if rule.exempt_groups & set(caller.groups):  # the caller's own groups, never a role
            applied[rule.name] = "exempt"
            continue
        no_rows = bool(policies.missing_attributes(rule.name, caller.attributes)) or not fits(
            rule, caller.attributes, compiled
        )
        applied[rule.name] = "no_rows" if no_rows else "restricted"
        for path, dataset in hits:
            found.setdefault(dataset, []).append(Restriction(rule, path, no_rows))
    cells: dict[MemberId, Cell] = {}
    for member in policies.column_policies:
        condition = policies.condition_of(member, caller.groups)
        if condition is None or member.dataset not in wanted:
            continue
        usable = fits_predicate(condition, member.dataset, caller.attributes, compiled)
        cells[member] = Cell(member.dataset, condition if usable else None)
    return Placement(
        {d: tuple(r) for d, r in found.items()},
        applied,
        dict(caller.attributes),
        MappingProxyType(cells),
    )
