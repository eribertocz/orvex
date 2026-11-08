"""The one check every DuckDB-to-Arrow export runs (#410): a column whose type nests a UHUGEINT.

Arrow has no exact unsigned 128-bit type. At the top level a UHUGEINT is read as decimal text;
nested in a LIST, ARRAY, STRUCT, MAP or UNION, DuckDB would wrap it into decimal128(38,0) (the
maximum becomes -1), so the whole read is refused before any row is produced.
"""

from __future__ import annotations

from collections.abc import Sequence

from duckdb.sqltypes import DuckDBPyType
from orvex.store.errors import StoreError

_NESTED = frozenset({"list", "array", "struct", "map", "union"})


def refuse_nested_uhugeint(names: Sequence[str], kinds: Sequence[DuckDBPyType]) -> None:
    """Raise `STORE_TYPE_UNSUPPORTED` for the first column whose type nests a UHUGEINT."""
    for name, kind in zip(names, kinds, strict=True):
        if kind.id in _NESTED and _holds_uhugeint(kind):
            raise StoreError(
                "STORE_TYPE_UNSUPPORTED",
                f"Column {name} holds a UHUGEINT inside {kind}, which Arrow cannot carry "
                "exactly; cast it to VARCHAR in the query.",
                {"column": name, "type": str(kind)},
            )


def _holds_uhugeint(kind: DuckDBPyType) -> bool:
    if kind.id == "uhugeint":
        return True
    if kind.id not in _NESTED:
        return False
    return any(
        isinstance(child, DuckDBPyType) and _holds_uhugeint(child) for _, child in kind.children
    )
