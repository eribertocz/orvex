"""The managed lake's names in SQL: its database, the catalog database DuckLake keeps for it, and
the names its sources may not take.
"""

from __future__ import annotations

from orvex.store.errors import StoreError

LAKE = "lake"
METADATA_PREFIX = "__ducklake_metadata"
METADATA = f"{METADATA_PREFIX}_{LAKE}"
RESERVED_SOURCE_NAMES = frozenset({LAKE, "main", "memory", "system", "temp"})


def _check_source_names(names: list[str]) -> None:
    """`STORE_SOURCE_INVALID` for a reserved source name or one declared more than once."""
    reserved = sorted(set(names) & RESERVED_SOURCE_NAMES)
    if reserved:
        # The contract excludes these names too; generated models do not check `not`.
        raise StoreError(
            "STORE_SOURCE_INVALID",
            f"Source name {reserved[0]} is reserved.",
            {"source": reserved[0]},
        )
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise StoreError(
            "STORE_SOURCE_INVALID",
            f"Source {duplicates[0]} is declared more than once.",
            {"source": duplicates[0]},
        )
