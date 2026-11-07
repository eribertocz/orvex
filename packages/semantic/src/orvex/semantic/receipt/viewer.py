"""The receipt viewer of one caller (P2-27a, #363): what a `security-context` v1 caller may see
in a receipt view, from the compiled model and its policies.

- A **member** is seen when its protection for the caller is `visible`; a masked or hidden one
  is never named.
- A **value** in the question is shown only on a member the caller sees.
- A **dataset** is seen when at least one of its members is visible to the caller.
- A **policy** is named only to a builder (`semantic-error` disclosure, #304).

Anything the model does not know is not seen (fail closed).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.ossie import sql
from orvex.semantic.policies import PolicyModel, read_caller


class CallerViewer:
    """A `Viewer` for the caller a `security-context` v1 document describes."""

    __slots__ = ("_builder", "_datasets", "_visible")

    def __init__(
        self, compiled: CompiledModel, policies: PolicyModel, context: Mapping[str, Any]
    ) -> None:
        caller = read_caller(context)
        visible: set[str] = set()
        datasets: set[str] = set()
        for member_id, member in compiled.members.items():
            if policies.protection(member_id, caller.groups) != "visible":
                continue
            visible.add(member.parsed.label)
            if member_id.dataset is not None:
                datasets.add(member_id.dataset)
        self._visible = frozenset(visible)
        self._datasets = frozenset(datasets)
        self._builder = caller.builder

    @property
    def builder(self) -> bool:
        return self._builder

    def sees_member(self, member: str) -> bool:
        return isinstance(member, str) and member in self._visible

    def sees_value(self, member: str, value: Mapping[str, object]) -> bool:
        return self.sees_member(member)

    def sees_dataset(self, dataset: str) -> bool:
        return isinstance(dataset, str) and sql.normalize(dataset) in self._datasets

    def knows_policy(self, name: str) -> bool:
        return self._builder
