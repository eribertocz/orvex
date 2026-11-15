"""The consumer's half of the transform contract's columns exchange (`analysis.ColumnsNeeded`):
call an operation, describe every relation it asks for, and call again until it answers.

Each relation is described once. An implementation that asks again for a relation it was
given, or asks for nothing, breaks the exchange: TRANSFORM_COLUMNS_REPEATED, raised as the
contract's `TransformError` (fail closed: never a loop, never an answer without the columns).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from orvex.contracts.transform.analysis import ColumnsNeeded, RelationColumns
from orvex.contracts.transform.interface import Known, TransformError


@dataclass(frozen=True, slots=True)
class ColumnsExchange[Answer]:
    """Answers operations whose result is `Answer` once given the columns they read:
    `describe` gives a relation's columns, None when it cannot be described."""

    describe: Callable[[tuple[str, ...]], Sequence[str] | None]

    def answer(self, ask: Callable[[Known], Answer | ColumnsNeeded]) -> Answer:
        """The answer of `ask`, given the columns of every relation it asks for."""
        known: dict[tuple[str, ...], RelationColumns] = {}
        while True:
            answer = ask(tuple(known.values()))
            if not isinstance(answer, ColumnsNeeded):
                return answer
            repeated = [relation for relation in answer.relations if relation in known]
            if repeated or not answer.relations:
                raise TransformError(
                    "TRANSFORM_COLUMNS_REPEATED",
                    f"transform asked again for the columns of {repeated or 'no relation'}",
                    {"relations": ", ".join(".".join(relation) for relation in repeated)},
                )
            for relation in answer.relations:
                found = self.describe(relation)
                columns = None if found is None else tuple(found)
                known[relation] = RelationColumns(relation, columns)
