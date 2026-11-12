"""Which relationships a caller may not use (#442; #449 Q3 and #442 Q3, both A): those whose
`from_columns` or `to_columns` include a member that is hidden or masked for that caller. For
that caller such a relationship does not exist: the catalog does not list it, and the planner
finds no path through it, so a question that needs it is refused exactly as on a model that never
declared it. Joining through a key tells which row of one dataset belongs to which of the other,
which hiding or masking the key exists to prevent.

This decides only what the caller may ask. Row policies propagate through every relationship of
the model, whatever the caller may see: hiding a key never removes a restriction.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from typing import Any

from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.ossie import sql
from orvex.semantic.policies.model import PolicyModel


def _items(node: object, key: str) -> list[Any]:
    value = node.get(key, ()) if isinstance(node, Mapping) else ()
    return list(value) if isinstance(value, list | tuple) else []


def keys(relationship: Mapping[str, Any]) -> Iterator[MemberId]:
    """The fields a relationship pairs: its `from_columns` and its `to_columns`."""
    left = sql.normalize(str(relationship["from"]))
    right = sql.normalize(str(relationship["to"]))
    for column in _items(relationship, "from_columns"):
        yield MemberId("field", left, sql.normalize(str(column)))
    for column in _items(relationship, "to_columns"):
        yield MemberId("field", right, sql.normalize(str(column)))


def unseen(
    compiled: CompiledModel, policies: PolicyModel, groups: Collection[str]
) -> frozenset[str]:
    """The relationships (normalized names) a caller in `groups` may not use."""
    return frozenset(
        sql.normalize(str(relationship["name"]))
        for relationship in _items(compiled.ossie.document, "relationships")
        if any(policies.protection(key, groups) != "visible" for key in keys(relationship))
    )
