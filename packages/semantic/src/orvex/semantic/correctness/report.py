"""The correctness report (P2-11, #310): what the engine decided before any SQL exists.

A report is immutable and has a canonical fingerprint (RFC 8785, `semantic-engine.md` §4). It
holds the facts the rules derived, the obligations the plan must discharge, and the rules that
were applied, each with its version. A rejection is never a report: it is a `SemanticError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from orvex.semantic.canonical import fingerprint
from orvex.semantic.receipt import Obligation as ReceiptObligation

REPORT_FORMAT = 1
ISSUED = object()  # only `analyze` makes reports: the planner refuses any other (P2-12)


@dataclass(frozen=True, slots=True)
class Obligation:
    """What the plan must make true. `members` and `datasets` are every member and dataset it
    names, as the model writes them: a receipt view shows it only to a viewer who sees them
    all (#310 §3)."""

    id: str
    rule: str  # C1 … C9
    kind: str
    metric: str
    from_grain: tuple[str, ...]
    to_grain: tuple[str, ...]
    members: tuple[str, ...]
    datasets: tuple[str, ...]

    def document(self) -> dict[str, object]:
        return {
            "id": self.id,
            "rule": self.rule,
            "kind": self.kind,
            "metric": self.metric,
            "from_grain": list(self.from_grain),
            "to_grain": list(self.to_grain),
            "members": list(self.members),
            "datasets": list(self.datasets),
        }

    def for_receipt(self) -> ReceiptObligation:
        """The receipt's record of this obligation; its evidence comes from the plan (C9)."""
        return ReceiptObligation(
            rule=self.rule,
            obligation=self.id,
            members=self.members,
            datasets=self.datasets,
        )


@dataclass(frozen=True, slots=True)
class CorrectnessReport:
    model_fingerprint: str
    query_fingerprint: str
    facts: Mapping[str, Any]  # read-only JSON-like values
    obligations: tuple[Obligation, ...]  # sorted by id
    rules: tuple[tuple[str, int], ...]  # (rule, version), in the order they ran
    fingerprint: str
    # What the planner reads; outside the identity, which the fingerprints above already bind.
    bound: Any = field(default=None, compare=False, repr=False)
    compiled: Any = field(default=None, compare=False, repr=False)
    caller_groups: tuple[str, ...] = field(default=(), compare=False, repr=False)
    issued: object = field(default=None, compare=False, repr=False)

    def document(self) -> dict[str, object]:
        return _document(
            self.model_fingerprint, self.query_fingerprint, self.facts, self.obligations, self.rules
        )


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _document(
    model_fingerprint: str,
    query_fingerprint: str,
    facts: Mapping[str, Any],
    obligations: tuple[Obligation, ...],
    rules: tuple[tuple[str, int], ...],
) -> dict[str, object]:
    return {
        "model_fingerprint": model_fingerprint,
        "query_fingerprint": query_fingerprint,
        "facts": _plain(facts),
        "obligations": [o.document() for o in obligations],
        "rules": [[rule, version] for rule, version in rules],
    }


def make_report(
    model_fingerprint: str,
    query_fingerprint: str,
    facts: Mapping[str, Any],
    obligations: list[Obligation],
    rules: tuple[tuple[str, int], ...],
    *,
    bound: Any,  # noqa: ANN401
    compiled: Any,  # noqa: ANN401
    caller_groups: tuple[str, ...],
) -> CorrectnessReport:
    ordered = tuple(sorted(obligations, key=lambda o: o.id))
    document = _document(model_fingerprint, query_fingerprint, facts, ordered, rules)
    return CorrectnessReport(
        model_fingerprint=model_fingerprint,
        query_fingerprint=query_fingerprint,
        facts=_frozen(facts),
        obligations=ordered,
        rules=rules,
        fingerprint=fingerprint("correctness_report", REPORT_FORMAT, document),
        bound=bound,
        compiled=compiled,
        caller_groups=caller_groups,
        issued=ISSUED,
    )


def _frozen(value: object) -> Any:  # noqa: ANN401
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _frozen(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_frozen(v) for v in value)
    return value
