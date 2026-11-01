"""IANA zones, checked against the pinned `tzdata` package, never the host's database (#280 §2).
Its version enters the model fingerprint (P2-04e)."""

from __future__ import annotations

from importlib import resources

import tzdata  # type: ignore[import-untyped]  # no stubs; the module holds data only

TZDATA_VERSION: str = tzdata.IANA_VERSION
# The package's own list of zone names: exact, independent of the file system's case rules.
ZONES: frozenset[str] = frozenset(
    resources.files("tzdata").joinpath("zones").read_text("utf-8").split()
)


def is_zone(name: str) -> bool:
    return name in ZONES
