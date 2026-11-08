"""Compatibility of a JSON Schema with an earlier one, node by node (P2-01, #276).

One rule decides compatibility everywhere: the frozen `ossie-extensions` v1 surface
(tests/contracts/test_ossie_extensions.py) and the upstream Ossie schema watch
(tools/ossie/watch.py, #285). The only allowed changes are a new optional member, a new enum
value, a new `$defs` entry and documentation. Below `if` and `not` a looser subschema makes the
schema stricter, so there nothing may change at all.
"""

from __future__ import annotations

import json
from typing import Any

DOCUMENTATION = ("description", "title", "$comment", "examples")


# Keywords whose subschema counts against the instance (or only selects a rule): loosening it
# tightens the schema, so their subtrees must stay identical.
INVERTED = ("if", "not")


def incompatible(old: Any, new: Any, pointer: str = "#", names: bool = False) -> list[str]:  # noqa: ANN401
    """Every way `new` accepts less than `old`, node by node. `names` marks a map of member or
    definition names in which a new name is compatible."""
    parent, _, key = pointer.rpartition("/")
    if key in INVERTED and not parent.endswith(("/properties", "/$defs")):
        return [] if old == new else [f"{pointer} changed (inverted subschema)"]
    is_name_map = pointer.endswith(("/properties", "/$defs"))
    if isinstance(old, dict) and isinstance(new, dict):
        problems: list[str] = []
        for key, before in old.items():
            here = f"{pointer}/{key}"
            if key in DOCUMENTATION and not is_name_map:
                continue
            if key not in new:
                problems.append(f"{here} removed")
            elif key == "enum" and not is_name_map:
                if {json.dumps(v) for v in before} - {json.dumps(v) for v in new[key]}:
                    problems.append(f"{here} lost values")
            elif key == "required" and not is_name_map:
                if set(new[key]) - set(before):
                    problems.append(f"{here} gained {sorted(set(new[key]) - set(before))}")
            else:
                # A new definition is always compatible. A new property is a new optional member
                # only where the object was closed (it refused that name before); elsewhere, as in
                # a `then` or an `allOf` branch, it constrains a member that already exists.
                opens = not is_name_map and (
                    key == "$defs"
                    or (key == "properties" and old.get("additionalProperties") is False)
                )
                problems.extend(incompatible(before, new[key], here, names=opens))
        added = set(new) - set(old) - (set() if is_name_map else set(DOCUMENTATION))
        if not (is_name_map and names):
            problems.extend(f"{pointer}/{key} added (a new constraint)" for key in sorted(added))
        return problems
    if isinstance(old, list) and isinstance(new, list):
        if len(new) != len(old):
            return [f"{pointer} changed length"]
        return [
            problem
            for index, item in enumerate(old)
            for problem in incompatible(item, new[index], f"{pointer}/{index}")
        ]
    return [] if old == new else [f"{pointer} changed"]
