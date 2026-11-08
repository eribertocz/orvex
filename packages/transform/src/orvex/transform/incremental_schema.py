"""Schema changes of an incremental target: what changed and the `ALTER TABLE` steps allowed."""

from __future__ import annotations

import re

from orvex.transform.incremental_types import Columns, quote_identifier, target_table
from orvex.transform.models import Model

# Type changes that keep every existing value exactly (sync_all_columns); nothing else is applied.
_SIGNED_INTEGERS = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT")
_UNSIGNED_INTEGERS = ("UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT")
_DECIMAL = re.compile(r"DECIMAL\((\d+),(\d+)\)")


def schema_change(model: Model, output: Columns, existing: Columns | None) -> str | None:
    """Why the output no longer matches the existing target, or None."""
    if existing is None:
        return None
    new = [(name.lower(), kind) for name, kind in output]
    old = [(name.lower(), kind) for name, kind in existing]
    if new == old:
        return None
    added = [name for name, _ in new if name not in dict(old)]
    removed = [name for name, _ in old if name not in dict(new)]
    retyped = [
        f"{name} {dict(old)[name]} → {kind}"
        for name, kind in new
        if name in dict(old) and dict(old)[name] != kind
    ]
    parts = []
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    if retyped:
        parts.append("changed " + ", ".join(retyped))
    return "; ".join(parts) or "the column order changed"


def widens(old: str, new: str) -> bool:
    """True when every value of type `old` casts to `new` exactly (and back)."""
    for family in (_SIGNED_INTEGERS, _UNSIGNED_INTEGERS):
        if old in family and new in family:
            return family.index(new) > family.index(old)
    old_decimal, new_decimal = _DECIMAL.fullmatch(old), _DECIMAL.fullmatch(new)
    if old_decimal and new_decimal:
        old_precision, old_scale = map(int, old_decimal.groups())
        new_precision, new_scale = map(int, new_decimal.groups())
        return new_scale == old_scale and new_precision > old_precision
    return old == "DATE" and new == "TIMESTAMP"


def schema_statements(model: Model, output: Columns, existing: Columns) -> list[str] | str:
    """The `ALTER TABLE` statements that make the target take the output's columns under the
    model's `on_schema_change`, or why it cannot (the run then fails with SCHEMA_CHANGED)."""
    changed = schema_change(model, output, existing)
    if changed is None:
        return []
    policy = model.config.on_schema_change or "fail"
    if policy == "fail":
        return changed
    target = target_table(model)
    old = {name.lower(): (name, kind) for name, kind in existing}
    new = {name.lower(): (name, kind) for name, kind in output}
    added = [new[key] for key in new if key not in old]
    removed = [old[key] for key in old if key not in new]
    retyped = [(old[key], new[key][1]) for key in new if key in old and old[key][1] != new[key][1]]
    if policy == "append_new_columns" and (removed or retyped):
        return changed + "; append_new_columns only adds columns"
    refused = [f"{name} {kind} → {to}" for (name, kind), to in retyped if not widens(kind, to)]
    if refused:
        return "cannot change " + ", ".join(refused) + " without changing values"
    # Added first, dropped last: the table never has zero columns.
    return (
        [f"ALTER TABLE {target} ADD COLUMN {quote_identifier(name)} {kind}" for name, kind in added]
        + [
            f"ALTER TABLE {target} ALTER COLUMN {quote_identifier(name)} SET DATA TYPE {to}"
            for (name, _), to in retyped
        ]
        + [f"ALTER TABLE {target} DROP COLUMN {quote_identifier(name)}" for name, _ in removed]
    )
