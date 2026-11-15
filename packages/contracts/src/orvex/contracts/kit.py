"""The conformance-kit format (#486 G1): the cases every implementation of a contract must pass,
with their recorded count and no skip list.

A kit belongs to one interface contract. Each case has a stable id, the operation it exercises,
its expectation in words (the guarantee the compatibility check holds to, #486 G3) and a check
that receives the implementation through a binding and raises when the guarantee fails. The
kit records how many cases it has; a kit whose cases differ from that count, or that leaves an
operation without a case, is refused when it is built.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .binding import Bound
from .interface import InterfaceContract
from .interface_errors import InterfaceContractError

CASE_ID = re.compile(r"[a-z][a-z0-9]*([.-][a-z0-9]+)*")


@dataclass(frozen=True, slots=True)
class KitCase:
    id: str
    operation: str
    expectation: str
    check: Callable[[Bound], None]


@dataclass(frozen=True, slots=True)
class Kit:
    contract: InterfaceContract
    recorded: int
    cases: tuple[KitCase, ...]

    def __post_init__(self) -> None:
        name = self.contract.name
        operations = {operation.name for operation in self.contract.operations}
        seen: set[str] = set()
        for case in self.cases:
            problem = (
                "its id is not lowercase, dotted or kebab-case"
                if not CASE_ID.fullmatch(case.id)
                else "its id is used twice"
                if case.id in seen
                else f"operation {case.operation!r} is not the contract's"
                if case.operation not in operations
                else "it states no expectation"
                if not case.expectation.strip()
                else None
            )
            if problem is not None:
                raise InterfaceContractError(
                    "KIT_INVALID", f"kit {name}, case {case.id!r}: {problem}"
                )
            seen.add(case.id)
        uncovered = sorted(operations - {case.operation for case in self.cases})
        if uncovered:
            raise InterfaceContractError(
                "KIT_OPERATION_UNCOVERED", f"kit {name}: operations {uncovered} have no case"
            )
        if len(self.cases) != self.recorded:
            raise InterfaceContractError(
                "KIT_CASE_COUNT_CHANGED",
                f"kit {name} records {self.recorded} cases and has {len(self.cases)}",
            )

    def describe(self) -> dict[str, object]:
        """The kit as plain data: what the compatibility check compares (#486 G3)."""
        cases = {c.id: {"operation": c.operation, "expectation": c.expectation} for c in self.cases}
        return {"recorded": self.recorded, "cases": cases}
