"""Which row policies restrict which datasets, for one caller (P2-21, #341).

`security-policies` v1 says what a policy means; this module says where it applies:
- a policy restricts its own dataset, and every dataset it reaches by propagation, each along
  its own path (`Reach`), so each role path is restricted unless the policy names its roles;
- `exempt_groups` exempt from that policy only, by the caller's own groups, never a role;
- a policy whose attribute the caller's context lacks gives zero rows (`no_rows`), with the
  notice `POLICY_ATTRIBUTE_MISSING`; it is never an error, and never all rows;
- a policy that reaches a dataset only through a bridge has no meaning yet: the dataset is not
  read (`row_policy`), never read without it.

The attributes come only from the validated security context (`read_caller`): the query has no
member for them, so a client can never supply or change one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from orvex.semantic.errors import SemanticError
from orvex.semantic.policies.caller import Attribute, Caller
from orvex.semantic.policies.model import PolicyModel, RowRule


@dataclass(frozen=True, slots=True)
class Restriction:
    """One row policy on one dataset, as one caller sees it."""

    rule: RowRule
    path: tuple[str, ...]  # relationship names from the policy's dataset; () for its own
    no_rows: bool  # an attribute it needs is absent from the caller's context


@dataclass(frozen=True, slots=True)
class Placement:
    """Every restriction of every dataset a question reads, and what the receipt records."""

    restrictions: Mapping[str, tuple[Restriction, ...]]  # dataset -> its restrictions
    applied: Mapping[str, str]  # policy -> restricted, exempt or no_rows
    attributes: Mapping[str, Attribute]

    def of(self, dataset: str) -> tuple[Restriction, ...]:
        return self.restrictions.get(dataset, ())


def place(policies: PolicyModel, datasets: Iterable[str], caller: Caller) -> Placement:
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
        no_rows = bool(policies.missing_attributes(rule.name, caller.attributes))
        applied[rule.name] = "no_rows" if no_rows else "restricted"
        for path, dataset in hits:
            found.setdefault(dataset, []).append(Restriction(rule, path, no_rows))
    return Placement({d: tuple(r) for d, r in found.items()}, applied, dict(caller.attributes))
